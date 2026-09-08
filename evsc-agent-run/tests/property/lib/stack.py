"""
Disposable-stack plumbing for the property suite.

Reuses tests/characterization/docker-compose.test.yml verbatim so the two suites
cannot drift apart on topology; only the compose project name differs, letting
both run without colliding. Observation is black-box: SQL, the recorded HTTP
request log, container logs, and `docker diff`. Nothing here reads service source.
"""

import base64
import json
import os
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROP_DIR = HERE.parent
REPO_ROOT = PROP_DIR.parent.parent
COMPOSE_FILE = REPO_ROOT / "tests" / "characterization" / "docker-compose.test.yml"
PROJECT = os.environ.get("PROP_PROJECT", "evse-prop")
ENV_FILE = PROP_DIR / ".env.runtime"
DB = "evse"


class StackError(RuntimeError):
    pass


def _compose(*args, capture=True, check=True, timeout=300):
    cmd = ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE),
           "--env-file", str(ENV_FILE), *args]
    proc = subprocess.run(cmd, capture_output=capture, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise StackError(f"{' '.join(args[:2])} failed: {proc.stderr[-2000:]}")
    return proc


def write_env(pairs=None):
    ENV_FILE.write_text("".join(f"{k}={v}\n" for k, v in (pairs or {}).items()))


def up(build=False):
    write_env()
    args = ["up", "-d"]
    if build:
        args.append("--build")
    _compose(*args, "db", "tariff", timeout=900)
    _wait_db()


def build_app():
    _compose("build", "app", timeout=900)


def down():
    write_env()
    _compose("down", "-v", "--remove-orphans", check=False)
    ENV_FILE.unlink(missing_ok=True)


def _wait_db(timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        p = _compose("exec", "-T", "db", "mysqladmin", "ping", "-h", "127.0.0.1",
                     "-uingest_svc", "-pingest_svc", check=False)
        if p.returncode == 0:
            for _ in range(60):
                try:
                    if sql_rows("SELECT COUNT(*) FROM charge_points")[0][0].isdigit():
                        return
                except Exception:
                    pass
                time.sleep(1)
            return
        time.sleep(1)
    raise StackError("database never became ready")


# ------------------------------------------------------------------- sql ----

def sql(statement):
    """Run one or more statements, discarding output."""
    p = _compose("exec", "-T", "db", "mysql", "-uingest_svc", "-pingest_svc",
                 "--database", DB, "-e", statement, check=False)
    if p.returncode != 0:
        raise StackError(f"sql failed: {p.stderr[-1500:]}\n{statement[:500]}")


def sql_rows(query):
    """Return rows as lists of strings. NULL arrives as the literal 'NULL'."""
    p = _compose("exec", "-T", "db", "mysql", "-uingest_svc", "-pingest_svc",
                 "--database", DB, "-N", "-B", "-e", query, check=False)
    if p.returncode != 0:
        raise StackError(f"query failed: {p.stderr[-1500:]}\n{query[:500]}")
    return [line.split("\t") for line in p.stdout.splitlines() if line != ""]


def sql_value(query):
    rows = sql_rows(query)
    return rows[0][0] if rows else None


def reset_db():
    """Replay the real schema file, then snapshot pristine reference tables."""
    schema = (REPO_ROOT / "sql" / "schema.sql").read_text()
    p = subprocess.run(
        ["docker", "compose", "-p", PROJECT, "-f", str(COMPOSE_FILE),
         "--env-file", str(ENV_FILE), "exec", "-T", "db",
         "mysql", "-uingest_svc", "-pingest_svc", "--database", DB],
        input=schema, capture_output=True, text=True, timeout=300)
    if p.returncode != 0:
        raise StackError(f"schema reset failed: {p.stderr[-1500:]}")
    sql("DROP TABLE IF EXISTS _p_cp; DROP TABLE IF EXISTS _p_conn;"
        "CREATE TABLE _p_cp AS SELECT * FROM charge_points;"
        "CREATE TABLE _p_conn AS SELECT * FROM connectors;")


def read_fleet():
    """The fleet as the database actually holds it -- never a hardcoded copy."""
    fleet = {}
    for row in sql_rows("SELECT cp_ident, id, model_code, tz, tariff, "
                        "IFNULL(fault_note,'~NULL~'), settled, flags, wh "
                        "FROM charge_points"):
        ident, cid, model, tz, tariff, note, settled, flags, wh = row
        fleet[ident] = {
            "id": int(cid), "model_code": int(model), "tz": tz,
            "tariff": None if tariff == "NULL" else tariff,
            "fault_note": None if note in ("NULL", "~NULL~") else note,
            "settled": int(settled), "flags": int(flags), "wh": int(wh),
        }
    return fleet


def read_connectors():
    return sql_rows("SELECT cp_id, connector_ident, connector_no, retired "
                    "FROM connectors ORDER BY id")


# ----------------------------------------------------------------- frames ----

def insert_frames(frames):
    """
    Insert every frame in ONE statement batch, so ids are 1..N in list order
    and a scenario costs one round trip. `body` is passed base64-encoded to keep
    arbitrary bytes intact through shell and SQL.
    """
    parts = []
    for f in frames:
        b64 = base64.b64encode(f["body"].encode()).decode()
        parts.append(
            "INSERT INTO inbox (src, status, cp_ident, body, body_hash, received_at) "
            "SELECT {src}, 'new', {cp}, b, SHA1(b), {rec} FROM "
            "(SELECT CONVERT(FROM_BASE64('{b64}') USING utf8mb4) AS b) t;".format(
                src=_q(f["src"]), cp=_q(f["cp_ident"]), rec=_q(f["received_at"]),
                b64=b64))
    sql("".join(parts))


def _q(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("\\", "\\\\").replace("'", "''") + "'"


# ------------------------------------------------------------------ drive ----

def app_start(env=None):
    write_env(env or {})
    _compose("up", "-d", "app", timeout=600)


def app_stop():
    _compose("stop", "-t", "2", "app", check=False)
    _compose("rm", "-f", "app", check=False)


def app_state():
    p = _compose("ps", "app", "--format", "{{.State}}", check=False)
    return (p.stdout or "").strip().splitlines()[0] if p.stdout.strip() else "absent"


def app_logs():
    p = _compose("logs", "--no-log-prefix", "app", check=False)
    return p.stdout or ""


def app_file_writes():
    p = _compose("ps", "-q", "app", check=False)
    cid = (p.stdout or "").strip().splitlines()
    if not cid:
        return []
    d = subprocess.run(["docker", "diff", cid[0]], capture_output=True, text=True)
    skip = {"C /tmp", "C /run", "C /var/run", "C /proc", "C /sys"}
    return [l for l in d.stdout.splitlines() if l and l not in skip]


def mock_reset():
    _compose("exec", "-T", "tariff", "sh", "-c", ": > /tmp/requests.log", check=False)


def mock_requests():
    p = _compose("exec", "-T", "tariff", "cat", "/tmp/requests.log", check=False)
    return [l for l in (p.stdout or "").splitlines() if l.strip()]


def mock_configure(mode="ok", value="9", delay_ms="0"):
    write_env({"TARIFF_MODE": mode, "TARIFF_VALUE": value,
               "TARIFF_DELAY_MS": delay_ms})
    _compose("up", "-d", "--no-deps", "tariff", timeout=300)


# -------------------------------------------------------------- quiescence ----

def wait_quiesce(timeout=90, stable_needed=3, poll=0.4):
    """
    Settle on: nothing claimable remains AND observable state has stopped
    changing. Stranded rows never reach a terminal status, so stability rather
    than terminality is the stop condition.
    """
    last, stable = None, 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        pending = sql_value("SELECT COUNT(*) FROM inbox WHERE status='new'")
        fingerprint = sql_rows(
            "SELECT (SELECT COUNT(*) FROM inbox),"
            " (SELECT IFNULL(GROUP_CONCAT(status ORDER BY id),'-') FROM inbox),"
            " (SELECT COUNT(*) FROM meter_events),"
            " (SELECT COUNT(*) FROM revisions),"
            " (SELECT COUNT(*) FROM revision_details),"
            " (SELECT IFNULL(SUM(CRC32(CONCAT_WS('~',id,wh,link_state,flags,"
            "   IFNULL(last_event_id,0),IFNULL(fault_note,''),"
            "   IFNULL(last_seen_at,''),IFNULL(rollup_at,''),settled,via_gateway)"
            "  )),0) FROM charge_points)")
        key = (pending, tuple(fingerprint[0]) if fingerprint else (), len(mock_requests()))
        if pending == "0" and key == last:
            stable += 1
            if stable >= stable_needed:
                return True
        else:
            stable = 0
        last = key
        time.sleep(poll)
    return False
