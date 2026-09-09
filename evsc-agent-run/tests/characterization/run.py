#!/usr/bin/env python3
"""
Characterization suite runner.

  ./run.py                      verify every case against its baseline
  ./run.py --record             (re)record baselines for every case
  ./run.py --record 'simple-*'  record only matching cases
  ./run.py --list               list cases
  ./run.py --keep-up 'simple-*' leave the stack running afterwards

Exit status is 0 only when every case matches its baseline and every declared
assertion holds. See tests/README.md for the refactor and bugfix workflows.

A case is data (cases/*.json), never code. This runner and tests/harness/stack.py
observe the service under test purely through its external edges, so the suite is
independent of the language the service is written in.
"""

import argparse
import difflib
import fnmatch
import json
import os
import re
import sys
import time
from pathlib import Path

SELF_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SELF_DIR.parent))

from harness import stack  # noqa: E402

CASES_DIR = SELF_DIR / "cases"
BASELINE_DIR = SELF_DIR / "baselines"
RESULT_DIR = SELF_DIR / ".results"

# Datetimes within this many seconds of "now" are clock-derived and are masked
# as <CLOCK> in snapshots. The window must exceed the largest real-world UTC
# offset (+/-14h), because the service writes some columns in the charge point's
# *site-local* time -- inbox.received_at on a msg_type 3 frame, for one -- while
# this comparison runs in the database's own timezone. 15h covers every zone.
# Fixture timestamps live in 2021 and 2099, years outside the window, so they
# are never masked.
CLOCK_WINDOW_SECONDS = 54000

# Tables observed in full, in a stable order.
TABLES = ("inbox", "meter_events", "revisions", "revision_details")

PID_PREFIX = re.compile(r"^\[[0-9]+\]")
PHP_LINE = re.compile(r"(in /app/src/[^ ]+) on line \d+")


# ------------------------------------------------------ column introspection ----

_COLUMN_CACHE = {}


def columns(table):
    """
    (name, data_type) per column in ordinal order. Discovered at runtime so the
    suite never hardcodes a schema it might drift from. Cached: reset_db replays
    the same sql/schema.sql for every case, and each lookup costs a container
    round trip.
    """
    if table not in _COLUMN_CACHE:
        _COLUMN_CACHE[table] = [(r[0], r[1]) for r in stack.sql_rows(
            "SELECT COLUMN_NAME, DATA_TYPE FROM information_schema.columns"
            f" WHERE table_schema='{stack.DB}' AND table_name='{table}'"
            " ORDER BY ORDINAL_POSITION")]
    return _COLUMN_CACHE[table]


def column_names(table):
    return "|".join(name for name, _ in columns(table))


def projection(table, alias=""):
    """
    A CONCAT_WS select expression masking non-deterministic values so snapshots
    are byte-stable across runs:
        datetime/date/timestamp near "now" -> <CLOCK>
        an in-flight inbox claim token     -> <TOKEN>
    """
    parts = []
    for name, dtype in columns(table):
        ref = f"{alias}`{name}`"
        if dtype in ("datetime", "timestamp", "date"):
            parts.append(
                f"CASE WHEN {ref} IS NULL THEN 'NULL'"
                f" WHEN ABS(TIMESTAMPDIFF(SECOND, {ref}, NOW())) <= {CLOCK_WINDOW_SECONDS}"
                f" THEN '<CLOCK>'"
                f" ELSE CAST({ref} AS CHAR) END")
        elif table == "inbox" and name == "status":
            parts.append(
                f"CASE WHEN {ref} REGEXP '^c[0-9a-f]{{11}}$' THEN '<TOKEN>'"
                f" ELSE IFNULL(CAST({ref} AS CHAR),'NULL') END")
        else:
            parts.append(f"IFNULL(CAST({ref} AS CHAR),'NULL')")
    return "CONCAT_WS('|'," + ",".join(parts) + ")"


def identity(table, alias=""):
    """Raw row-identity expression, used only to detect whether a row changed."""
    parts = [f"IFNULL(CAST({alias}`{name}` AS CHAR),'~NULL~')"
             for name, _ in columns(table)]
    return "MD5(CONCAT_WS('~'," + ",".join(parts) + "))"


# ------------------------------------------------------------------ observe ----

def dump_table(table, out):
    out.append(f"-- {table} ({column_names(table)})")
    rows = stack.sql_rows(f"SELECT {projection(table)} FROM `{table}` ORDER BY id")
    if not rows:
        out.append("   <no rows>")
    else:
        out.extend(r[0] for r in rows)


def dump_changed(table, out):
    """
    Report only rows that differ from the pristine seed, so a 22-row fleet does
    not drown the diff -- and so an unexpected write to an unrelated row shows up.
    """
    pristine = stack.PRISTINE[table]
    out.append(f"-- {table} CHANGED vs seed ({column_names(table)})")
    rows = stack.sql_rows(
        f"SELECT {projection(table, 'c.')} FROM `{table}` c"
        f" JOIN `{pristine}` p ON p.id = c.id"
        f" WHERE {identity(table, 'c.')} <> {identity(table, 'p.')}"
        " ORDER BY c.id")
    added = stack.sql_value(
        f"SELECT COUNT(*) FROM `{table}` c WHERE NOT EXISTS"
        f" (SELECT 1 FROM `{pristine}` p WHERE p.id=c.id)")
    removed = stack.sql_value(
        f"SELECT COUNT(*) FROM `{pristine}` p WHERE NOT EXISTS"
        f" (SELECT 1 FROM `{table}` c WHERE c.id=p.id)")
    if not rows:
        out.append("   <none>")
    else:
        out.extend(r[0] for r in rows)
    out.append(f"   rows_inserted={added} rows_deleted={removed}")


def app_stdout():
    # Normalize non-deterministic prefixes the service emits:
    #   "[1] msg"               -> "[PID] msg"   (pid varies per container)
    #   "... on line 159"       -> "... on line <N>"  (shifts with any source edit)
    out = "\n".join(stack.app_logs().splitlines())
    out = PID_PREFIX.sub("[PID]", out)
    out = PHP_LINE.sub(r"\1 on line <N>", out)
    return out


def _block(out, header, body, empty):
    out.append(header)
    if not body:
        out.append(empty)
    else:
        out.extend(body)


def phase_snapshot(phase_no, label):
    """The full observable snapshot for one phase, as a list of lines."""
    out = [f"### phase {phase_no}: {label}"]
    for t in TABLES:
        dump_table(t, out)
    dump_changed("charge_points", out)
    dump_changed("connectors", out)
    _block(out, "-- tariff requests", stack.mock_requests(), "   <none>")
    _block(out, "-- app stdout", app_stdout().splitlines(), "   <empty>")
    _block(out, "-- app file writes", stack.app_file_writes(), "   <none>")
    out.append(f"-- app container state: {stack.app_state()}")
    out.append("")
    return out


# --------------------------------------------------------------- case runner ----

def load_cases(patterns):
    files = sorted(CASES_DIR.glob("*.json"))
    if not patterns:
        return files
    return [f for f in files
            if any(fnmatch.fnmatch(f.stem, p) for p in patterns)]


def frame_body(frame):
    if "raw_body" in frame:
        return frame["raw_body"]
    # Compact separators match `jq -c`, which produced every recorded baseline.
    return json.dumps(frame["body"], separators=(",", ":"), ensure_ascii=False)


def run_case(path, mode, results):
    name = path.stem
    case = json.loads(path.read_text())
    case_env = {k: str(v) for k, v in (case.get("env") or {}).items()}
    print(f"  {name:<42} ", end="", flush=True)

    stack.app_stop()
    stack.write_env(case_env)
    # Recreate the mock so it picks up this case's TARIFF_* settings.
    stack.mock_recreate()
    stack.reset_db()
    stack.mock_reset()

    started_at = time.time()
    lines = [
        f"# characterization baseline: {name}",
        f"# {case.get('description', '')}",
        f"# documents: {', '.join(case.get('documents') or [])}",
        "#",
        "# Masked values: <CLOCK> clock-derived datetime, <TOKEN> in-flight",
        "# claim token, [PID] worker process id.",
        "",
    ]

    for i, phase in enumerate(case["phases"]):
        # Pre-phase SQL runs before the worker can see anything new.
        for statement in phase.get("sql") or []:
            stack.sql(statement)

        # Frames are inserted while the worker is stopped so that batch
        # composition -- and therefore id allocation -- is deterministic.
        frames = [{
            "src": f.get("src", "cp"),
            "cp_ident": f["cp_ident"],
            "received_at": f.get("received_at", "2021-03-04 10:00:00"),
            "body": frame_body(f),
        } for f in phase.get("frames") or []]
        if frames:
            stack.insert_frames(frames)

        # app_start rewrites the env file, so the whole case env goes with it.
        stack.app_start(case_env)
        if not stack.wait_quiesce(timeout=phase.get("timeout", 45)):
            print("FAIL (no quiesce)")
            results["failed"].append(f"{name}: batch did not quiesce")
            stack.app_stop()
            return
        lines.extend(phase_snapshot(i + 1, phase.get("label", "phase")))

    elapsed_ms = int((time.time() - started_at) * 1000)
    snapshot = "\n".join(lines) + "\n"
    (RESULT_DIR / f"{name}.snapshot").write_text(snapshot)

    errors = check_assertions(case.get("assert") or {}, elapsed_ms)
    stack.app_stop()

    baseline = BASELINE_DIR / f"{name}.snapshot"
    diff_path = RESULT_DIR / f"{name}.diff"

    if mode == "record":
        baseline.write_text(snapshot)
        if errors:
            print("RECORDED (assertions failed)")
            results["failed"].extend(f"{name}: {e}" for e in errors)
        else:
            print("RECORDED")
            results["recorded"].append(name)
        return

    if not baseline.exists():
        print("FAIL (no baseline; run --record)")
        results["failed"].append(f"{name}: no baseline recorded")
        return

    want = baseline.read_text()
    if want != snapshot:
        diff = difflib.unified_diff(
            want.splitlines(keepends=True), snapshot.splitlines(keepends=True),
            fromfile=str(baseline), tofile=str(RESULT_DIR / f"{name}.snapshot"))
        diff_path.write_text("".join(diff))
        print("FAIL (snapshot drift)")
        results["failed"].append(
            f"{name}: snapshot drift -> .results/{name}.diff")
        return
    diff_path.unlink(missing_ok=True)

    if errors:
        print("FAIL (assertions)")
        results["failed"].extend(f"{name}: {e}" for e in errors)
        return

    print("ok")
    results["passed"].append(name)


def check_assertions(spec, elapsed_ms):
    """Evaluated after quiescence, i.e. at the end of a unit of processing."""
    errors = []

    want_reqs = spec.get("tariff_request_count")
    if want_reqs is not None:
        got = len(stack.mock_requests())
        if str(got) != str(want_reqs):
            errors.append(f"tariff_request_count: want {want_reqs} got {got}")

    requests = stack.mock_requests()
    for want in spec.get("tariff_requests_include") or []:
        if want not in requests:
            errors.append(f"tariff_requests_include: missing '{want}'")

    out = app_stdout()
    for want in spec.get("stdout_contains") or []:
        if want not in out:
            errors.append(f"stdout_contains: missing '{want}'")
    for deny in spec.get("stdout_excludes") or []:
        if deny in out:
            errors.append(f"stdout_excludes: found '{deny}'")

    for probe in spec.get("sql") or []:
        got = stack.sql_value(probe["query"])
        if str(got) != str(probe["value"]):
            errors.append(f"sql[{probe['query']}]: "
                          f"want '{probe['value']}' got '{got}'")

    min_ms = spec.get("min_duration_ms")
    if min_ms is not None and elapsed_ms < int(min_ms):
        errors.append(f"min_duration_ms: want >={min_ms} got {elapsed_ms}")

    want_state = spec.get("app_state")
    if want_state is not None:
        got_state = stack.app_state()
        if got_state != want_state:
            errors.append(f"app_state: want '{want_state}' got '{got_state}'")

    return errors


# ---------------------------------------------------------------------- main ----

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--record", action="store_true", help="(re)record baselines")
    ap.add_argument("--list", action="store_true", help="list cases")
    ap.add_argument("--keep-up", action="store_true",
                    help="leave the stack running afterwards")
    ap.add_argument("patterns", nargs="*", metavar="PATTERN",
                    help="case name globs")
    args = ap.parse_args()

    stack.configure(os.environ.get("CH_PROJECT", "evse-charz"),
                    SELF_DIR / ".env.runtime")

    cases = load_cases(args.patterns)
    if not cases:
        print("no cases matched", file=sys.stderr)
        return 2

    if args.list:
        for f in cases:
            desc = json.loads(f.read_text()).get("description", "")
            print(f"{f.stem:<42} {desc}")
        return 0

    BASELINE_DIR.mkdir(parents=True, exist_ok=True)
    RESULT_DIR.mkdir(parents=True, exist_ok=True)

    mode = "record" if args.record else "verify"
    results = {"passed": [], "failed": [], "recorded": []}

    print(f"characterization suite: {len(cases)} case(s), mode={mode}")
    print("bringing up disposable stack...")
    try:
        stack.up(build=True)
        stack.build_app()
        print()
        for f in cases:
            run_case(f, mode, results)
    finally:
        if args.keep_up:
            print()
            print(f"stack left running (project '{stack.PROJECT}'); tear down with:")
            print(f"  docker compose -p {stack.PROJECT} -f {stack.COMPOSE_FILE}"
                  f" --env-file {stack.ENV_FILE} down -v")
        else:
            stack.down()

    print()
    print(f"passed:   {len(results['passed'])}")
    print(f"recorded: {len(results['recorded'])}")
    print(f"failed:   {len(results['failed'])}")
    if results["failed"]:
        print()
        for f in results["failed"]:
            print(f"  {f}")
        return 1
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # `run.py --list | head` closes the pipe early; that is not an error.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
