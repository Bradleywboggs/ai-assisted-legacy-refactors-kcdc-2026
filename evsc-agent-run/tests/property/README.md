# Property suite

Generative testing for `evse-ingest`. Complements the golden-baseline
[characterization suite](../characterization/README.md): that one pins *known*
inputs exactly, this one explores *unknown* inputs and checks properties that
must hold for all of them.

Read [`../README.md`](../README.md) for the refactor and bugfix workflows. Both
suites are regression gates and both must be green before a change lands.

---

## Why generated inputs rather than a production sample

**No production-quality data exists to sample.** This was checked, not assumed:

| Source | Result |
|---|---|
| Repository | `sql/schema.sql` only — 16 seeded charge points, 3 connectors, no frames |
| Any data file (`*.csv`, `*.json`, `*.dump`, `*.ndjson`, `*.parquet`) | none |
| Config pointing at an off-box database | none; `DB_HOST` defaults to `127.0.0.1` and compose points at a local container |
| Sibling MySQL containers on the same daemon | two found, each holding **36** synthetic `inbox` rows with byte-identical counts — hand-built probes, not captured traffic |

Thirty-six hand-written frames is not a substantial sample, and identical row
counts across two independent stacks confirm they were script-generated rather
than observed. So the input space is **reconstructed from usage** instead.

That reconstruction is [`lib/domains.py`](lib/domains.py). Every domain cites the
line in `src/ingest.php` that motivates its values — `msg_type` draws from the
literals the dispatch actually compares against, `nt` draws markers at offset 0
*and* at a later offset because one call site omits `!== false`, coordinates draw
the shapes that get interpolated into the lookup URL, and so on. The goal is to
cover the branches that exist rather than a space someone imagined.

The sibling fixtures were not wasted: inspecting them **corroborated the field
vocabulary** (every key they use appears in the documented field table) and
surfaced three input shapes worth generating that the characterization suite had
not covered — an unparseable `la`, a JSON-null `m.zd`, and a caller-supplied
`dt`. All three are now in the domains.

---

## Running it

```bash
cd tests/property

./property_test.py                          # 30 scenarios, random seed
./property_test.py --scenarios 100          # longer run
./property_test.py --seed 20260907          # reproduce a run exactly
./property_test.py --replay corpus/x.json   # re-run a saved counterexample
./property_test.py --no-shrink              # skip shrinking
./property_test.py --keep-up                # leave the stack up
```

Requirements: `docker`, `python3` (stdlib only — `zoneinfo` supplies the
timezone oracle). Roughly 19 seconds per scenario, dominated by the database
reset and worker restart.

Reproducibility is the point: the seed is printed on every run, and every
failure is written to `corpus/` with its seed so it can be replayed verbatim.

---

## What it checks

Two kinds of assertion, deliberately separated.

### Invariants — hold for any input, no model of the service required

| # | Property |
|---|---|
| P1 | The batch quiesces and no frame is left claimable |
| P2 | Every frame ends `done`, `bad`, or holding a claim token; a token implies an `ERR` log line |
| P3 | `connectors` is never written |
| P4 | No two `meter_events` rows share an `inbox_id` |
| P5 | Every `meter_events.cp_id` and `inbox_id` resolves |
| P6 | Every `revision_details.rev_id` resolves |
| P7 | `charge_points` is never inserted into or deleted from |
| P8 | The service writes no files — stdout only |
| P9 | `flags` bit 2 is set-only, never cleared |
| P10 | `link_state` stays within `{0,1}` |
| P11 | The service does not crash |
| P21 | Synthetic fan-out rows are always pre-marked `done` |

### Narrow oracles — independent predictions of small, exactly-stated contracts

| # | Property |
|---|---|
| P12 | Frame classification: non-JSON, non-object, missing `1`, unknown ident, and `la` beyond now+2d all yield `bad` |
| P14 | **Batch poisoning**: every frame after the first throw is abandoned holding its claim token |
| P15 | An accepted frame yields exactly one reading on `model_code != 7`, and zero on `model_code == 7` |
| P17 | `utc_event_at` equals the frame's time source converted from the unit's `tz`, computed independently with `zoneinfo` |
| P18 | `local_event_at` is populated only from `la`; `rollup_date`/`rollup_hour` only from `rd`/`rh` |
| P19 | The number of outbound lookups matches the prediction from tariff state, marker position, and `OVERHEAT` |
| P20 | Every recorded request path equals `/r/{m.la}/{m.lo}` with the documented `0` defaults |

P19 and P20 are how the "payloads must be preserved and checked" requirement is
met over generated input: the mock records every request verbatim and the oracle
predicts both the count and the exact paths from the input alone.

### Deliberately not modelled

The audit trail, the parent-row update set, and the multi-connector revision
fan-out. Re-implementing those in the oracle would clone the service's defects
into the oracle and then assert the two copies agree, which proves nothing.
Characterization baselines pin them instead. This division is the reason both
suites exist.

### Fields deliberately not generated

Listed with rationale in `domains.EXCLUDED_FIELDS`:

- **`md`** rewrites `model_code`, the branch discriminator, mid-batch.
- **`rt`** rewrites `tariff`, changing whether *later* frames perform a lookup.
- **a numeric `"1"`** makes MySQL coerce the `VARCHAR` `cp_ident` column to a
  number, so `0` matches every row. That deserves a dedicated characterization
  case rather than random exploration — it is a genuine hazard, currently
  uncovered.
- **an empty-string `la`** parses as "now" in PHP, making `utc_event_at`
  clock-derived and unpredictable.

---

## What it found

Ten scenarios into the first real run, the suite produced a counterexample that
the 24 characterization cases had all missed:

```
scenario   3/10 (4 frame(s), tariff=http500)              FAIL
      P12 frame 4 expected done (accepted), got 'c49041e7a63a'
      shrinking... 7 candidate(s) -> 2 frame(s)
```

Shrunk to two frames: an unparseable `la` followed by a perfectly valid frame.
The valid frame was stranded. The cause is `ingest.php:389-400` — the per-frame
`catch` block `return`s out of `processInboxBatch` from **inside** the `foreach`,
so the first frame that throws abandons every remaining frame in the same
claimed batch. Those frames already carry the claim token, so no worker ever
revisits them.

With the production default `BATCH=4` one malformed frame can strand three
innocent ones; the suite's `BATCH=32` makes it thirty-one. This is recorded as
[known issue 14](../../docs/known-issues.md#14-one-malformed-frame-strands-the-rest-of-its-batch),
promoted into the characterization suite as case 25, and modelled by P14.

It also retroactively explains an artefact in the sibling fixture data: two rows
there share the claim token `c32aaa9df8bb`, one with `"la":"banana"` and one with
an unknown identifier. The second would have been marked `bad` had it ever been
reached. Independent corroboration from a party that never noticed it.

Every case the characterization suite covers uses a single frame per phase, which
is exactly why it could not see this. Generated multi-frame batches could.

---

## Shrinking

On failure the driver greedily reduces the scenario while it still fails: drop
each frame, drop the front or back half, drop the fleet perturbation. Both the
original and the minimal reproducer are written to `corpus/`, each annotated with
the failures **it** produced.

The budget is 12 executions (~4 minutes). Raise it in `shrink_failure` if a
reproducer is still too large to read.

**Known weakness: shrinking removes whole frames but never simplifies within
one.** It can drop a frame, drop half the batch, or drop the fleet
perturbation; it cannot strip irrelevant keys from a frame body. The minimal
reproducer for known issue 15 still carried `msg_type`, `dt`, `nt` and `wh`
when only `as` and the provisioned connector rows actually mattered. A real
property-testing framework shrinks within values and would have produced a
smaller example. Budget reading time accordingly.

---

## Saved counterexamples are a gate

Every file in [`corpus/`](corpus/) is replayed **before** any new scenario is
generated, so a resolved failure cannot quietly return — a future random run
rediscovering it by luck is not a gate. Corpus failures are reported separately
from generated-scenario failures and fail the run.

This mirrors what a real framework's example database does automatically. It was
missing from the first version of this suite: the replay flag and the file
format existed, but nothing replayed them, so the corpus was documentation
rather than a test.

Retire a file once its behavior is pinned by a characterization case — that case
runs on every `make test`, whereas a corpus entry is only as good as the replay.
[`corpus/README.md`](corpus/README.md) records what has been retired and why.

---

## Promoting a counterexample

A property failure is transient knowledge until it becomes a fixed test. The
workflow:

1. Read the shrunk scenario in `corpus/`.
2. Confirm the cause in the source, and check whether it is new.
3. Write it as a characterization case in
   [`../characterization/cases/`](../characterization/cases/), translating
   `fleet_setup` to the case's `phases[].sql` and `frames` to `phases[].frames`.
4. Record its baseline: `cd ../characterization && ./run.py --record 'NN-*'`.
5. Document it in [`docs/known-issues.md`](../../docs/known-issues.md) and add a
   gotcha to [`AGENTS.md`](../../AGENTS.md).
6. If the oracle was wrong rather than the service, fix the oracle and say so in
   its docstring.

Case 25 was produced by exactly this path and can be used as the worked example.

---

## Interpreting a failure

A property failure means one of three things, in decreasing likelihood:

| Cause | How to tell | Action |
|---|---|---|
| The oracle is wrong | The observed behavior is defensible on reading the source | Fix `lib/oracle.py`, note it in the docstring |
| A genuine undocumented behavior | Source confirms it and no document mentions it | Promote to a characterization case and document |
| A real regression | The behavior contradicts a recorded baseline | The characterization suite will also be red; fix the code |

Check the characterization suite first when both are red — it localises faster
because its expected values are concrete.

---

## Known limitations

- **Single worker.** Concurrency would make id allocation non-deterministic and
  break the per-frame oracle. Multi-worker claim safety is argued from
  `SELECT ... FOR UPDATE SKIP LOCKED`, not tested.
- **No coverage measurement.** There is no instrumentation, so "did this run
  reach the fan-out branch" is inferred from generated shapes, not measured.
- **Fixed fixture epoch.** Timestamps live in 2021 and 2099 so that nothing is
  clock-derived. A domain value near the present day would make `utc_event_at`
  unpredictable.
- **`STRAND` prediction is curated, not derived.** PHP's `DateTime` parser is
  permissive in surprising ways, so `LA_UNPARSEABLE` holds values verified to
  throw rather than anything the oracle reasons about.
