<?php
/**
 * Ingest worker daemon. Claims a batch from the inbox, processes it, sleeps
 * a short interval, repeats.
 *
 * Runs one process per CPU core allotted to the deployment; there is no
 * coordination between instances beyond what the database enforces.
 */

require_once __DIR__ . '/../src/ingest.php';

$pollIntervalUs = (int)(getenv('POLL_INTERVAL_US') ?: 250000);

while (true) {
    processInboxBatch(true);
    usleep($pollIntervalUs);
}
