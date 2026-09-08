"""
Property oracle.

Two kinds of check live here:

  * INVARIANTS -- must hold for every input, and are stated without predicting
    what the service will do. "connectors is never written" needs no model of
    the service at all.
  * NARROW ORACLES -- independent predictions for the few behaviors whose
    contract is small enough to state exactly: frame classification, UTC
    conversion, and which frames trigger the outbound lookup.

Deliberately NOT modelled: the audit trail, the parent-row update set, the
multi-connector revision fan-out. Re-implementing those would just clone the
service's bugs into the oracle and assert they match. Characterization
baselines pin those instead.

Every prediction below cites the source line it derives from, so a reader can
audit the oracle rather than trust it.
"""

import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DONE = "done"
BAD = "bad"
STRAND = "strand"          # kept its claim token: an exception mid-frame

DEFAULT_TZ = "America/Chicago"          # ingest.php:127


def _parse_naive(text):
    """
    Accept only the unambiguous shapes the generator emits. Returning None means
    "this oracle cannot predict it", which callers treat as unparseable.
    """
    if not isinstance(text, str):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


def _is_mysql_int(value):
    """
    Whether MySQL, under STRICT_TRANS_TABLES, will accept this value for an INT
    column. Anything that is not a clean integer -- '', 'a', '5abc', '1.5' --
    raises 1366/1265 rather than coercing.
    """
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return float(value).is_integer()
    if not isinstance(value, str):
        return False
    try:
        int(value.strip())
        return True
    except (ValueError, AttributeError):
        return False


def _fanout_throws(decoded, cp, fleet, connectors):
    """
    Predict the per-connector insert failure documented as known issue 15.

    On model_code 7 the `as` field is split on ':' and indexed by
    connector_no - 1 (ingest.php:200-204). The segment becomes meter_events.wh,
    an INT column (:228). A non-integer segment -- '' or 'a:b:c' -- is rejected
    by MySQL strict mode, which throws and strands the frame.

    Reachable only when a charge_points row exists for the connector ident,
    because otherwise the insert at :227 is skipped entirely (known issue 1
    masks issue 15). Returns the offending ident, or None.
    """
    if cp["model_code"] != 7:
        return None
    meta = decoded.get("m") or {}
    if isinstance(meta, dict) and meta.get("zd"):
        return None                      # descriptor suppresses fan-out (:195)
    if "as" not in decoded:
        return None                      # no per-connector values assigned
    if not connectors:
        return None

    parts = str(decoded["as"]).split(":")
    seen = set()
    for cp_id, ident, connector_no, retired in connectors:
        if int(cp_id) != cp["id"] or int(retired) != 0:
            continue
        if ident in seen:
            continue                     # $emittedConnectorIdents dedup (:210)
        seen.add(ident)
        idx = int(connector_no) - 1
        if idx < 0 or idx >= len(parts):
            continue                     # isset() fails, wh falls back (:222)
        if ident not in fleet:
            continue                     # no charge_points row -> insert skipped
        if not _is_mysql_int(parts[idx]):
            return ident
    return None


def classify(frame, fleet, now_utc, connectors=None):
    """
    Predict the terminal state of one frame and, for the simple path, how many
    meter_events rows it should produce against its own inbox id.

    `connectors` is the connectors table as rows of
    (cp_id, connector_ident, connector_no, retired). It is required to predict
    the multi-connector insert failure of known issue 15; omit it only when the
    caller does not care about that path.

    Returns dict: status, events, utc_event_at (or None/'unpredictable'),
                  local_event_at, rollup_date, rollup_hour, model_code, reason
    """
    raw = frame["body"]
    try:
        decoded = json.loads(raw)
    except ValueError:
        # json_decode returns null -> is_array() false -> bad (ingest.php:93)
        return _bad("body is not JSON")

    # PHP: json_decode('[]', true) yields an array, so is_array() passes and the
    # frame instead fails the isset($msg['1']) test. Either way: bad.
    if not isinstance(decoded, dict):
        return _bad("decoded body is not an object")
    if "1" not in decoded or decoded["1"] is None:
        return _bad("missing key '1'")          # isset() is false for null

    # Future-timestamp guard, ingest.php:109-118. Guarded by !empty(), so a
    # null/""/0 value skips the check entirely.
    la = decoded.get("la")
    if la not in (None, "", 0):
        parsed = _parse_naive(la)
        if parsed is None:
            # new DateTime($la) throws -> caught at :389 -> row keeps its token
            return {"status": STRAND, "events": 0, "utc_event_at": None,
                    "local_event_at": None, "rollup_date": None,
                    "rollup_hour": None, "model_code": None,
                    "reason": "unparseable la throws in DateTime"}
        if parsed > now_utc + timedelta(days=2):
            return _bad("la more than two days in the future")

    ident = str(decoded["1"])
    cp = fleet.get(ident)
    if cp is None:
        return _bad("cp_ident not present in charge_points")

    tz_name = cp["tz"] or DEFAULT_TZ
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return {"status": STRAND, "events": 0, "utc_event_at": None,
                "local_event_at": None, "rollup_date": None, "rollup_hour": None,
                "model_code": cp["model_code"],
                "reason": f"unresolvable tz {tz_name}"}

    # dt selection, ingest.php:130-135. isset() semantics: a JSON null does not
    # count as set, and a caller-supplied dt survives when neither rd nor la is.
    dt_text, local_event_at = None, None
    rollup_date, rollup_hour = None, None
    if decoded.get("rd") is not None:
        rollup_date = decoded["rd"]
        rollup_hour = decoded.get("rh", 0)
        if rollup_hour is None:
            rollup_hour = 0
        dt_text = f"{rollup_date} {rollup_hour}:00:00"
    elif la is not None:
        dt_text = la
        local_event_at = la
    elif decoded.get("dt") is not None:
        dt_text = decoded["dt"]

    utc_event_at = None
    if dt_text is not None:
        naive = _parse_naive(dt_text)
        if naive is None:
            return {"status": STRAND, "events": 0, "utc_event_at": None,
                    "local_event_at": None, "rollup_date": None,
                    "rollup_hour": None, "model_code": cp["model_code"],
                    "reason": f"unparseable dt {dt_text!r}"}
        utc_event_at = (naive.replace(tzinfo=tz)
                        .astimezone(ZoneInfo("UTC"))
                        .strftime("%Y-%m-%d %H:%M:%S"))

    # Known issue 15: a non-integer `as` segment is rejected by the INT column
    # during per-connector insert, which throws before the frame can complete.
    offender = _fanout_throws(decoded, cp, fleet, connectors)
    if offender is not None:
        return {"status": STRAND, "events": 0, "utc_event_at": None,
                "local_event_at": None, "rollup_date": None,
                "rollup_hour": None, "model_code": cp["model_code"],
                "reason": f"non-integer as segment for {offender} "
                          f"violates meter_events.wh INT"}

    # Event count against the frame's OWN inbox id. The multi-connector branch
    # (ingest.php:194) replaces the simple insert at :239, so the parent frame
    # never gets a row of its own -- readings, if any, hang off synthetic rows.
    events = 0 if cp["model_code"] == 7 else 1

    return {"status": DONE, "events": events, "utc_event_at": utc_event_at,
            "local_event_at": local_event_at, "rollup_date": rollup_date,
            "rollup_hour": rollup_hour, "model_code": cp["model_code"],
            "reason": "accepted"}


def _bad(reason):
    return {"status": BAD, "events": 0, "utc_event_at": None,
            "local_event_at": None, "rollup_date": None, "rollup_hour": None,
            "model_code": None, "reason": reason}


def predicts_lookup(frame, fleet):
    """
    Whether this frame should cause exactly one outbound tariff request, and the
    path it should use.

    Mirrors ingest.php:166-178 including the truthiness quirk at :168: that line
    omits `!== false`, so a marker at offset 0 is falsy and missed. Line :166
    gets it right for OVERHEAT.
    """
    try:
        decoded = json.loads(frame["body"])
    except ValueError:
        return (False, None)
    if not isinstance(decoded, dict) or decoded.get("1") is None:
        return (False, None)

    cp = fleet.get(str(decoded["1"]))
    if cp is None:
        return (False, None)

    nt = decoded.get("nt")
    if not isinstance(nt, str):
        return (False, None)

    # :166 -- correct strpos usage, so any position counts.
    if "OVERHEAT" in nt:
        return (False, None)

    # :168 -- truthy strpos, so offset 0 does not count.
    hit = (nt.find("GROUND FAULT") > 0) or (nt.find("CONNECTOR LOCK FAULT") > 0)
    if not hit:
        return (False, None)

    # :170 -- a NULL tariff takes the branch that never calls out.
    if cp["tariff"] is None:
        return (False, None)

    meta = decoded.get("m") or {}
    lat = meta.get("la", 0) if isinstance(meta, dict) else 0
    lon = meta.get("lo", 0) if isinstance(meta, dict) else 0
    if lat is None:
        lat = 0
    if lon is None:
        lon = 0
    return (True, f"GET /r/{_fmt_coord(lat)}/{_fmt_coord(lon)}")


def _fmt_coord(value):
    """
    Reproduce PHP string interpolation of the coordinate into the URL. PHP
    renders integral floats without a decimal part, so 90.0 becomes "90".
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
