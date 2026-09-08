# Known Issues

Defects and surprising behaviors found while documenting this service. Each entry states how it was established: **Reproduced** means it was observed on a running stack; **Source-read** means it was identified in code but not triggered.

Nothing here has been fixed. This document is a map of the traps, not a changelog.

| # | Issue | Severity | Evidence |
|---|---|---|---|
| [1](#1-a-multi-connector-frame-can-be-marked-done-having-written-no-readings) | Multi-connector frame marked `done` with zero readings | **Data loss** | Reproduced |
| [2](#2-multi-connector-parent-rows-are-never-updated-except-on-msg_type-3) | Parent row never updated except on `msg_type 3` | **Data loss** | Reproduced |
| [3](#3-last_event_id-can-be-set-to-an-inbox-id) | `last_event_id` set to an `inbox` id | **Corruption** | Reproduced |
| [4](#4-the-mzd-metadata-key-silently-suppresses-all-event-writing) | `m.zd` suppresses all event writing | **Data loss** | Reproduced |
| [5](#5-stranded-frames-are-unrecoverable-without-manual-sql) | Stranded frames need manual SQL | **Data loss** | Source-read |
| [6](#6-synthetic-frames-reuse-the-parents-body_hash) | Synthetic frames reuse `body_hash` | Medium | Reproduced |
| [7](#7-rejected-frames-still-update-last_seen_at) | Rejected frames still bump `last_seen_at` | Medium | Reproduced |
| [8](#8-rollup_at-never-advances-for-rdrh-style-frames) | `rollup_at` never advances for `rd`/`rh` frames | Medium | Reproduced |
| [9](#9-audit-details-queued-before-their-header-are-discarded--no-observable-effect) | Audit details queued before their header are discarded — **no observable effect** | Low | Reproduced |
| [10](#10-strpos-truthiness-bug-in-fault-detection) | `strpos()` truthiness bug in fault detection | Medium | Source-read |
| [11](#11-minor-defects-and-dead-code) | Minor defects and dead code | Low | Source-read |
| [12](#12-repository-hygiene) | Repository hygiene | Low | Reproduced |
| [13](#13-every-rollup-advance-writes-two-identical-audit-rows) | Every rollup advance writes two identical audit rows | Medium | Reproduced |
| [14](#14-one-malformed-frame-strands-the-rest-of-its-batch) | One malformed frame strands the rest of its batch | **Data loss** | Reproduced |
| [15](#15-an-empty-as-field-strands-multi-connector-frames) | An empty `as` field strands multi-connector frames | **Data loss** | Reproduced |

---

## 1. A multi-connector frame can be marked `done` having written no readings

**Severity: data loss.** **Reproduced.**

`src/ingest.php:225` looks up a `charge_points` row whose `cp_ident` equals the connector's `connector_ident`:

```php
$connector_cp_row = $readConnection->q("SELECT id FROM charge_points WHERE cp_ident = ?", [$connectorRow['connector_ident']])->fetch();
if ($connector_cp_row) {
    $writeConnection->ex("INSERT INTO meter_events ...");
}
```

When that row does not exist the insert is skipped with no `else` and no log line. Because the `model_code == 7` branch at `:194` *replaces* the simple single-row insert at `:239`, the frame produces **no `meter_events` rows at all** — and is still marked `done`.

The shipped seed data triggers this: `connectors` contains `CP-0002-1`, `CP-0002-2`, `CP-0002-3`, but `charge_points` has no rows with those idents.

### Reproduction

```bash
docker compose up -d --build && sleep 20
docker compose exec -T db mysql -u ingest_svc -pingest_svc evse -e "
  SET @b='{\"1\":\"CP-0002\",\"msg_type\":1,\"wh\":900,\"as\":\"100:200:300\",\"la\":\"2026-09-07 12:00:00\"}';
  INSERT INTO inbox (src,status,cp_ident,body,body_hash,received_at)
  VALUES ('cp','new','CP-0002',@b,SHA1(@b),NOW());"
sleep 3
docker compose exec -T db mysql -u ingest_svc -pingest_svc evse -e "
  SELECT id,status,cp_ident FROM inbox ORDER BY id;
  SELECT COUNT(*) AS meter_events FROM meter_events;"
```

### Observed

```
id  status  cp_ident
1   done    CP-0002
2   done    CP-0002-1
3   done    CP-0002-2
4   done    CP-0002-3

meter_events
0
```

Four `inbox` rows all `done`, zero readings, and the worker log was empty.

### Implication

Any change to the multi-connector path must be validated against `CP-0002`. Testing with `CP-0001` alone exercises a completely different code path and will show a green result while this defect is live.

---

## 2. Multi-connector parent rows are never updated except on `msg_type 3`

**Severity: data loss.** **Reproduced.**

There are two independent branches on `model_code == 7`, and they carry different secondary conditions:

- `src/ingest.php:194` — `if ($chargePoint['model_code'] == 7)` — chooses fan-out over the simple insert.
- `src/ingest.php:335` — `if ($chargePoint['model_code'] == 7 && $decodedMessage['msg_type'] != 3)` — chooses per-connector auditing over `UPDATE charge_points`.

The `msg_type != 3` qualifier on the second branch means a multi-connector unit's parent row is only ever written by heartbeat frames. Every ordinary telemetry frame is audited into `revision_details` and then discarded.

### Observed

A frame carrying `wh: 777` for `CP-0002` with `msg_type: 1` left `charge_points` row 2 at its previous `wh = 900`. The `revision_details` table recorded the intended change; the `charge_points` table never received it.

### Implication

`charge_points.wh`, `fault_note`, `firmware`, and `tariff` are effectively frozen for all `model_code = 7` units unless heartbeats happen to carry the values. The audit trail is more complete than the table it audits — reporting that reads `charge_points` for multi-connector units is reading stale data.

---

## 3. `last_event_id` can be set to an `inbox` id

**Severity: corruption.** **Reproduced.**

`src/ingest.php:245` captures the event id immediately after the insert branch:

```php
$insertedEventId = $writeConnection->lastId();
```

On the simple path the preceding statement is the `meter_events` insert, so this is correct. On the fan-out path the last statement executed is either a per-connector `meter_events` insert **or**, when issue #1 applies, the synthetic `inbox` insert at `:218`. `lastInsertId()` is per-connection, not per-table, so it returns whichever id came last.

That value flows into `$chargePointUpdates['last_event_id']` at `:256` and is persisted whenever branch 2 takes the `else` path — which for a multi-connector unit means `msg_type == 3`.

### Reproduction

Run against a **fresh** stack — `docker compose down -v && docker compose up -d --build && sleep 20` — so the ids below match. The defect reproduces on a warm stack too, but the id values shift.

```bash
docker compose exec -T db mysql -u ingest_svc -pingest_svc evse -e "
  SET @b='{\"1\":\"CP-0002\",\"msg_type\":3,\"wh\":900,\"as\":\"100:200:300\",\"la\":\"2026-09-07 12:00:00\"}';
  INSERT INTO inbox (src,status,cp_ident,body,body_hash,received_at)
  VALUES ('cp','new','CP-0002',@b,SHA1(@b),NOW());"
sleep 3
docker compose exec -T db mysql -u ingest_svc -pingest_svc evse -e "
  SELECT id,wh,last_event_id FROM charge_points WHERE id=2;
  SELECT COUNT(*) AS meter_events FROM meter_events;
  SELECT id,cp_ident FROM inbox ORDER BY id;"
```

### Observed

```
id  wh   last_event_id
2   900  4

meter_events
0

id  cp_ident
1   CP-0002
2   CP-0002-1
3   CP-0002-2
4   CP-0002-3
```

`last_event_id = 4` while `meter_events` is empty. Id 4 is the synthetic **`inbox`** row for `CP-0002-3`.

The specific number is whatever `lastInsertId()` happened to return, so it tracks the last synthetic `inbox` row rather than any fixed value. Running this after issue #1's reproduction on the same stack yields `last_event_id = 8` — same defect, different garbage.

Because `charge_points.last_event_id` has no foreign key (see [Data Model](data-model.md)), nothing detects this. Any downstream join of `charge_points.last_event_id` to `meter_events.id` will either return nothing or, once `meter_events` has grown past that id, silently return **the wrong reading**.

---

## 4. The `m.zd` metadata key silently suppresses all event writing

**Severity: data loss.** **Reproduced.**

The entire fan-out block is wrapped in `if (!$connectorDescriptor)` at `src/ingest.php:195`, where `$connectorDescriptor` is `m.zd`. When present, neither the fan-out nor the simple insert runs — the simple insert lives in the `else` of the *outer* `model_code` check, so it is unreachable for `model_code == 7`.

### Observed

A frame `{"1":"CP-0002","msg_type":1,"wh":777,"m":{"zd":"Z1"},"la":"..."}` produced exactly one `inbox` row (no synthetic children), zero new `meter_events` rows, and left `charge_points` row 2 untouched. Status: `done`.

There is no code path that handles a supplied connector descriptor. The key's only effect is suppression.

---

## 5. Stranded frames are unrecoverable without manual SQL

**Severity: data loss.** **Source-read**, mechanism confirmed by inspection of `src/ingest.php:389-401`.

When a frame throws anything other than a deadlock, the handler logs, rolls back, and returns. **That `return` exits `processInboxBatch` entirely, so every remaining frame in the same claimed batch is abandoned too — see [issue 14](#14-one-malformed-frame-strands-the-rest-of-its-batch) for the real blast radius.** The frame's `inbox.status` still holds the claiming worker's random token, which matches neither `new`, `done`, nor `bad`. Every claim query filters `status = 'new'`, so no worker will ever see it again. The same applies after a hard crash or container kill mid-batch — there is no signal handling in `bin/worker.php`.

Compounding it: `Cx::rollBack()` has no savepoints and always discards the entire outermost transaction, so a rollback also undoes the `status = 'bad'` write that the code may have already issued.

### Detection and recovery

```sql
SELECT id, cp_ident, status, received_at
FROM inbox WHERE status NOT IN ('new','done','bad');

UPDATE inbox SET status = 'new' WHERE status NOT IN ('new','done','bad');
```

Recovery is safe to run blind: the idempotency guard at `src/ingest.php:70` prevents duplicate `meter_events` rows on replay. Verified — resetting a processed frame to `new` returned it to `done` with no duplicate written.

There is no reaper, no lease expiry, no dead-letter table, and no alerting. Add the detection query above to monitoring if this service matters.

---

## 6. Synthetic frames reuse the parent's `body_hash`

**Severity: medium.** **Reproduced.**

`src/ingest.php:219` writes the parent's `body_hash` onto each synthetic row while writing a *modified* body. The body is patched by splitting on `,` and string-matching `"1"` (`:211-217`) rather than decoding and re-encoding the JSON.

### Observed

`inbox` ids 1–4 from issue #1 all carried `body_hash = 156644c24603150ee2b3868ce378058d370e6c58`, despite four different bodies.

Consequences: `body_hash` cannot be used for deduplication or integrity checking, and the string-surgery patch will corrupt any body where the literal `"1"` appears before the identifier field or where the JSON contains a `,` inside a string value.

---

## 7. Rejected frames still update `last_seen_at`

**Severity: medium.** **Reproduced.**

The liveness stamp at `src/ingest.php:85-90` runs *before* JSON decoding and validation, and is gated only on `src != 'gw'`. It is inside the frame's transaction, which the `bad` path commits.

### Observed

| Unit | `src` | Frame | `last_seen_at` before | after | Final status |
|---|---|---|---|---|---|
| `CP-0010` | `cp` | `{"garbage":true}` | `NULL` | `2026-09-07 21:47:57` | `bad` |
| `CP-0011` | `gw` | `{"garbage":true}` | `NULL` | `NULL` | `bad` |

Whether this is a defect depends on intent — "the radio works but the payload is malformed" is arguably still contact. But it is asymmetric: identical malformed frames produce different liveness outcomes depending on the transport. Anything alerting on `last_seen_at` will treat a unit emitting pure garbage over a direct connection as healthy.

---

## 8. `rollup_at` never advances for `rd`/`rh`-style frames

**Severity: medium.** **Reproduced.**

Frames may carry time either as `la` (a local timestamp) or as `rd` + `rh` (a rollup date and hour). Both feed `$decodedMessage['dt']` and therefore `utc_event_at`. But the `rollup_at` advancement at `src/ingest.php:275` is guarded on `array_key_exists('local_event_at', $chargePointUpdates)`, and `local_event_at` is only ever populated from `la` (`:251`).

So the rollup pointer only moves for `la`-style frames.

### Observed

A rollup frame `{"1":"CP-0003","msg_type":1,"wh":5000,"rd":"2026-09-06","rh":14}` produced a correct `meter_events` row — `rollup_date = 2026-09-06`, `rollup_hour = 14`, `utc_event_at = 2026-09-06 20:00:00` (Denver, UTC−6) — while `charge_points.rollup_at` stayed `NULL` and `local_event_at` stayed `NULL`.

---

## 9. Audit details queued before their header are discarded — no observable effect

**Severity: low (downgraded).** **Reproduced — and the original assessment was wrong.**

`$revisionDetailRows` is appended to throughout processing, but the `revisions` header is not created until `:286`. Entries queued earlier carry a `NULL` `rev_id` — notably the fault-note entries at `:171` and `:175`. Both flush loops skip them:

```php
if ($detailRow[0] === null) { continue; }
```

This document previously concluded that "a detected fault note can be silently dropped from the audit trail". **Characterization testing disproved that.** The dropped row is always shadowed by an equivalent row carrying a valid `rev_id`, written later in the same frame by one of two other paths:

- when the alert gate at `:307` passes, it appends its own `fault_note` detail at `:311`;
- when the gate fails, `fault_note` stays in `$chargePointUpdates` and the generic diff loop at `:330` appends it.

Exactly one of those two always fires, so the audit trail is complete. For `model_code == 7` the null-`rev_id` rows are rescued outright: `:344` assigns a connector revision id to every queued row before the flush.

### Observed

Four suite cases cover every branch — high tariff, low tariff, no lookup, and NULL tariff — and **all four end with exactly one `fault_note` row in `revision_details`**:

| Case | Tariff | HTTP call | `fault_note` details | Written by |
|---|---|---|---|---|
| `12-tariff-lookup-payload-recorded` | `PEAK`, value 9 | yes | 1 | alert gate (`:311`) |
| `13-tariff-low-value-persists-note-without-alerting` | `OFFPEAK`, value 3 | yes | 1 | generic diff loop (`:330`) |
| `14-tariff-not-called-when-note-starts-at-offset-zero` | `PEAK`, not called | no | 1 | alert gate (`:311`) |
| `15-null-tariff-skips-lookup-audit-still-complete` | `NULL` | no | 1 | alert gate (`:311`) |

The `continue` is therefore dead weight rather than an audit hole. It remains worth deleting during any cleanup of this function, but it is not a correctness defect and does not need coordinating with downstream consumers.

### What the alert gate actually controls

Establishing the above also corrected a second misreading. The gate at `:307-314` does **not** decide whether the fault note is audited. It decides two other things, and its sense is the opposite of what "suppression" suggests:

| Gate outcome | Condition | `charge_points.fault_note` | `charge_points.alerted_at` |
|---|---|---|---|
| **passes** | tariff unknown, or posted value ≥ 5, or overheat | **not persisted** (`:313` unsets it) | **set** from `la` |
| **fails** | posted value < 5 without overheat | **persisted** | left `NULL` |

So a high or unknown tariff raises an alert and deliberately withholds the note from the parent row, while a cheap tariff records the note and stays quiet. Cases 12 and 13 pin both directions.

---

## 10. `strpos()` truthiness bug in fault detection

**Severity: medium.** **Source-read.**

`src/ingest.php:168`:

```php
&& (strpos($decodedMessage['nt'], 'GROUND FAULT') || strpos($decodedMessage['nt'], 'CONNECTOR LOCK FAULT'))
```

`strpos()` returns `0` when the needle is at the start of the string, and `0` is falsy. A fault note reading exactly `"GROUND FAULT"` is therefore **not** detected. The check one line above at `:166` gets this right by using `!== false`.

This matters because `:186-188` synthesizes notes that begin with precisely these strings — `'GROUND FAULT. '` and `'CONNECTOR LOCK FAULT. '` — so synthesized notes are the ones most likely to be missed.

---

## 11. Minor defects and dead code

**Source-read.**

| Location | Issue |
|---|---|
| `src/ingest.php:189` | `rtrim($decodedMessage['nt'], ' ');` — return value discarded, so the trailing space is never trimmed. PHP strings are immutable; this is a no-op. |
| `src/ingest.php:270-276` | `$existing_rollup_row` is only assigned inside `if (array_key_exists('wh', ...))` but read unconditionally at `:276`. A frame without `wh` that reaches this branch reads an undefined variable. |
| `src/ingest.php:48` | `$onlyScanId` parameter is declared and never read. Passing it has no effect. |
| `src/ingest.php:53` | `$processed_count` is incremented and never read or returned. |
| `src/ingest.php:145` | `unset($reportedModel)` in the `else` branch, followed by `isset($reportedModel)` at `:253` — works, but obscures intent. |
| `src/ingest.php:153`, `:263`, `:287` | `$revisionInsertSql` assigned three times with identical SQL. |
| `src/db.php:35-47` | `Cx::ex()` and `Cx::q()` are byte-identical. The read/write distinction is convention only. |
| `src/ingest.php:12` | `fetchPostedTariff()` sleeps 120–210 ms and returns `null` when `LOOKUP_URL` is unset — **inside the open write transaction**, holding row locks for the duration. |
| `src/ingest.php:307-310` | The `fault_note` gate reads as a near-tautology: `($postedTariff === null \|\| ($postedTariff < 5 && $overheat) \|\| $postedTariff >= 5 \|\| $overheat)` is true for every value except a non-null value below 5 without overheat. That one exception is load-bearing — it inverts whether the note is persisted or an alert is raised. See [issue 9](#9-audit-details-queued-before-their-header-are-discarded--no-observable-effect). |
| `src/ingest.php:36-37` | `SHARD`/`SHARDS`/`LIMIT` are interpolated into the claim SQL rather than bound. Safe only because of the `(int)` casts — keep them if you touch this query. |

---

## 12. Repository hygiene

**Reproduced.**

| Issue | Detail |
|---|---|
| `bin/seed.php` does not exist | `README.md` documents `docker compose exec app php bin/seed.php 400` as the way to load sample frames. The file is absent, so the "Running locally" section cannot be followed as written. Use the manual `INSERT` recipes in this document or in [AGENTS.md](../AGENTS.md). |
| No version control | There is no `.git` directory. Edits are not revertible; be conservative with destructive changes. |
| Three undocumented env vars | `LOOKUP_URL`, `SHARD`, `SHARDS` are read by the code but absent from the README's configuration table. |
| `DB_USER` / `DB_PASS` have silent defaults | Both fall back to `ingest_svc`. The README implies they are required. A misconfigured deployment connects with default credentials instead of failing fast. |
| No `.dockerignore` | `COPY . .` bakes the entire working tree into the image, including `sql/` and anything else present at build time. |
| No `app` healthcheck | Compose cannot detect a hung worker loop; `restart: unless-stopped` only reacts to process exit. |

---

## 13. Every rollup advance writes two identical audit rows

**Severity: medium.** **Reproduced.**

When the rollup pointer advances, `rollup_at` is appended to `$revisionDetailRows` twice:

- explicitly at `:277`, immediately before `$chargePointUpdates['rollup_at']` is set at `:278`;
- again by the generic diff loop at `:330`, because `rollup_at` is now a key in `$chargePointUpdates` and its new value differs from the row read at the start of the frame.

Both carry the same `rev_id`, `col`, `before_`, and `after_`, and both are inserted. The audit trail therefore double-counts every rollup advance.

### Observed

Baseline `tests/characterization/baselines/01-simple-telemetry-single-connector.snapshot` — a single ordinary telemetry frame:

```
-- revision_details (id|rev_id|col|before_|after_)
1|1|rollup_at|NULL|2021-03-04 18:00:00
2|1|wh|0|1234
3|1|local_event_at|NULL|2021-03-04 12:00:00
4|1|last_event_id|NULL|1
5|1|last_seen_at|NULL|2021-03-04 10:00:00
6|1|rollup_at|NULL|2021-03-04 18:00:00
```

Rows 1 and 6 are byte-identical. Case 01 asserts this explicitly:
`SELECT COUNT(*) FROM revision_details WHERE col='rollup_at'` is `2`.

No other field duplicates, because `rollup_at` is the only one appended by hand before the generic loop runs. Any consumer counting audit rows, or replaying them to reconstruct history, sees the change twice.

---

## 14. One malformed frame strands the rest of its batch

**Severity: data loss.** **Reproduced** — found by the property suite, seed 1001.

This is [issue 5](#5-stranded-frames-are-unrecoverable-without-manual-sql) with a far larger blast radius than that entry describes. The per-frame `catch` at `:389` does not `continue` to the next frame — it **`return`s out of `processInboxBatch` from inside the `foreach`** at `:63`:

```php
} catch (Exception $scanException) {
    say('ERR ' . $exceptionMessage);
    if ($writeConnection->inTx() == true) {
        $writeConnection->rollBack();
        if ($lockCycleMarker) { say('DEADLOCK caught, not crashing'); return true; }
    }
    return false;                 // <-- abandons every remaining frame
}
```

Every frame later in the batch has already been stamped with the claim token by `claimBatch()`, so none of them is `new` and no worker ever revisits them. **One bad frame silently destroys up to `BATCH - 1` innocent frames.** With the production default `BATCH=4` that is three; the test suite's `BATCH=32` makes it thirty-one.

### Observed

Suite case `25-one-throw-strands-rest-of-batch`. Four frames, only the first defective (`"la":"banana"`, which throws in `new DateTime`):

```
id  status   cp_ident  body
1   <TOKEN>  CP-0011   {"1":"CP-0011","msg_type":9,"wh":1,"la":"banana"}
2   <TOKEN>  CP-0003   {"1":"CP-0003","msg_type":1,"wh":7,"la":"2021-01-01 00:00:00"}
3   <TOKEN>  CP-9999   {"1":"CP-9999","msg_type":1,"wh":2}
4   <TOKEN>  CP-0001   {"1":"CP-0001","msg_type":1,"wh":4242,"la":"2021-03-04 12:00:00"}
```

All four stranded; `meter_events` and `revisions` both empty. Frames 2 and 4 were perfectly valid and frame 3 would merely have been marked `bad`. A single `ERR` line is logged — for frame 1 only, so the three casualties are invisible in the log.

This also explains an artefact in unrelated fixture data on a sibling stack: two rows there share the claim token `c32aaa9df8bb`, one carrying `"la":"banana"` and the next an unknown identifier. The second was collateral. Independent corroboration from a party that never noticed it.

Recovery is the same manual `UPDATE` as issue 5, and is safe thanks to the replay guard. Detection should therefore alert on *count*, not just existence:

```sql
SELECT status, COUNT(*) FROM inbox
 WHERE status NOT IN ('new','done','bad') GROUP BY status;
```

Every row sharing one token was lost to the same throw.

---

## 15. An empty `as` field strands multi-connector frames

**Severity: data loss.** **Reproduced** — found by the property suite, seed 20260907.

On `model_code == 7`, `:200` splits the `as` field on `:`. An empty `as` yields `[""]`, so the connector with `connector_no = 1` receives the empty string as its meter value at `:203`, and `:228` then inserts that into `meter_events.wh`, an `INT` column. MySQL runs with `STRICT_TRANS_TABLES` by default and rejects it:

```
[PID] ERR SQLSTATE[HY000]: General error: 1366 Incorrect integer value: '' for column 'wh' at row 1
```

The exception strands the frame and, per issue 14, the rest of its batch.

### Masked by issue 1

This is only reachable when per-connector `charge_points` rows exist. With stock seed data the lookup at `:225` finds nothing and the insert is skipped, so [issue 1](#1-a-multi-connector-frame-can-be-marked-done-having-written-no-readings) hides this crash entirely.

**That matters for sequencing: fixing issue 1 will expose issue 15.** Suite case `26-empty-as-field-strands-multiconnector-frame` provisions the connector rows explicitly so the crash is pinned now rather than discovered later.

### Observed

```sql
-- after provisioning CP-0002-1..3 as charge_points rows
SELECT COUNT(*) FROM inbox WHERE status NOT IN ('new','done','bad');  -- 1
SELECT COUNT(*) FROM meter_events;                                    -- 0
```

Any non-numeric `as` segment does the same thing; `""` is simply the easiest to hit. Note `wh` at `:240` on the simple path is `$decodedMessage['wh'] ?? 0`, which does not have this problem — only the per-connector split does.

---

## Consistency with AGENTS.md

These documents were cross-checked against [`AGENTS.md`](../AGENTS.md) and against `src/` directly.

**No contradictions were found.** Every claim in `AGENTS.md` that overlaps this document was verified correct, including the two-branch `model_code == 7` split, the strand mechanism, the `body_hash` reuse, and the `strpos` bug.

Four findings here were **new** — discovered by executing the code rather than reading it — and have been added to `AGENTS.md` as verified gotchas:

- issue #3, `last_event_id` receiving an `inbox` id
- issue #4, `m.zd` suppressing all event writing
- issue #7, rejected frames bumping `last_seen_at`
- issue #8, `rollup_at` not advancing for `rd`/`rh` frames

One refinement was applied rather than a correction: `AGENTS.md` originally described issue #1 as per-connector inserts being "silently skipped". Execution showed the stronger result — **zero** `meter_events` rows for the frame, because the fan-out branch replaces the parent insert entirely. The wording was tightened to match the observed behavior.

### Corrections from the characterization suite

Building [`tests/characterization/`](../tests/characterization/) exercised these
behaviors end to end and forced two changes to this document. Both are cases
where reading the code produced the wrong conclusion.

1. **Issue 9 was overstated and is now downgraded.** It claimed a detected fault
   note "can be silently dropped from the audit trail". It cannot: the dropped
   row is always shadowed by an equivalent row written by the alert gate or by
   the generic diff loop. Four suite cases cover every branch and all four end
   with exactly one `fault_note` audit row. Severity went medium → low, and the
   issue is now labelled as having no observable effect. Establishing this also
   corrected a second misreading: the alert gate governs whether the note is
   *persisted* and whether `alerted_at` is *set*, not whether it is audited —
   and its sense is the opposite of "suppression".
2. **Issue 13 is new.** Every rollup advance writes two byte-identical
   `revision_details` rows. It appears in the very first baseline recorded and
   had been missed by three prior reading passes over the same function.

`AGENTS.md` gotcha 12 was rewritten to match correction 1, and gotcha 19 was
added for correction 2.

---

## Related documents

- [Domain Overview](domain-overview.md) — guarantees and non-guarantees
- [Architecture](architecture.md) — the two-branch control flow
- [Sequence Diagrams](sequence-diagrams.md) — failure paths over time
- [Data Model](data-model.md) — unenforced relationships these defects exploit
- [Integration Points](integration-points.md) — every external touchpoint and its blast radius
- [Stability Spec](stability-spec.md) — the invariants that must survive any change
