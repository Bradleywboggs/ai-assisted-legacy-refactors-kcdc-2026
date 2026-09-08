#!/usr/bin/env bash
# Characterization suite runner.
#
#   ./run.sh                      verify every case against its baseline
#   ./run.sh --record             (re)record baselines for every case
#   ./run.sh --record simple-*    record only matching cases
#   ./run.sh --list               list cases
#   ./run.sh --keep-up simple-*   leave the stack running afterwards
#
# Exit status is 0 only when every case matches its baseline and every declared
# assertion holds. See tests/README.md for the refactor and bugfix workflows.
#
# A case is data (cases/*.json), never code. This runner and lib/harness.sh
# observe the service under test purely through its external edges, so the suite
# is independent of the language the service is written in.

set -o errexit
set -o nounset
set -o pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib/harness.sh
source "$SELF_DIR/lib/harness.sh"

CASES_DIR="$SELF_DIR/cases"
BASELINE_DIR="$SELF_DIR/baselines"
RESULT_DIR="$SELF_DIR/.results"

MODE="verify"
KEEP_UP=0
PATTERNS=()

while (( $# )); do
  case "$1" in
    --record)  MODE="record" ;;
    --list)    MODE="list" ;;
    --keep-up) KEEP_UP=1 ;;
    -h | --help)
      sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  PATTERNS+=("$1") ;;
  esac
  shift
done

mkdir -p "$BASELINE_DIR" "$RESULT_DIR"

jqr() { jq -r "$@"; }

select_cases() {
  local f name
  for f in "$CASES_DIR"/*.json; do
    [[ -e "$f" ]] || continue
    name="$(basename "$f" .json)"
    if (( ${#PATTERNS[@]} )); then
      local p matched=0
      for p in "${PATTERNS[@]}"; do
        # shellcheck disable=SC2053
        [[ "$name" == $p ]] && matched=1
      done
      (( matched )) || continue
    fi
    printf '%s\n' "$f"
  done
}

if [[ "$MODE" == "list" ]]; then
  while read -r f; do
    printf '%-42s %s\n' "$(basename "$f" .json)" "$(jqr '.description // ""' "$f")"
  done < <(select_cases)
  exit 0
fi

# ------------------------------------------------------------- case runner ----

# Emits the full observable snapshot for one phase.
emit_phase_snapshot() {
  local phase_no="$1" phase_label="$2"
  printf '### phase %s: %s\n' "$phase_no" "$phase_label"
  local t
  for t in "${CH_TABLES[@]}"; do ch_dump_table "$t"; done
  ch_dump_changed charge_points
  ch_dump_changed connectors

  printf -- "-- tariff requests\n"
  local reqs
  reqs="$(ch_mock_requests)"
  if [[ -z "$reqs" ]]; then printf -- "   <none>\n"; else printf '%s\n' "$reqs"; fi

  printf -- "-- app stdout\n"
  local out
  out="$(ch_app_stdout)"
  if [[ -z "$out" ]]; then printf -- "   <empty>\n"; else printf '%s\n' "$out"; fi

  printf -- "-- app file writes\n"
  local writes
  writes="$(ch_app_file_writes)"
  if [[ -z "$writes" ]]; then printf -- "   <none>\n"; else printf '%s\n' "$writes"; fi

  printf -- "-- app container state: %s\n" "$(ch_app_state)"
  printf '\n'
}

FAILED=()
PASSED=()
RECORDED=()

run_case() {
  local file="$1"
  local name; name="$(basename "$file" .json)"
  local snapshot="$RESULT_DIR/$name.snapshot"
  local baseline="$BASELINE_DIR/$name.snapshot"

  printf '  %-42s ' "$name"

  # --- per-case stack configuration -----------------------------------------
  local env_pairs=()
  while IFS= read -r kv; do [[ -n "$kv" ]] && env_pairs+=("$kv"); done \
    < <(jqr '(.env // {}) | to_entries[] | "\(.key)=\(.value)"' "$file")

  ch_app_stop
  ch_write_env "${env_pairs[@]+"${env_pairs[@]}"}"

  # Recreate the mock only when its behavior differs from the running one.
  ch_compose up -d --no-deps tariff >/dev/null 2>&1
  ch_reset_db
  ch_mock_reset

  # --- phases ----------------------------------------------------------------
  local n_phases; n_phases="$(jqr '.phases | length' "$file")"
  local started_at; started_at="$(date +%s%3N)"
  : >"$snapshot"
  {
    printf '# characterization baseline: %s\n' "$name"
    printf '# %s\n' "$(jqr '.description // ""' "$file")"
    printf '# documents: %s\n' "$(jqr '(.documents // []) | join(", ")' "$file")"
    printf '#\n# Masked values: <CLOCK> clock-derived datetime, <TOKEN> in-flight\n'
    printf '# claim token, [PID] worker process id.\n\n'
  } >>"$snapshot"

  local i
  for (( i = 0; i < n_phases; i++ )); do
    local label; label="$(jqr ".phases[$i].label // \"phase\"" "$file")"

    # Pre-phase SQL runs before the worker can see anything new.
    local sql
    while IFS= read -r sql; do
      [[ -n "$sql" ]] && ch_sql "$sql"
    done < <(jqr ".phases[$i].sql[]? // empty" "$file")

    # Frames are inserted while the worker is stopped on phase 0 so that batch
    # composition -- and therefore id allocation -- is deterministic.
    local n_frames; n_frames="$(jqr ".phases[$i].frames | length // 0" "$file")"
    local j
    for (( j = 0; j < n_frames; j++ )); do
      local src cp body received
      src="$(jqr ".phases[$i].frames[$j].src // \"cp\"" "$file")"
      cp="$(jqr ".phases[$i].frames[$j].cp_ident" "$file")"
      received="$(jqr ".phases[$i].frames[$j].received_at // \"2021-03-04 10:00:00\"" "$file")"
      if [[ "$(jqr ".phases[$i].frames[$j] | has(\"raw_body\")" "$file")" == "true" ]]; then
        body="$(jqr ".phases[$i].frames[$j].raw_body" "$file")"
      else
        body="$(jq -c ".phases[$i].frames[$j].body" "$file")"
      fi
      ch_insert_frame "$src" "$cp" "$body" "$received"
    done

    ch_app_start
    local timeout; timeout="$(jqr ".phases[$i].timeout // 45" "$file")"
    if ! ch_wait_quiesce "$timeout"; then
      printf 'FAIL (no quiesce)\n'
      FAILED+=("$name: batch did not quiesce")
      ch_app_stop
      return 0
    fi

    emit_phase_snapshot "$(( i + 1 ))" "$label" >>"$snapshot"
  done

  local elapsed=$(( $(date +%s%3N) - started_at ))

  # --- declared assertions ---------------------------------------------------
  # Evaluated after quiescence, i.e. at the end of a unit of processing.
  local errors=()

  local want_reqs; want_reqs="$(jqr '.assert.tariff_request_count // empty' "$file")"
  if [[ -n "$want_reqs" ]]; then
    local got_reqs; got_reqs="$(ch_mock_request_count)"
    [[ "$got_reqs" == "$want_reqs" ]] || \
      errors+=("tariff_request_count: want $want_reqs got $got_reqs")
  fi

  while IFS= read -r want; do
    [[ -z "$want" ]] && continue
    ch_mock_requests | grep -Fqx "$want" || \
      errors+=("tariff_requests_include: missing '$want'")
  done < <(jqr '.assert.tariff_requests_include[]? // empty' "$file")

  while IFS= read -r want; do
    [[ -z "$want" ]] && continue
    ch_app_stdout | grep -Fq "$want" || errors+=("stdout_contains: missing '$want'")
  done < <(jqr '.assert.stdout_contains[]? // empty' "$file")

  while IFS= read -r deny; do
    [[ -z "$deny" ]] && continue
    ch_app_stdout | grep -Fq "$deny" && errors+=("stdout_excludes: found '$deny'")
  done < <(jqr '.assert.stdout_excludes[]? // empty' "$file")

  while IFS= read -r probe; do
    [[ -z "$probe" ]] && continue
    local q v want
    q="$(printf '%s' "$probe" | base64 -d | jq -r '.query')"
    want="$(printf '%s' "$probe" | base64 -d | jq -r '.value')"
    v="$(ch_sql_value "$q")"
    [[ "$v" == "$want" ]] || errors+=("sql[$q]: want '$want' got '$v'")
  done < <(jqr '.assert.sql[]? | @base64' "$file")

  local min_ms; min_ms="$(jqr '.assert.min_duration_ms // empty' "$file")"
  if [[ -n "$min_ms" ]] && (( elapsed < min_ms )); then
    errors+=("min_duration_ms: want >=${min_ms} got ${elapsed}")
  fi

  local want_state; want_state="$(jqr '.assert.app_state // empty' "$file")"
  if [[ -n "$want_state" ]]; then
    local got_state; got_state="$(ch_app_state)"
    [[ "$got_state" == "$want_state" ]] || \
      errors+=("app_state: want '$want_state' got '$got_state'")
  fi

  ch_app_stop

  # --- baseline comparison ---------------------------------------------------
  if [[ "$MODE" == "record" ]]; then
    cp "$snapshot" "$baseline"
    if (( ${#errors[@]} )); then
      printf 'RECORDED (assertions failed)\n'
      local e; for e in "${errors[@]}"; do FAILED+=("$name: $e"); done
    else
      printf 'RECORDED\n'
      RECORDED+=("$name")
    fi
    return 0
  fi

  if [[ ! -f "$baseline" ]]; then
    printf 'FAIL (no baseline; run --record)\n'
    FAILED+=("$name: no baseline recorded")
    return 0
  fi

  if ! diff -u "$baseline" "$snapshot" >"$RESULT_DIR/$name.diff"; then
    printf 'FAIL (snapshot drift)\n'
    FAILED+=("$name: snapshot drift -> ${RESULT_DIR#"$SELF_DIR/"}/$name.diff")
    return 0
  fi
  rm -f "$RESULT_DIR/$name.diff"

  if (( ${#errors[@]} )); then
    printf 'FAIL (assertions)\n'
    local e; for e in "${errors[@]}"; do FAILED+=("$name: $e"); done
    return 0
  fi

  printf 'ok\n'
  PASSED+=("$name")
}

# ------------------------------------------------------------------- main ----

mapfile -t CASE_FILES < <(select_cases)
if (( ${#CASE_FILES[@]} == 0 )); then
  echo "no cases matched" >&2
  exit 2
fi

cleanup() {
  if (( KEEP_UP )); then
    echo
    echo "stack left running (project '$PROJECT'); tear down with:"
    echo "  docker compose -p $PROJECT -f $COMPOSE_FILE --env-file $ENV_FILE down -v"
  else
    ch_teardown
  fi
}
trap cleanup EXIT

echo "characterization suite: ${#CASE_FILES[@]} case(s), mode=$MODE"
echo "bringing up disposable stack..."
ch_infra_up
ch_compose build app >/dev/null 2>&1
echo

for f in "${CASE_FILES[@]}"; do
  run_case "$f"
done

echo
echo "passed:   ${#PASSED[@]}"
echo "recorded: ${#RECORDED[@]}"
echo "failed:   ${#FAILED[@]}"
if (( ${#FAILED[@]} )); then
  echo
  printf '  %s\n' "${FAILED[@]}"
  exit 1
fi
exit 0
