#!/usr/bin/env bash
# Manual end-to-end smoke test against the production compose stack.
#
# Brings the stack up, injects one telemetry frame exactly as the upstream
# intake layer would, and reports what the service did with it. This is the
# check that `bin/seed.php` was supposed to provide -- that script is referenced
# by README.md but does not exist in the repository.
#
# For a real regression gate use the automated suites (`make test`); this exists
# for eyeballing behavior and for verifying a fresh environment works at all.

set -o errexit
set -o nounset
set -o pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

CP_IDENT="${CP_IDENT:-CP-0001}"
WH="${WH:-1234}"
# Fixture time is deliberately in the past so the two-day future guard passes
# and the expected UTC value is stable. CP-0001 is America/Chicago; March is
# CST (UTC-6), so 12:00 local must land at 18:00 UTC.
LOCAL_AT="${LOCAL_AT:-2021-03-04 12:00:00}"

mysql_exec() {
  docker compose exec -T db mysql -u ingest_svc -pingest_svc evse "$@" 2>/dev/null
}

echo "==> bringing up the stack"
docker compose up -d --build >/dev/null 2>&1

echo "==> waiting for the database"
for _ in $(seq 1 60); do
  if docker compose exec -T db mysqladmin ping -h 127.0.0.1 \
       -uingest_svc -pingest_svc >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
if ! mysql_exec -N -B -e "SELECT 1" >/dev/null 2>&1; then
  echo "database never became ready" >&2
  exit 1
fi

echo "==> injecting one frame for ${CP_IDENT}"
BODY="{\"1\":\"${CP_IDENT}\",\"msg_type\":1,\"wh\":${WH},\"la\":\"${LOCAL_AT}\"}"
B64="$(printf '%s' "$BODY" | base64 -w0)"
mysql_exec -e "
  SET @b = CONVERT(FROM_BASE64('${B64}') USING utf8mb4);
  INSERT INTO inbox (src, status, cp_ident, body, body_hash, received_at)
  VALUES ('cp', 'new', '${CP_IDENT}', @b, SHA1(@b), NOW());"
echo "    body: ${BODY}"

echo "==> waiting for the worker to claim and process it"
for _ in $(seq 1 40); do
  PENDING="$(mysql_exec -N -B -e "SELECT COUNT(*) FROM inbox WHERE status='new'")"
  [ "$PENDING" = "0" ] && break
  sleep 0.5
done

echo
echo "--- inbox ---"
mysql_exec -e "SELECT id, src, status, cp_ident, received_at
                 FROM inbox ORDER BY id DESC LIMIT 5;"
echo "--- meter_events ---"
mysql_exec -e "SELECT id, inbox_id, cp_id, msg_type, wh, local_event_at, utc_event_at
                 FROM meter_events ORDER BY id DESC LIMIT 5;"
echo "--- audit trail ---"
mysql_exec -e "SELECT r.target_id, r.op, d.col, d.before_, d.after_
                 FROM revisions r JOIN revision_details d ON d.rev_id = r.id
                ORDER BY r.id DESC, d.id LIMIT 12;"
echo "--- worker log ---"
docker compose logs --no-log-prefix app 2>/dev/null | tail -5 || true

echo
STATUS="$(mysql_exec -N -B -e "SELECT status FROM inbox ORDER BY id DESC LIMIT 1")"
EVENTS="$(mysql_exec -N -B -e "SELECT COUNT(*) FROM meter_events WHERE cp_id=(SELECT id FROM charge_points WHERE cp_ident='${CP_IDENT}')")"
echo "==> frame status: ${STATUS}    readings for ${CP_IDENT}: ${EVENTS}"

if [ "$STATUS" != "done" ]; then
  echo "==> UNEXPECTED: frame did not reach 'done'." >&2
  echo "    A claim-token status means it was stranded -- see docs/known-issues.md #5 and #14." >&2
  exit 1
fi

echo "==> ok. Tear down with 'make down', or 'make nuke' to drop the data volume."
