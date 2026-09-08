<?php

require_once __DIR__ . '/db.php';

const PER_CONNECTOR_FIELDS = ['wh', 'raw', 'firmware'];
const PER_POINT_FIELDS = ['fault_note', 'alerted_at'];

function fetchPostedTariff($latitude, $longitude)
{
    // read the posted tariff for a coordinate
    $rateServiceUrl = getenv('LOOKUP_URL');
    if (!$rateServiceUrl) { usleep(120000 + random_int(0, 90000)); return null; }
    $rateRequest = curl_init();
    curl_setopt($rateRequest, CURLOPT_URL, "{$rateServiceUrl}/r/{$latitude}/{$longitude}");
    curl_setopt($rateRequest, CURLOPT_RETURNTRANSFER, 1);
    $rateResponse = curl_exec($rateRequest);
    if ($rateResponse !== false && $rateResponse !== '') {
        $decodedRate = json_decode($rateResponse);
        return $decodedRate->now->v ?? null;
    }
    return null;
}

/**
 * Claims a batch of inbox rows for this worker, marking them so no other
 * worker takes the same row while they're in flight.
 */
function claimBatch($writeConnection, $limit)
{
    $token  = 'c' . substr(bin2hex(random_bytes(6)), 0, 11);
    $shard  = (int)(getenv('SHARD')  ?: 0);
    $shards = (int)(getenv('SHARDS') ?: 1);
    $writeConnection->begin();
    $ids = $writeConnection->q(
        "SELECT id FROM inbox
          WHERE status = 'new' AND (CRC32(cp_ident) % {$shards}) = {$shard}
          ORDER BY id LIMIT {$limit} FOR UPDATE SKIP LOCKED"
    )->fetchAll(PDO::FETCH_COLUMN);
    if ($ids) {
        $marks = implode(',', array_fill(0, count($ids), '?'));
        $writeConnection->ex("UPDATE inbox SET status = ? WHERE id IN ({$marks})", array_merge([$token], $ids));
    }
    $writeConnection->commit();
    if (!$ids) { return []; }
    return $writeConnection->q("SELECT * FROM inbox WHERE status = ?", [$token])->fetchAll();
}

function processInboxBatch($runOnce, $onlyScanId = null)
{
    $writeConnection = conn();
    $readConnection  = conn();
    $keepRunning     = true;
    $processed_count  = 0;

    try {
        while ($keepRunning) {
            if ($runOnce) { $keepRunning = false; }

            $batch_limit = (int)(getenv('BATCH') ?: 4);
            $inboxRows = claimBatch($writeConnection, $batch_limit);
            if (!$inboxRows) { return true; }

            foreach ($inboxRows as $current_message_index => $inboxRecord) {
                $inboxRow         = $inboxRecord;
                $revisionDetailRows = [];
                $postedTariff = null;
                $overheatReported   = false;

                try {
                    $alreadyIngested = $readConnection->q("SELECT id FROM meter_events WHERE inbox_id = ?", [$inboxRow['id']])->fetch();
                    if ($alreadyIngested) {
                        try {
                            $writeConnection->begin();
                            $writeConnection->ex("UPDATE inbox SET status = 'done' WHERE id = ?", [$inboxRow['id']]);
                            $writeConnection->commit();
                            $processed_count++;
                        } catch (Exception $acknowledgeException) {
                            say('skip ' . $acknowledgeException->getMessage());
                        }
                        continue;
                    }

                    $writeConnection->begin();

                    if ($inboxRow['src'] != 'gw') {
                        // stamp the last contact on the unit that called
                        $writeConnection->begin();
                        $writeConnection->ex("UPDATE charge_points SET last_seen_at = ? WHERE cp_ident = ?", [$inboxRow['received_at'], $inboxRow['cp_ident']]);
                        $writeConnection->commit();
                    }

                    $decodedMessage = @json_decode($inboxRow['body'], true);
                    if (!is_array($decodedMessage) || !isset($decodedMessage['1'])) {
                        $writeConnection->ex("UPDATE inbox SET status = 'bad' WHERE id = ?", [$inboxRow['id']]);
                        $writeConnection->commit();
                        continue;
                    }

                    $decodedMessage['received_at'] = $inboxRow['received_at'];

                    $messageMetadata      = $decodedMessage['m'] ?? [];
                    $reportedFirmware  = $messageMetadata['firmware'] ?? null;
                    $connectorDescriptor    = $messageMetadata['zd'] ?? null;
                    $latitude          = $messageMetadata['la'] ?? null;
                    $longitude         = $messageMetadata['lo'] ?? null;
                    $settlementRequested    = $messageMetadata['sw'] ?? 0;
                    if (isset($decodedMessage['m'])) { unset($decodedMessage['m']); }

                    if (!empty($decodedMessage['la'])) {
                        $event_timestamp = new DateTime($decodedMessage['la']);
                        $future_boundary    = new DateTime(date('Y-m-d H:i:s', time()));
                        date_add($future_boundary, date_interval_create_from_date_string('2 days'));
                        if ($event_timestamp > $future_boundary) {
                            $writeConnection->ex("UPDATE inbox SET status = 'bad' WHERE id = ?", [$inboxRow['id']]);
                            $writeConnection->commit();
                            continue;
                        }
                    }

                    $chargePoint = $readConnection->q("SELECT * FROM charge_points WHERE cp_ident = ?", [$decodedMessage['1']])->fetch();
                    if (!$chargePoint) {
                        $writeConnection->ex("UPDATE inbox SET status = 'bad' WHERE id = ?", [$inboxRow['id']]);
                        $writeConnection->commit();
                        continue;
                    }

                    $siteTimezone   = $chargePoint['tz'] ?: 'America/Chicago';
                    $settlementRequested = ($chargePoint['settled'] == 1) ? 1 : $settlementRequested;

                    if (isset($decodedMessage['rd'])) {
                        $reported_hour = $decodedMessage['rh'] ?? 0;
                        $decodedMessage['dt'] = "{$decodedMessage['rd']} {$reported_hour}:00:00";
                    } else if (isset($decodedMessage['la'])) {
                        $decodedMessage['dt'] = $decodedMessage['la'];
                    }

                    $utcEventAt = null;
                    if (isset($decodedMessage['dt'])) {
                        $utcEventAt = new DateTime($decodedMessage['dt'], new DateTimeZone($siteTimezone));
                        $utcEventAt->setTimezone(new DateTimeZone('UTC'));
                        $utcEventAt = $utcEventAt->format('Y-m-d H:i:s');
                    }

                    $reportedModel = null;
                    if (isset($decodedMessage['md'])) { $reportedModel = $decodedMessage['md']; unset($decodedMessage['md']); } else { unset($reportedModel); }
                    $reportedTariff = null;
                    if (isset($decodedMessage['rt'])) { $reportedTariff = $decodedMessage['rt']; unset($decodedMessage['rt']); }

                    $revisionInsertSql = "INSERT INTO revisions (who, tname, target_id, op, at_) VALUES (0, 'ChargePoint', :t, 'U', NOW())";

                    if ($decodedMessage['msg_type'] == 9 || $decodedMessage['msg_type'] == 11 || $decodedMessage['msg_type'] == 14) {
                        $writeConnection->ex($revisionInsertSql, ['t' => $chargePoint['cp_ident']]);
                        $connectionRevisionId = $writeConnection->lastId();
                        $writeConnection->ex("UPDATE charge_points SET link_state = ?, flags = flags | 4 WHERE id = ?",
                            [$decodedMessage['msg_type'] == 14 ? 0 : 1, $chargePoint['id']]);
                        $writeConnection->ex("INSERT INTO revision_details (rev_id, col, before_, after_) VALUES (?,?,?,?)",
                            [$connectionRevisionId, 'link_state', $chargePoint['link_state'], $decodedMessage['msg_type'] == 14 ? 0 : 1]);
                        if (isset($decodedMessage['nl'])) { unset($decodedMessage['nl']); }
                    } else if ($decodedMessage['msg_type'] == 3 && $decodedMessage['la'] === null) {
                        $site_local_time = (new DateTime('now', new DateTimeZone($siteTimezone)))->format('Y-m-d H:i:s');
                        $writeConnection->ex("UPDATE inbox SET received_at = ? WHERE id = ?", [$site_local_time, $inboxRow['id']]);
                    } else if ($decodedMessage['msg_type'] == 17) {
                        $overheatReported = true;
                    }

                    if (isset($decodedMessage['nt']) && strpos($decodedMessage['nt'], 'OVERHEAT') !== false) { $overheatReported = true; }
                    if (isset($decodedMessage['nt'])
                        && (strpos($decodedMessage['nt'], 'GROUND FAULT') || strpos($decodedMessage['nt'], 'CONNECTOR LOCK FAULT'))
                        && $overheatReported !== true) {
                        if ($chargePoint['tariff'] === null) {
                            $revisionDetailRows[] = [null, 'fault_note', null, $decodedMessage['nt']];
                        } else {
                            $postedTariff = fetchPostedTariff($latitude ?? 0, $longitude ?? 0);
                            if ($postedTariff > 5 || $postedTariff === null) {
                                $revisionDetailRows[] = [null, 'fault_note', null, $decodedMessage['nt']];
                            }
                        }
                    }

                    if (isset($decodedMessage['fl']) && $decodedMessage['fl'] == '1'
                        && strtoupper((string)$chargePoint['fault_note']) === 'OK'
                        && empty($decodedMessage['nt'])
                        && ($decodedMessage['sv'] < 6 || $decodedMessage['hv'] < 3)) {
                        $decodedMessage['nt'] = '';
                        $diagnostic_blob = $decodedMessage['dbg'] ?? '';
                        if (preg_match('/;A1;/', $diagnostic_blob)) { $decodedMessage['nt'] .= 'GROUND FAULT. '; }
                        if (preg_match('/;B1;/', $diagnostic_blob)) { $decodedMessage['nt'] .= 'CONNECTOR LOCK FAULT. '; }
                        if (preg_match('/;C1;/', $diagnostic_blob)) { $decodedMessage['nt'] .= 'UNDER VOLTAGE. '; }
                        rtrim($decodedMessage['nt'], ' ');
                    }

                    $connectorRows                = [];
                    $syntheticInboxIdsByConnector = [];
                    if ($chargePoint['model_code'] == 7) {
                        if (!$connectorDescriptor) {
                            $connectorRows = $readConnection->q("SELECT * FROM connectors WHERE cp_id = ? AND retired = 0", [$chargePoint['id']])->fetchAll();
                            if (!empty($connectorRows)) {
                                if (isset($decodedMessage['as'])) {
                                    $connectorMeterValues = [];
                                    $meterReadingParts = explode(':', $decodedMessage['as']);
                                    foreach ($connectorRows as $connectorRow) {
                                        if (isset($meterReadingParts[$connectorRow['connector_no'] - 1])) {
                                            $connectorMeterValues[$connectorRow['connector_ident']][] = $meterReadingParts[$connectorRow['connector_no'] - 1];
                                        }
                                    }
                                }
                                $emittedConnectorIdents = [];
                                foreach ($connectorRows as $connectorRow) {
                                    $connectorMessage = $decodedMessage;
                                    if (!in_array($connectorRow['connector_ident'], $emittedConnectorIdents)) {
                                        $body_fields = explode(',', $inboxRow['body']);
                                        foreach ($body_fields as $body_field_index => $body_field) {
                                            if (strpos($body_field, '"1"') !== false) {
                                                $body_fields[$body_field_index] = '"1":"' . $connectorRow['connector_ident'] . '"';
                                                break;
                                            }
                                        }
                                        $writeConnection->ex("INSERT INTO inbox (src, status, cp_ident, body, body_hash, received_at) VALUES (?, 'done', ?, ?, ?, ?)",
                                            [$inboxRow['src'], $connectorRow['connector_ident'], implode(',', $body_fields), $inboxRow['body_hash'], $inboxRow['received_at']]);
                                        $synthetic_inbox_id = $writeConnection->lastId();
                                        $connectorMessage['1'] = $connectorRow['connector_ident'];
                                        if (isset($connectorMeterValues[$connectorRow['connector_ident']][0])) {
                                            $connectorMessage['wh'] = $connectorMeterValues[$connectorRow['connector_ident']][0];
                                        }
                                        $connector_cp_row = $readConnection->q("SELECT id FROM charge_points WHERE cp_ident = ?", [$connectorRow['connector_ident']])->fetch();
                                        if ($connector_cp_row) {
                                            $writeConnection->ex("INSERT INTO meter_events (inbox_id, cp_id, msg_type, wh, raw, local_event_at, utc_event_at, flags) VALUES (?,?,?,?,?,?,?,?)",
                                                [$synthetic_inbox_id, $connector_cp_row['id'], $connectorMessage['msg_type'], $connectorMessage['wh'] ?? 0,
                                                 substr($inboxRow['body'], 0, 250), $connectorMessage['la'] ?? null, $utcEventAt, (int)($decodedMessage['fl'] ?? 0)]);
                                        }
                                        $emittedConnectorIdents[] = $connectorRow['connector_ident'];
                                        $syntheticInboxIdsByConnector[$connectorRow['connector_ident']] = $synthetic_inbox_id;
                                    }
                                    $connectorMessage['1'] = $connectorRow['connector_ident'];
                                }
                            }
                        }
                    } else {
                        $writeConnection->ex("INSERT INTO meter_events (inbox_id, cp_id, msg_type, wh, raw, local_event_at, utc_event_at, rollup_date, rollup_hour, fault_note, flags) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                            [$inboxRow['id'], $chargePoint['id'], $decodedMessage['msg_type'], $decodedMessage['wh'] ?? 0,
                             substr($inboxRow['body'], 0, 250), $decodedMessage['la'] ?? null, $utcEventAt,
                             $decodedMessage['rd'] ?? null, $decodedMessage['rh'] ?? null, $decodedMessage['nt'] ?? null, (int)($decodedMessage['fl'] ?? 0)]);
                    }

                    $insertedEventId = $writeConnection->lastId();

                    $writeConnection->ex("UPDATE inbox SET status = 'done' WHERE id = ?", [$inboxRow['id']]);

                    $chargePointUpdates = [];
                    if (isset($decodedMessage['wh'])) { $chargePointUpdates['wh'] = $decodedMessage['wh']; }
                    if (isset($decodedMessage['la']))  { $chargePointUpdates['local_event_at'] = $decodedMessage['la']; }
                    if (isset($reportedTariff))       { $chargePointUpdates['tariff'] = $reportedTariff; }
                    if (isset($reportedModel) && $reportedModel !== null) { $chargePointUpdates['model_code'] = $reportedModel; }
                    if ($reportedFirmware !== null && $reportedFirmware != $chargePoint['firmware']) { $chargePointUpdates['firmware'] = $reportedFirmware; }
                    if (isset($decodedMessage['nt'])) { $chargePointUpdates['fault_note'] = $decodedMessage['nt']; }
                    $chargePointUpdates['last_event_id'] = $insertedEventId;
                    $chargePointUpdates['last_seen_at']    = $inboxRow['received_at'];

                    $revisionId = false;

                    if ($utcEventAt && $utcEventAt >= $chargePoint['rollup_at']) {
                        if (!$revisionId) {
                            $revisionInsertSql = "INSERT INTO revisions (who, tname, target_id, op, at_) VALUES (0, 'ChargePoint', :t, 'U', NOW())";
                            $writeConnection->ex($revisionInsertSql, ['t' => $chargePoint['cp_ident']]);
                            $revisionId = $writeConnection->lastId();
                        }
                        if (array_key_exists('wh', $chargePointUpdates)) {
                            $rollup_lookup = $readConnection->q("SELECT rollup_date FROM meter_events WHERE id = ?", [$chargePoint['rollup_event_id']]);
                            if ($rollup_lookup === false) {
                                $existing_rollup_row['rollup_date'] = null;
                            } else {
                                $existing_rollup_row = $rollup_lookup->fetch();
                            }
                        }
                        if (array_key_exists('local_event_at', $chargePointUpdates) && $chargePointUpdates['local_event_at'] != $chargePoint['local_event_at']) {
                            if (empty($existing_rollup_row['rollup_date']) || ($decodedMessage['rd'] ?? '') >= $existing_rollup_row['rollup_date']) {
                                $revisionDetailRows[] = [$revisionId, 'rollup_at', $chargePoint['rollup_at'], $utcEventAt];
                                $chargePointUpdates['rollup_at'] = $utcEventAt;
                            }
                        }
                    } else {
                        if (isset($chargePointUpdates['rollup_at'])) { unset($chargePointUpdates['rollup_at']); }
                    }

                    if (!empty($chargePointUpdates) || $inboxRow['received_at'] > $chargePoint['last_seen_at']) {
                        if (!$revisionId) {
                            $revisionInsertSql = "INSERT INTO revisions (who, tname, target_id, op, at_) VALUES (0, 'ChargePoint', :t, 'U', NOW())";
                            $writeConnection->ex($revisionInsertSql, ['t' => $chargePoint['cp_ident']]);
                            $revisionId = $writeConnection->lastId();
                        }
                        if ($chargePoint['model_code'] == 7 && !empty($connectorRows)) {
                            $auditedConnectorIdents = [];
                            $connectorRevisionIds  = [];
                            foreach ($connectorRows as $connectorRow) {
                                if (in_array($connectorRow['connector_ident'], $auditedConnectorIdents)) { continue; }
                                if ($connectorRow['connector_ident'] == $chargePoint['cp_ident']) {
                                    $connectorRevisionIds[$connectorRow['connector_ident']] = $revisionId;
                                    continue;
                                }
                                $writeConnection->ex($revisionInsertSql, ['t' => $connectorRow['connector_ident']]);
                                $connectorRevisionIds[$connectorRow['connector_ident']] = $writeConnection->lastId();
                                $auditedConnectorIdents[] = $connectorRow['connector_ident'];
                            }
                        }
                    }

                    if (array_key_exists('fault_note', $chargePointUpdates) && $chargePointUpdates['fault_note'] != $chargePoint['fault_note']
                        && ($postedTariff === null || ($postedTariff < 5 && $overheatReported === true)
                            || $postedTariff >= 5 || $overheatReported === true)
                        && ($decodedMessage['la'] ?? '') > $chargePoint['alerted_at']) {
                        $revisionDetailRows[] = [$revisionId, 'fault_note', $chargePoint['fault_note'], $chargePointUpdates['fault_note']];
                        $chargePointUpdates['alerted_at'] = $decodedMessage['la'];
                        unset($chargePointUpdates['fault_note']);
                    }
                    if ($settlementRequested == 1 && $chargePoint['settled'] != 1) { $chargePointUpdates['settled'] = 1; }
                    if ($inboxRow['src'] == 'gw' && $chargePoint['via_gateway'] == 0) { $chargePointUpdates['via_gateway'] = 1; }
                    if (isset($decodedMessage['connector_count']) && $decodedMessage['connector_count'] != $chargePoint['connector_count']) { $chargePointUpdates['connector_count'] = $decodedMessage['connector_count']; }
                    if (isset($reportedFirmware)) {
                        $firmware_major_version = ltrim(explode('-', (string)$reportedFirmware)[0], 'v');
                        if ($firmware_major_version != $chargePoint['firmware']) { $chargePointUpdates['firmware'] = $firmware_major_version; }
                    }

                    $perConnectorValues = [];
                    foreach ($chargePointUpdates as $fieldName => $newFieldValue) {
                        if ($chargePoint['model_code'] == 7 && in_array($fieldName, PER_CONNECTOR_FIELDS)) {
                            $perConnectorValues[$fieldName] = $chargePointUpdates[$fieldName];
                            $revisionDetailRows[] = [$revisionId, $fieldName, $chargePoint[$fieldName] ?? null, $newFieldValue];
                        } else if ($chargePoint['model_code'] == 7 && in_array($fieldName, PER_POINT_FIELDS)) {
                            $revisionDetailRows[] = [$revisionId, $fieldName, $chargePoint[$fieldName] ?? null, $newFieldValue];
                        } else if (!array_key_exists($fieldName, $chargePoint) || $newFieldValue != $chargePoint[$fieldName]) {
                            $revisionDetailRows[] = [$revisionId, $fieldName, $chargePoint[$fieldName] ?? null, $newFieldValue];
                        }
                    }

                    if ($chargePoint['model_code'] == 7 && $decodedMessage['msg_type'] != 3) {
                        if (!empty($connectorRows)) {
                            $reconciledConnectorIdents = [];
                            foreach ($connectorRows as $connectorRow) {
                                if (!in_array($connectorRow['connector_ident'], $reconciledConnectorIdents) && isset($connectorRevisionIds)) {
                                    $connectorRevisionId = $connectorRevisionIds[$connectorRow['connector_ident']] ?? null;
                                    $connectorDetailRows = $revisionDetailRows;
                                    if (empty($connectorDetailRows)) { continue; }
                                    foreach ($connectorDetailRows as $detailIndex => $detailRow) {
                                        $connectorDetailRows[$detailIndex][0] = $connectorRevisionId;
                                        if (in_array($detailRow[1], PER_CONNECTOR_FIELDS) && isset($perConnectorValues[$detailRow[1]])) {
                                            $matchedConnectorValues = [];
                                            $splitConnectorValues   = explode(':', (string)$perConnectorValues[$detailRow[1]]);
                                            foreach ($splitConnectorValues as $connectorIndex => $connectorValue) {
                                                foreach ($connectorRows as $candidateConnectorRow) {
                                                    if ($candidateConnectorRow['connector_ident'] == $connectorRow['connector_ident']
                                                        && $candidateConnectorRow['connector_no'] == $connectorIndex + 1) {
                                                        $matchedConnectorValues[] = $connectorValue;
                                                    }
                                                }
                                            }
                                            $connectorDetailRows[$detailIndex] = [$connectorRevisionId, $detailRow[1], 0, implode(':', $matchedConnectorValues)];
                                        }
                                    }
                                    foreach ($connectorDetailRows as $detailRow) {
                                        if ($detailRow[0] === null) { continue; }
                                        $writeConnection->ex("INSERT INTO revision_details (rev_id, col, before_, after_) VALUES (?,?,?,?)",
                                            [$detailRow[0], $detailRow[1], $detailRow[2], $detailRow[3]]);
                                    }
                                    $reconciledConnectorIdents[] = $connectorRow['connector_ident'];
                                }
                            }
                        }
                    } else {
                        $updateAssignments = [];
                        $updateBindings    = [];
                        foreach ($chargePointUpdates as $fieldName => $fieldValue) {
                            if (!array_key_exists($fieldName, $chargePoint)) { continue; }
                            $updateAssignments[] = "`{$fieldName}` = ?";
                            $updateBindings[]    = $fieldValue;
                        }
                        if ($updateAssignments) {
                            $updateBindings[] = $chargePoint['id'];
                            $writeConnection->ex("UPDATE charge_points SET " . implode(', ', $updateAssignments) . " WHERE id = ?", $updateBindings);
                        }
                        foreach ($revisionDetailRows as $detailRow) {
                            if ($detailRow[0] === null) { continue; }
                            $writeConnection->ex("INSERT INTO revision_details (rev_id, col, before_, after_) VALUES (?,?,?,?)",
                                [$detailRow[0], $detailRow[1], $detailRow[2], $detailRow[3]]);
                        }
                    }

                    $writeConnection->commit();
                    $processed_count++;
                } catch (Exception $scanException) {
                    $exceptionMessage = $scanException->getMessage();
                    $lockCycleMarker  = strpos($exceptionMessage, 'Deadlock');
                    say('ERR ' . $exceptionMessage);
                    if ($writeConnection->inTx() == true) {
                        $writeConnection->rollBack();
                        if ($lockCycleMarker) {
                            say('DEADLOCK caught, not crashing');
                            return true;
                        }
                    }
                    return false;
                }
            }
        }
    } catch (Exception $batchException) {
        $exceptionMessage = $batchException->getMessage();
        $lockCycleMarker  = strpos($exceptionMessage, 'Deadlock');
        say('FATAL ' . $exceptionMessage);
        if ($writeConnection->inTx() == true) { $writeConnection->rollBack(); }
        if ($lockCycleMarker && isset($inboxRow)) { return true; }
        return false;
    }
    return true;
}
