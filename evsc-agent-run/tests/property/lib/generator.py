"""
Scenario generator. Draws from lib/domains.py to build a fleet perturbation plus
a batch of frames, then records enough structure for lib/oracle.py to predict
the outcome without re-implementing the service.

A scenario is plain data and is fully reproducible from its seed, so any
counterexample can be replayed or promoted into the characterization suite.
"""

import json
import random

from . import domains as D

RECEIVED_AT = "2021-03-04 10:00:00"


def _maybe(rng, value, p=0.5):
    """Return value with probability p, else the sentinel 'absent'."""
    return value if rng.random() < p else ABSENT


class _Absent:
    def __repr__(self):
        return "<absent>"


ABSENT = _Absent()


def _put(body, key, value):
    if value is not ABSENT:
        body[key] = value


def generate_scenario(rng):
    """
    Build one scenario:
      fleet_setup : SQL-visible perturbations applied before any frame is seen
      provision   : whether per-connector charge_points rows exist, which
                    decides if multi-connector fan-out can write readings
      frames      : list of {src, cp_ident, received_at, body, spec}
      tariff      : mock configuration
    """
    fleet_setup = []
    # Perturb a handful of units so timezone, tariff and fault-note state vary.
    targets = rng.sample(
        D.SEED_SINGLE_CONNECTOR + D.SEED_MULTI_CONNECTOR + D.SEED_LEGACY,
        k=rng.randint(2, 5))
    setup_state = {}
    for ident in targets:
        tz = rng.choice(D.TZ_VALUES)
        tariff = rng.choice(D.TARIFF_VALUES)
        note = rng.choice(D.FAULT_NOTE_VALUES)
        settled = rng.choice(D.SETTLED_VALUES)
        setup_state[ident] = {"tz": tz, "tariff": tariff,
                              "fault_note": note, "settled": settled}
        fleet_setup.append(
            "UPDATE charge_points SET tz={tz}, tariff={tf}, fault_note={fn}, "
            "settled={st} WHERE cp_ident='{id}';".format(
                tz=_sq(tz), tf=_sq(tariff), fn=_sq(note), st=settled, id=ident))

    # Optionally provision the per-connector charge_points rows the fan-out path
    # looks for. Seed data lacks them, so without this the path writes nothing.
    provision = rng.random() < 0.35
    if provision:
        for offset, ident in enumerate(D.CONNECTOR_IDENTS):
            fleet_setup.append(
                "INSERT INTO charge_points (id, cp_ident, wh, connector_count, "
                "model_code, flags, link_state, via_gateway, tz) VALUES "
                "({cid}, '{id}', 0, 1, 0, 0, 0, 0, 'America/Chicago');".format(
                    cid=900 + offset, id=ident))

    n_frames = rng.randint(1, 8)
    frames = [_generate_frame(rng) for _ in range(n_frames)]

    tariff_mode = rng.choices(
        ["ok", "ok", "ok", "http500", "empty", "malformed", "no_field"],
        k=1)[0]
    tariff_value = str(rng.choice([0, 3, 5, 6, 9, 42]))

    return {
        "fleet_setup": fleet_setup,
        "setup_state": setup_state,
        "provision_connectors": provision,
        "frames": frames,
        "tariff": {"mode": tariff_mode, "value": tariff_value, "delay_ms": "0"},
    }


def _generate_frame(rng):
    """One frame, plus a `spec` describing the choices the oracle needs."""
    shape = rng.choices(
        ["ordinary", "ordinary", "ordinary", "rollup", "link", "heartbeat",
         "fault", "diagnostic", "dt_direct", "unknown_cp", "future", "malformed",
         "unparseable_la", "multiconnector"],
        k=1)[0]

    src = rng.choice(D.SRC_VALUES)
    body = {}
    ident = None
    raw_override = None

    if shape == "malformed":
        raw_override = rng.choice([
            "this is not json", "{not json", "[]", '"just a string"', "123",
            "null", "{}", '{"msg_type":1,"wh":5}',
        ])
        ident = rng.choice(D.SEED_SINGLE_CONNECTOR)
    else:
        if shape == "unknown_cp":
            ident = rng.choice(D.UNKNOWN_IDENTS)
        elif shape == "multiconnector":
            ident = D.SEED_MULTI_CONNECTOR[0]
        else:
            ident = rng.choice(
                D.SEED_SINGLE_CONNECTOR + D.SEED_MULTI_CONNECTOR + D.SEED_LEGACY)
        body["1"] = ident

        if shape == "link":
            body["msg_type"] = rng.choice([9, 11, 14])
            _put(body, "nl", _maybe(rng, rng.choice(D.NL_VALUES), 0.4))
        elif shape == "heartbeat":
            body["msg_type"] = 3
        else:
            body["msg_type"] = rng.choice(D.MSG_TYPE_ORDINARY + D.MSG_TYPE_DISPATCHED)

        _put(body, "wh", _maybe(rng, rng.choice(D.WH_VALUES), 0.8))

        # Time source. Exactly one of rd / la / direct dt, or none.
        if shape == "rollup":
            body["rd"] = rng.choice(D.RD_VALUES)
            rh = rng.choice(D.RH_VALUES)
            if rh is not None:
                body["rh"] = rh
        elif shape == "future":
            body["la"] = rng.choice(D.LA_FUTURE_REJECTED)
        elif shape == "unparseable_la":
            body["la"] = rng.choice(D.LA_UNPARSEABLE)
        elif shape == "dt_direct":
            body["dt"] = rng.choice(D.DT_DIRECT)
        elif shape == "heartbeat":
            if rng.random() < 0.5:
                body["la"] = None            # JSON null: isset() is false
        else:
            if rng.random() < 0.85:
                body["la"] = rng.choice(D.LA_PARSEABLE_PAST)

        if shape == "fault":
            body["nt"] = rng.choice(
                D.NT_MARKER_OFFSET + D.NT_MARKER_AT_ZERO + D.NT_OVERHEAT)
        else:
            _put(body, "nt", _maybe(rng, rng.choice(
                D.NT_BENIGN + D.NT_MARKER_OFFSET + D.NT_OVERHEAT), 0.25))

        if shape == "diagnostic":
            body["fl"] = "1"
            body["sv"] = rng.choice([0, 5])
            body["hv"] = rng.choice([0, 2])
            body["dbg"] = rng.choice(D.DBG_VALUES)
        else:
            _put(body, "fl", _maybe(rng, rng.choice(D.FL_VALUES), 0.2))
            _put(body, "sv", _maybe(rng, rng.choice(D.SV_VALUES), 0.2))
            _put(body, "hv", _maybe(rng, rng.choice(D.HV_VALUES), 0.2))
            _put(body, "dbg", _maybe(rng, rng.choice(D.DBG_VALUES), 0.15))

        _put(body, "as", _maybe(rng, rng.choice(D.AS_VALUES), 0.5))
        _put(body, "connector_count",
             _maybe(rng, rng.choice(D.CONNECTOR_COUNT_VALUES), 0.15))

        # Metadata sub-object.
        if rng.random() < 0.5:
            meta = {}
            _put(meta, "firmware", _maybe(rng, rng.choice(D.FIRMWARE_VALUES), 0.6))
            _put(meta, "zd", _maybe(rng, rng.choice(D.ZD_VALUES), 0.4))
            _put(meta, "sw", _maybe(rng, rng.choice(D.SW_VALUES), 0.4))
            if rng.random() < 0.7:
                meta["la"] = rng.choice(D.LAT_VALUES)
                meta["lo"] = rng.choice(D.LON_VALUES)
            if meta:
                body["m"] = meta

        # Strip JSON nulls that were drawn as "present but null" for keys where
        # that is indistinguishable from absent, except la where it is meaningful.
        for key in ("fl", "sv", "hv", "connector_count", "nl"):
            if key in body and body[key] is None:
                del body[key]

    raw = raw_override if raw_override is not None else json.dumps(body)
    return {
        "src": src,
        "cp_ident": ident,
        "received_at": RECEIVED_AT,
        "body": raw,
        "spec": {"shape": shape, "decoded": None if raw_override else body},
    }


def _sq(value):
    if value is None:
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"


def shrink(scenario, rng):
    """
    Candidate reductions, simplest first. Property suites are only useful if a
    failure arrives small enough to read, and a random 8-frame batch is not.
    """
    out = []
    frames = scenario["frames"]

    # Drop each frame individually.
    if len(frames) > 1:
        for i in range(len(frames)):
            reduced = dict(scenario)
            reduced["frames"] = frames[:i] + frames[i + 1:]
            out.append(reduced)

    # Drop the front and back halves.
    if len(frames) > 2:
        mid = len(frames) // 2
        for half in (frames[:mid], frames[mid:]):
            reduced = dict(scenario)
            reduced["frames"] = half
            out.append(reduced)

    # Remove the fleet perturbation entirely.
    if scenario["fleet_setup"]:
        reduced = dict(scenario)
        reduced["fleet_setup"] = []
        reduced["setup_state"] = {}
        reduced["provision_connectors"] = False
        out.append(reduced)

    return out
