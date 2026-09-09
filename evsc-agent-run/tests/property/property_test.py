#!/usr/bin/env python3
"""
Property-based driver for evse-ingest.

No production data exists for this service, so instead of replaying a sample
this generates inputs from the domains in lib/domains.py -- each derived from
how the service actually consumes the field -- runs them through the real
service, and checks properties that must hold for any input.

  ./property_test.py                        30 scenarios, random seed
  ./property_test.py --scenarios 100        longer run
  ./property_test.py --seed 12345           reproduce a specific run
  ./property_test.py --replay corpus/x.json re-run a saved counterexample
  ./property_test.py --no-shrink            skip shrinking on failure

Every failure prints the seed, writes the scenario to corpus/, and shrinks it to
a minimal reproducer. Promote a counterexample into the permanent suite by
converting it to a characterization case; see README.md.
"""

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness import stack  # noqa: E402
from lib import generator, oracle  # noqa: E402

CORPUS = Path(__file__).resolve().parent / "corpus"


class PropertyFailure(Exception):
    def __init__(self, failures):
        super().__init__("; ".join(failures))
        self.failures = failures


# --------------------------------------------------------------- execution ----

def execute(scenario):
    """Run one scenario against the real service and collect observations."""
    stack.app_stop()
    t = scenario["tariff"]
    stack.mock_configure(t["mode"], t["value"], t["delay_ms"])
    stack.reset_db()
    stack.mock_reset()

    if scenario["fleet_setup"]:
        stack.sql("".join(scenario["fleet_setup"]))

    fleet_before = stack.read_fleet()
    connectors_before = stack.read_connectors()
    cp_count_before = int(stack.sql_value("SELECT COUNT(*) FROM charge_points"))

    stack.insert_frames(scenario["frames"])
    n = len(scenario["frames"])

    stack.app_start({"TARIFF_MODE": t["mode"], "TARIFF_VALUE": t["value"],
                     "TARIFF_DELAY_MS": t["delay_ms"],
                     "LOOKUP_URL": "http://tariff:8080",
                     "BATCH": "32", "POLL_INTERVAL_US": "50000"})
    quiesced = stack.wait_quiesce(timeout=120)

    inbox = stack.sql_rows(
        "SELECT id, status, IFNULL(cp_ident,'NULL') FROM inbox ORDER BY id")
    events = stack.sql_rows(
        "SELECT id, inbox_id, cp_id, IFNULL(CAST(utc_event_at AS CHAR),'NULL'),"
        " IFNULL(CAST(local_event_at AS CHAR),'NULL'),"
        " IFNULL(CAST(rollup_date AS CHAR),'NULL'),"
        " IFNULL(CAST(rollup_hour AS CHAR),'NULL') FROM meter_events ORDER BY id")
    obs = {
        "quiesced": quiesced,
        "n_inserted": n,
        "inbox": inbox,
        "events": events,
        "fleet_before": fleet_before,
        "fleet_after": stack.read_fleet(),
        "connectors_before": connectors_before,
        "connectors_after": stack.read_connectors(),
        "cp_count_before": cp_count_before,
        "cp_count_after": int(stack.sql_value("SELECT COUNT(*) FROM charge_points")),
        "requests": stack.mock_requests(),
        "logs": stack.app_logs(),
        "file_writes": stack.app_file_writes(),
        "app_state": stack.app_state(),
        "orphan_events": stack.sql_rows(
            "SELECT e.id FROM meter_events e LEFT JOIN charge_points c ON c.id=e.cp_id"
            " WHERE c.id IS NULL"),
        "orphan_events_inbox": stack.sql_rows(
            "SELECT e.id FROM meter_events e LEFT JOIN inbox i ON i.id=e.inbox_id"
            " WHERE i.id IS NULL"),
        "orphan_details": stack.sql_rows(
            "SELECT d.id FROM revision_details d LEFT JOIN revisions r ON r.id=d.rev_id"
            " WHERE r.id IS NULL"),
        "dup_events": stack.sql_rows(
            "SELECT inbox_id, COUNT(*) FROM meter_events GROUP BY inbox_id"
            " HAVING COUNT(*) > 1"),
        "bad_link_state": stack.sql_rows(
            "SELECT id, link_state FROM charge_points WHERE link_state NOT IN (0,1)"),
    }
    stack.app_stop()
    return obs


# --------------------------------------------------------------- properties ----

def check(scenario, obs):
    """Return a list of human-readable property violations."""
    f = []
    n = obs["n_inserted"]
    inbox = {int(r[0]): r[1] for r in obs["inbox"]}
    mine = {i: inbox.get(i) for i in range(1, n + 1)}
    synthetic = {i: s for i, s in inbox.items() if i > n}
    logs = obs["logs"]

    def is_token(status):
        return (status or "").startswith("c") and len(status or "") == 12

    # -- P1 no frame is left claimable
    if not obs["quiesced"]:
        f.append("P1 batch never quiesced")
    for i, status in mine.items():
        if status == "new":
            f.append(f"P1 frame {i} still 'new' after quiescence")

    # -- P2 terminal or stranded, and stranded implies a logged error
    stranded = [i for i, s in mine.items() if is_token(s)]
    for i, status in mine.items():
        if status not in (oracle.DONE, oracle.BAD) and not is_token(status):
            f.append(f"P2 frame {i} has unexpected status {status!r}")
    if stranded and "ERR" not in logs:
        f.append(f"P2 frames {stranded} stranded but no ERR line was logged")

    # -- P3 connectors is never written
    if obs["connectors_before"] != obs["connectors_after"]:
        f.append("P3 connectors table was modified")

    # -- P4 at most one reading per inbox row
    if obs["dup_events"]:
        f.append(f"P4 duplicate meter_events for inbox_id {obs['dup_events']}")

    # -- P5/P6 referential integrity
    if obs["orphan_events"]:
        f.append(f"P5 meter_events rows with unknown cp_id: {obs['orphan_events']}")
    if obs["orphan_events_inbox"]:
        f.append(f"P5 meter_events rows with unknown inbox_id: {obs['orphan_events_inbox']}")
    if obs["orphan_details"]:
        f.append(f"P6 revision_details rows with unknown rev_id: {obs['orphan_details']}")

    # -- P7 the service never inserts or deletes charge points
    if obs["cp_count_before"] != obs["cp_count_after"]:
        f.append(f"P7 charge_points count changed "
                 f"{obs['cp_count_before']} -> {obs['cp_count_after']}")

    # -- P8 stdout is the only write
    if obs["file_writes"]:
        f.append(f"P8 service wrote files: {obs['file_writes'][:5]}")

    # -- P9 flags bit 2 is set-only, never cleared
    for ident, before in obs["fleet_before"].items():
        after = obs["fleet_after"].get(ident)
        if after and (before["flags"] & 4) and not (after["flags"] & 4):
            f.append(f"P9 {ident} lost flags bit 2")

    # -- P10 link_state stays boolean
    if obs["bad_link_state"]:
        f.append(f"P10 link_state outside (0,1): {obs['bad_link_state']}")

    # -- P11 the service did not crash
    if obs["app_state"] != "running":
        f.append(f"P11 app container state is {obs['app_state']!r}")

    # -- P12..P18 per-frame classification and conversion oracle
    #
    # Batch poisoning: the per-frame catch at ingest.php:389 `return`s out of
    # processInboxBatch from inside the foreach, so the FIRST frame that throws
    # abandons every remaining frame in the same claimed batch. Those frames are
    # already stamped with the claim token and no worker will ever revisit them.
    # The oracle therefore tracks a poisoned flag rather than judging each frame
    # independently.
    now_utc = datetime.now(timezone.utc).replace(tzinfo=None)
    events_by_inbox = {}
    for row in obs["events"]:
        events_by_inbox.setdefault(int(row[1]), []).append(row)

    poisoned_at = None
    for idx, frame in enumerate(scenario["frames"], start=1):
        want = oracle.classify(frame, obs["fleet_before"], now_utc,
                               obs["connectors_before"])
        got_status = mine.get(idx)
        got_events = events_by_inbox.get(idx, [])

        if poisoned_at is not None:
            # Collateral: never processed, so it must still hold its token and
            # must not have produced a reading.
            if not is_token(got_status):
                f.append(f"P14 frame {idx} follows the throw at frame "
                         f"{poisoned_at} so it should be abandoned holding its "
                         f"claim token, got {got_status!r}")
            if got_events:
                f.append(f"P14 abandoned frame {idx} produced a reading")
            continue

        if want["status"] == oracle.STRAND:
            # Predicted to throw. Accept a token, and accept 'bad' too: the
            # throw may happen after the status write on some paths.
            if not is_token(got_status) and got_status != oracle.BAD:
                f.append(f"P12 frame {idx} ({want['reason']}) expected strand/bad, "
                         f"got {got_status!r}")
            if is_token(got_status):
                poisoned_at = idx
            continue

        if got_status != want["status"]:
            f.append(f"P12 frame {idx} expected {want['status']} "
                     f"({want['reason']}), got {got_status!r}")
            continue

        if want["status"] == oracle.BAD:
            if got_events:
                f.append(f"P13 rejected frame {idx} still produced a reading")
            continue

        # Accepted frame.
        if len(got_events) != want["events"]:
            f.append(f"P15 frame {idx} (model_code {want['model_code']}) expected "
                     f"{want['events']} reading(s), got {len(got_events)}")
            continue

        if want["events"] == 1:
            _, _, _, utc, local, rd, rh = got_events[0]
            if want["utc_event_at"] is not None and utc != want["utc_event_at"]:
                f.append(f"P17 frame {idx} utc_event_at expected "
                         f"{want['utc_event_at']} got {utc}")
            if want["utc_event_at"] is None and utc != "NULL":
                f.append(f"P17 frame {idx} expected NULL utc_event_at got {utc}")
            want_local = want["local_event_at"] or "NULL"
            if local != want_local:
                f.append(f"P18 frame {idx} local_event_at expected "
                         f"{want_local} got {local}")
            want_rd = want["rollup_date"] or "NULL"
            if rd != want_rd:
                f.append(f"P18 frame {idx} rollup_date expected {want_rd} got {rd}")

    # -- P19/P20 outbound lookup count and payload
    expected_paths = []
    predictable = True
    for frame in scenario["frames"]:
        want = oracle.classify(frame, obs["fleet_before"], now_utc,
                               obs["connectors_before"])
        if want["status"] == oracle.STRAND:
            # A throw can occur before or after the lookup; count becomes
            # unpredictable, so skip the HTTP properties for this scenario.
            predictable = False
        hit, path = oracle.predicts_lookup(frame, obs["fleet_before"])
        if hit:
            expected_paths.append(path)

    if predictable:
        if len(obs["requests"]) != len(expected_paths):
            f.append(f"P19 expected {len(expected_paths)} tariff request(s), "
                     f"got {len(obs['requests'])}: {obs['requests']}")
        elif sorted(obs["requests"]) != sorted(expected_paths):
            f.append(f"P20 tariff payload mismatch: expected "
                     f"{sorted(expected_paths)} got {sorted(obs['requests'])}")

    # -- P21 synthetic fan-out rows are always pre-marked done
    for i, status in synthetic.items():
        if status != oracle.DONE:
            f.append(f"P21 synthetic inbox row {i} has status {status!r}, not done")

    return f


# ------------------------------------------------------------------ driver ----

def run_scenario(scenario):
    obs = execute(scenario)
    failures = check(scenario, obs)
    if failures:
        raise PropertyFailure(failures)


def shrink_failure(scenario, rng, failures, budget=12):
    """
    Greedily reduce while the scenario still fails. Returns the smallest
    scenario AND the failures *it* produced -- not the original's, which would
    describe frame indices that no longer exist.
    """
    best, best_failures = scenario, failures
    tried = 0
    improved = True
    while improved and tried < budget:
        improved = False
        for candidate in generator.shrink(best, rng):
            if tried >= budget:
                break
            tried += 1
            try:
                run_scenario(candidate)
            except PropertyFailure as exc:
                best, best_failures = candidate, exc.failures
                improved = True
                break
            except Exception:
                continue
    return best, best_failures, tried


def save(scenario, seed, failures, tag="counterexample"):
    CORPUS.mkdir(exist_ok=True)
    path = CORPUS / f"{tag}-seed{seed}-{int(time.time())}.json"
    payload = dict(scenario)
    payload["_seed"] = seed
    payload["_failures"] = failures
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n")
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenarios", type=int, default=30)
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--replay", type=str, default=None)
    ap.add_argument("--no-shrink", action="store_true")
    ap.add_argument("--keep-up", action="store_true")
    args = ap.parse_args()

    stack.configure(os.environ.get("PROP_PROJECT", "evse-prop"),
                    Path(__file__).resolve().parent / ".env.runtime")

    seed = args.seed if args.seed is not None else random.randrange(1, 2**31)
    print(f"property suite: seed={seed}")

    print("bringing up disposable stack...")
    stack.up(build=True)
    stack.build_app()

    failures_total = []
    try:
        if args.replay:
            scenario = json.loads(Path(args.replay).read_text())
            scenario.pop("_seed", None)
            scenario.pop("_failures", None)
            print(f"replaying {args.replay}")
            try:
                run_scenario(scenario)
                print("  replay PASSED (no longer reproduces)")
            except PropertyFailure as exc:
                print("  replay FAILED:")
                for line in exc.failures:
                    print(f"    {line}")
                failures_total.append(args.replay)
        else:
            # Replay every saved counterexample BEFORE generating new ones, so a
            # known failure is a hard gate rather than something a future random
            # run might rediscover by luck. This is what a real PBT framework's
            # example database does automatically.
            saved = sorted(CORPUS.glob("*.json"))
            if saved:
                print(f"replaying {len(saved)} saved counterexample(s) first")
                for path in saved:
                    scenario = json.loads(path.read_text())
                    scenario.pop("_seed", None)
                    scenario.pop("_failures", None)
                    print(f"  {path.name[:50]:52}", end="", flush=True)
                    try:
                        run_scenario(scenario)
                        print("ok")
                    except PropertyFailure as exc:
                        print("FAIL")
                        for line in exc.failures:
                            print(f"      {line}")
                        failures_total.append(f"corpus:{path.name}")
                print()

            rng = random.Random(seed)
            for i in range(1, args.scenarios + 1):
                scenario = generator.generate_scenario(rng)
                label = (f"  scenario {i:>3}/{args.scenarios} "
                         f"({len(scenario['frames'])} frame(s), "
                         f"tariff={scenario['tariff']['mode']})")
                print(label.ljust(60), end="", flush=True)
                try:
                    run_scenario(scenario)
                    print("ok")
                except PropertyFailure as exc:
                    print("FAIL")
                    for line in exc.failures:
                        print(f"      {line}")
                    path = save(scenario, seed, exc.failures)
                    print(f"      saved: {path.name}")
                    if not args.no_shrink:
                        print("      shrinking...", end="", flush=True)
                        small, small_failures, tried = shrink_failure(
                            scenario, rng, exc.failures)
                        print(f" {tried} candidate(s) -> "
                              f"{len(small['frames'])} frame(s)")
                        for line in small_failures:
                            print(f"      minimal: {line}")
                        spath = save(small, seed, small_failures, tag="shrunk")
                        print(f"      saved: {spath.name}")
                    failures_total.append(f"scenario {i}")
    finally:
        if not args.keep_up:
            stack.down()

    print()
    print(f"failing scenarios: {len(failures_total)}")
    return 1 if failures_total else 0


if __name__ == "__main__":
    sys.exit(main())
