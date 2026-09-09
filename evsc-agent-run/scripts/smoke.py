#!/usr/bin/env python3
"""
Manual end-to-end smoke test against the production compose stack.

Brings the stack up, injects one telemetry frame exactly as the upstream
intake layer would, and reports what the service did with it. This is the
check that `bin/seed.php` was supposed to provide -- that script is referenced
by README.md but does not exist in the repository.

For a real regression gate use the automated suites (`make test`); this exists
for eyeballing behavior and for verifying a fresh environment works at all.

  ./smoke.py                          defaults: CP-0001, wh=1234
  CP_IDENT=CP-0002 WH=900 ./smoke.py  multi-connector unit
"""

import base64
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DB = "evse"

CP_IDENT = os.environ.get("CP_IDENT", "CP-0001")
WH = os.environ.get("WH", "1234")
# Fixture time is deliberately in the past so the two-day future guard passes
# and the expected UTC value is stable. CP-0001 is America/Chicago; March is
# CST (UTC-6), so 12:00 local must land at 18:00 UTC.
LOCAL_AT = os.environ.get("LOCAL_AT", "2021-03-04 12:00:00")


def compose(*args, capture=True, check=True, timeout=300):
    cmd = ["docker", "compose", *args]
    p = subprocess.run(cmd, cwd=str(REPO_ROOT), capture_output=capture,
                       text=True, timeout=timeout)
    if check and p.returncode != 0:
        msg = (p.stderr or p.stdout or "")[-2000:]
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{msg}")
    return p


def db_exec(*args, check=True):
    return compose("exec", "-T", "db", "mysql", "-u", "ingest_svc",
                   "-pingest_svc", DB, *args, check=check)


def db_value(query):
    p = db_exec("-N", "-B", "-e", query, check=False)
    return p.stdout.strip() if p.returncode == 0 else ""


def main():
    print("==> bringing up the stack")
    compose("up", "-d", "--build", capture=True, check=True, timeout=900)

    print("==> waiting for the database")
    for _ in range(60):
        if compose("exec", "-T", "db", "mysqladmin", "ping", "-h", "127.0.0.1",
                   "-u", "ingest_svc", "-pingest_svc", check=False).returncode == 0:
            break
        time.sleep(1)
    if db_value("SELECT 1") != "1":
        print("database never became ready", file=sys.stderr)
        return 1

    print(f"==> injecting one frame for {CP_IDENT}")
    body = json_body(CP_IDENT, WH, LOCAL_AT)
    b64 = base64.b64encode(body.encode()).decode()
    db_exec("-e",
            f"SET @b = CONVERT(FROM_BASE64('{b64}') USING utf8mb4);"
            f"INSERT INTO inbox (src, status, cp_ident, body, body_hash, received_at)"
            f"VALUES ('cp', 'new', '{CP_IDENT}', @b, SHA1(@b), NOW());")
    print(f"    body: {body}")

    print("==> waiting for the worker to claim and process it")
    for _ in range(40):
        if db_value("SELECT COUNT(*) FROM inbox WHERE status='new'") == "0":
            break
        time.sleep(0.5)

    print()
    print("--- inbox ---")
    db_exec("-e", "SELECT id, src, status, cp_ident, received_at"
                   " FROM inbox ORDER BY id DESC LIMIT 5;")
    print("--- meter_events ---")
    db_exec("-e", "SELECT id, inbox_id, cp_id, msg_type, wh, local_event_at, utc_event_at"
                   " FROM meter_events ORDER BY id DESC LIMIT 5;")
    print("--- audit trail ---")
    db_exec("-e", "SELECT r.target_id, r.op, d.col, d.before_, d.after_"
                   " FROM revisions r JOIN revision_details d ON d.rev_id = r.id"
                   " ORDER BY r.id DESC, d.id LIMIT 12;")
    print("--- worker log ---")
    p = compose("logs", "--no-log-prefix", "app", check=False)
    for line in (p.stdout or "").splitlines()[-5:]:
        print(line)

    print()
    status = db_value("SELECT status FROM inbox ORDER BY id DESC LIMIT 1")
    events = db_value(
        f"SELECT COUNT(*) FROM meter_events"
        f" WHERE cp_id=(SELECT id FROM charge_points WHERE cp_ident='{CP_IDENT}')")
    print(f"==> frame status: {status}    readings for {CP_IDENT}: {events}")

    if status != "done":
        print("==> UNEXPECTED: frame did not reach 'done'.", file=sys.stderr)
        print("    A claim-token status means it was stranded"
              " -- see docs/known-issues.md #5 and #14.", file=sys.stderr)
        return 1

    print("==> ok. Tear down with 'make down', or 'make nuke' to drop the data volume.")
    return 0


def json_body(cp_ident, wh, local_at):
    """Compact JSON, matching the terse frame keys the service decodes."""
    import json
    return json.dumps({"1": cp_ident, "msg_type": 1, "wh": int(wh),
                       "la": local_at}, separators=(",", ":"))


if __name__ == "__main__":
    sys.exit(main())
