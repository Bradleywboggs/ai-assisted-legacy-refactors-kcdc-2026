#!/usr/bin/env bash
# Characterization harness. Black-box helpers only: every observation of the
# service under test is made through its external edges -- SQL against the
# database, the recorded HTTP request log, container stdout, and container
# filesystem diffs. Nothing here reads or references application source.
#
# Requires: bash 4+, docker (with compose v2), jq (or jaq).

set -o errexit
set -o nounset
set -o pipefail

CH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "$CH_DIR/../.." && pwd)"
COMPOSE_FILE="$CH_DIR/docker-compose.test.yml"
PROJECT="${CH_PROJECT:-evse-charz}"
DB_NAME="evse"
ENV_FILE="$CH_DIR/.env.runtime"

# Datetimes within this many seconds of "now" are clock-derived and are masked
# as <CLOCK> in snapshots. The window must exceed the largest real-world UTC
# offset (+/-14h), because the service writes some columns in the charge point's
# *site-local* time -- inbox.received_at on a msg_type 3 frame, for one -- while
# this comparison runs in the database's own timezone. 15h covers every zone.
# Fixture timestamps live in 2021 and 2099, years outside the window, so they
# are never masked.
CLOCK_WINDOW_SECONDS=54000

# Tables observed in full, in a stable order.
CH_TABLES=(inbox meter_events revisions revision_details)

# ---------------------------------------------------------------- compose ----

ch_compose() {
  docker compose -p "$PROJECT" -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"
}

ch_write_env() {
  # $1..$n are KEY=VALUE strings; anything unset falls back to compose defaults.
  : >"$ENV_FILE"
  local kv
  for kv in "$@"; do printf '%s\n' "$kv" >>"$ENV_FILE"; done
}

ch_infra_up() {
  ch_write_env
  ch_compose up -d --build db tariff >/dev/null 2>&1
  ch_wait_db
}

ch_wait_db() {
  local i
  for i in $(seq 1 90); do
    if ch_compose exec -T db mysqladmin ping -h 127.0.0.1 -uingest_svc -pingest_svc \
        >/dev/null 2>&1; then
      # ping succeeds slightly before the seed script finishes; wait for seed data
      for i in $(seq 1 60); do
        if [[ "$(ch_sql_value "SELECT COUNT(*) FROM charge_points")" =~ ^[0-9]+$ ]]; then
          return 0
        fi
        sleep 1
      done
      return 0
    fi
    sleep 1
  done
  echo "harness: database never became ready" >&2
  return 1
}

ch_teardown() {
  ch_write_env
  ch_compose down -v --remove-orphans >/dev/null 2>&1 || true
  rm -f "$ENV_FILE"
}

# --------------------------------------------------------------------- sql ----

# Run SQL, discard output. Fails loudly.
ch_sql() {
  ch_compose exec -T db mysql -uingest_svc -pingest_svc --database "$DB_NAME" \
    -e "$1" 2>/dev/null
}

# Run SQL, return raw tab-separated rows with no header and no box drawing.
ch_sql_rows() {
  ch_compose exec -T db mysql -uingest_svc -pingest_svc --database "$DB_NAME" \
    -N -B -e "$1" 2>/dev/null
}

# Run SQL expected to yield exactly one scalar.
ch_sql_value() {
  ch_sql_rows "$1" | head -n 1
}

# ------------------------------------------------------------------- reset ----

# Reset to a pristine database by replaying the real schema file, then capture
# pristine copies so "which rows changed" can be computed without hardcoding
# the seed contents here.
ch_reset_db() {
  ch_compose exec -T db mysql -uingest_svc -pingest_svc --database "$DB_NAME" \
    <"$REPO_ROOT/sql/schema.sql" 2>/dev/null
  ch_sql "DROP TABLE IF EXISTS _pristine_charge_points;
          DROP TABLE IF EXISTS _pristine_connectors;
          CREATE TABLE _pristine_charge_points AS SELECT * FROM charge_points;
          CREATE TABLE _pristine_connectors    AS SELECT * FROM connectors;"
}

# --------------------------------------------------- column introspection ----

# Emit "name<TAB>data_type" for a table, in ordinal order. Discovered at runtime
# so the harness never hardcodes a schema it might drift from.
ch_columns() {
  ch_sql_rows "SELECT COLUMN_NAME, DATA_TYPE
                 FROM information_schema.columns
                WHERE table_schema='$DB_NAME' AND table_name='$1'
                ORDER BY ORDINAL_POSITION"
}

ch_column_names() {
  ch_columns "$1" | cut -f1 | paste -sd'|' -
}

# Build a CONCAT_WS select expression for a table, masking non-deterministic
# values so snapshots are byte-stable across runs:
#   * datetime/date/timestamp near "now"  -> <CLOCK>
#   * an in-flight inbox claim token      -> <TOKEN>
# $1 table, $2 optional alias prefix (e.g. "c.")
ch_projection() {
  local table="$1" alias="${2:-}" name type expr parts=()
  while IFS=$'\t' read -r name type; do
    [[ -z "$name" ]] && continue
    local ref="${alias}\`${name}\`"
    case "$type" in
      datetime | timestamp | date)
        expr="CASE WHEN $ref IS NULL THEN 'NULL'
                   WHEN ABS(TIMESTAMPDIFF(SECOND, $ref, NOW())) <= $CLOCK_WINDOW_SECONDS
                     THEN '<CLOCK>'
                   ELSE CAST($ref AS CHAR) END"
        ;;
      *)
        if [[ "$table" == "inbox" && "$name" == "status" ]]; then
          expr="CASE WHEN $ref REGEXP '^c[0-9a-f]{11}\$' THEN '<TOKEN>'
                     ELSE IFNULL(CAST($ref AS CHAR),'NULL') END"
        else
          expr="IFNULL(CAST($ref AS CHAR),'NULL')"
        fi
        ;;
    esac
    parts+=("$expr")
  done < <(ch_columns "$table")
  local IFS=','
  printf "CONCAT_WS('|',%s)" "${parts[*]}"
}

# Raw (unmasked) row-identity expression, used only to detect whether a row
# changed relative to its pristine copy.
ch_identity() {
  local table="$1" alias="${2:-}" name type parts=()
  while IFS=$'\t' read -r name type; do
    [[ -z "$name" ]] && continue
    parts+=("IFNULL(CAST(${alias}\`${name}\` AS CHAR),'~NULL~')")
  done < <(ch_columns "$table")
  local IFS=','
  printf "MD5(CONCAT_WS('~',%s))" "${parts[*]}"
}

# ---------------------------------------------------------------- observe ----

ch_dump_table() {
  local table="$1" proj
  proj="$(ch_projection "$table")"
  printf -- "-- %s (%s)\n" "$table" "$(ch_column_names "$table")"
  local rows
  rows="$(ch_sql_rows "SELECT $proj FROM \`$table\` ORDER BY id")"
  if [[ -z "$rows" ]]; then printf -- "   <no rows>\n"; else printf '%s\n' "$rows"; fi
}

# Report only rows that differ from the pristine seed, so a 22-row fleet does
# not drown the diff -- and so an unexpected write to an unrelated row shows up.
ch_dump_changed() {
  local table="$1" pristine="_pristine_$1" proj ident_c ident_p
  proj="$(ch_projection "$table" "c.")"
  ident_c="$(ch_identity "$table" "c.")"
  ident_p="$(ch_identity "$table" "p.")"
  printf -- "-- %s CHANGED vs seed (%s)\n" "$table" "$(ch_column_names "$table")"
  local rows
  rows="$(ch_sql_rows "
    SELECT $proj FROM \`$table\` c
      JOIN \`$pristine\` p ON p.id = c.id
     WHERE $ident_c <> $ident_p
     ORDER BY c.id")"
  local added removed
  added="$(ch_sql_value "SELECT COUNT(*) FROM \`$table\` c
             WHERE NOT EXISTS (SELECT 1 FROM \`$pristine\` p WHERE p.id=c.id)")"
  removed="$(ch_sql_value "SELECT COUNT(*) FROM \`$pristine\` p
             WHERE NOT EXISTS (SELECT 1 FROM \`$table\` c WHERE c.id=p.id)")"
  if [[ -z "$rows" ]]; then
    printf -- "   <none>\n"
  else
    printf '%s\n' "$rows"
  fi
  printf -- "   rows_inserted=%s rows_deleted=%s\n" "$added" "$removed"
}

ch_mock_reset() {
  ch_compose exec -T tariff sh -c ': > /tmp/requests.log' 2>/dev/null || true
}

ch_mock_requests() {
  ch_compose exec -T tariff cat /tmp/requests.log 2>/dev/null || true
}

ch_mock_request_count() {
  ch_mock_requests | grep -c . || true
}

ch_app_stdout() {
  # Normalize the pid prefix the service emits: "[1] msg" -> "[PID] msg".
  ch_compose logs --no-log-prefix app 2>/dev/null | sed -E 's/^\[[0-9]+\]/[PID]/'
}

ch_app_state() {
  ch_compose ps app --format '{{.State}}' 2>/dev/null | head -n 1
}

# Files the service under test wrote inside its own container. The documented
# expectation is that it writes nothing but stdout, so this should stay empty.
ch_app_file_writes() {
  local cid
  cid="$(ch_compose ps -q app 2>/dev/null | head -n 1)"
  [[ -z "$cid" ]] && return 0
  docker diff "$cid" 2>/dev/null | grep -vE '^C /(tmp|run|var/run|proc|sys)$' || true
}

# ------------------------------------------------------------------ drive ----

ch_app_start() {
  ch_compose up -d app >/dev/null 2>&1
}

ch_app_stop() {
  ch_compose stop -t 2 app >/dev/null 2>&1 || true
  ch_compose rm -f app >/dev/null 2>&1 || true
}

# Insert one frame exactly as the upstream intake layer would.
# $1 src, $2 cp_ident, $3 body (JSON text), $4 received_at
ch_insert_frame() {
  local src="$1" cp="$2" body="$3" received="$4" b64
  # Base64 to keep quoting and unicode intact through shell + SQL.
  b64="$(printf '%s' "$body" | base64 -w0)"
  ch_sql "SET @b = CONVERT(FROM_BASE64('$b64') USING utf8mb4);
          INSERT INTO inbox (src, status, cp_ident, body, body_hash, received_at)
          VALUES ('$src', 'new', '$cp', @b, SHA1(@b), '$received');"
}

# Wait until the batch has quiesced: nothing claimable remains AND the full
# observable state has stopped changing. Stranded rows never reach a terminal
# status, so stability -- not terminality -- is the stop condition.
ch_wait_quiesce() {
  local timeout="${1:-45}" stable_needed=3 stable=0 last="" now_hash deadline
  deadline=$(( $(date +%s) + timeout ))
  while (( $(date +%s) < deadline )); do
    local pending
    pending="$(ch_sql_value "SELECT COUNT(*) FROM inbox WHERE status='new'")"
    now_hash="$(
      { ch_sql_rows "SELECT
            (SELECT COUNT(*) FROM inbox),
            (SELECT IFNULL(GROUP_CONCAT(status ORDER BY id),'-') FROM inbox),
            (SELECT COUNT(*) FROM meter_events),
            (SELECT COUNT(*) FROM revisions),
            (SELECT COUNT(*) FROM revision_details),
            (SELECT IFNULL(SUM(CRC32(CONCAT_WS('~',id,wh,link_state,flags,
                IFNULL(last_event_id,0),IFNULL(fault_note,''),
                IFNULL(last_seen_at,''),IFNULL(rollup_at,''),settled,via_gateway))),0)
             FROM charge_points"
        ch_mock_request_count; } | md5sum
    )"
    if [[ "$pending" == "0" && "$now_hash" == "$last" ]]; then
      stable=$(( stable + 1 ))
      (( stable >= stable_needed )) && return 0
    else
      stable=0
    fi
    last="$now_hash"
    sleep 0.4
  done
  echo "harness: batch did not quiesce within ${timeout}s" >&2
  return 1
}
