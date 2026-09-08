# Characterization suite

Pins the externally observable behavior of `evse-ingest`. For *when* to run this
and how to handle a moved baseline, read [`../README.md`](../README.md) first —
it covers the refactor and bugfix workflows. This file documents the mechanics.

## Layout

```
characterization/
  run.sh                     runner: verify | record | list
  lib/harness.sh             black-box observation helpers
  docker-compose.test.yml    disposable stack: db + tariff mock + service
  mock/tariff_service.py     recording stand-in for the rate service
  cases/*.json               one file per case -- data, not code
  baselines/*.snapshot       recorded expected output, committed
  .results/                  last run's snapshots and diffs (not committed)
```

## How a case runs

For each case, in order:

1. Stop the service container, so nothing is processed at an unpredictable time.
2. Reset the database by replaying the real `sql/schema.sql`, then snapshot
   pristine copies of `charge_points` and `connectors` for later diffing.
3. Clear the tariff mock's request log and reconfigure its behavior.
4. Apply the phase's `sql`, then insert its `frames` into `inbox` as the upstream
   intake layer would — while the service is still stopped, so batch composition
   and therefore id allocation are deterministic.
5. Start the service and **wait for quiescence**.
6. Emit a snapshot of every observable edge.
7. After all phases, evaluate the case's declared assertions.
8. Compare against the baseline, or overwrite it in `--record` mode.

### Quiescence

The stop condition is not "all frames reached a terminal status" — stranded
frames never do (see case 20). Instead the harness waits until nothing claimable
remains **and** the full observable state has been stable for three consecutive
polls. This is what makes "assert at the end of a unit of processing" meaningful
rather than a race.

## Case schema

```jsonc
{
  "description": "one line, shown by --list and written into the baseline header",
  "documents": ["docs/known-issues.md#..."],   // provenance for the behavior

  "env": {                                    // per-case stack configuration
    "LOOKUP_URL": "http://tariff:8080",       // unset => no outbound call
    "TARIFF_MODE": "ok",                      // ok|empty|malformed|no_field|http500|reset
    "TARIFF_VALUE": "9",                      // value placed at now.v
    "TARIFF_DELAY_MS": "3000",                // server-side delay
    "BATCH": "8", "POLL_INTERVAL_US": "50000",
    "SHARD": "0", "SHARDS": "1"
  },

  "phases": [                                 // each phase is one unit of processing
    {
      "label": "shown in the snapshot",
      "timeout": 45,                          // seconds to wait for quiescence
      "sql": ["UPDATE charge_points SET tariff='PEAK' WHERE id=1"],
      "frames": [
        {
          "src": "cp",                        // 'gw' marks a gateway-sourced frame
          "cp_ident": "CP-0001",
          "received_at": "2021-03-04 10:00:00",
          "body": { "1": "CP-0001", "msg_type": 1, "wh": 1234 }
        }
      ]
    }
  ],

  "assert": {                                 // evaluated after the last phase settles
    "tariff_request_count": 1,
    "tariff_requests_include": ["GET /r/41.88/-87.63"],
    "stdout_contains": ["ERR"],
    "stdout_excludes": ["FATAL"],
    "min_duration_ms": 3000,
    "app_state": "running",
    "sql": [{ "query": "SELECT COUNT(*) FROM meter_events", "value": "1" }]
  }
}
```

Use `"raw_body"` instead of `"body"` to send bytes that are not valid JSON
(case 21). `body_hash` is always computed as `SHA1(body)` by the harness, exactly
as the upstream intake layer is assumed to do.

## Snapshot format

Plain text, designed to diff cleanly. Per phase:

- `inbox`, `meter_events`, `revisions`, `revision_details` — every row, ordered
  by id, with a column-name header
- `charge_points` and `connectors` — only rows that **differ from the pristine
  seed**, plus insert and delete counts. A 22-row fleet would otherwise drown the
  diff, and this way an unexpected write to an unrelated row still shows up
- recorded tariff requests, verbatim
- service stdout, with `[<pid>]` normalised to `[PID]`
- files the service wrote inside its container, via `docker diff`
- final container state

### Masked values

Three values are non-deterministic and are replaced so snapshots are byte-stable:

| Mask | Replaces |
|---|---|
| `<CLOCK>` | any datetime within 15h of now, i.e. clock-derived rather than fixture-derived |
| `<TOKEN>` | an in-flight `inbox.status` claim token, matched as `^c[0-9a-f]{11}$` |
| `[PID]` | the worker's process id in its log prefix |

The `<CLOCK>` window is 15 hours, not seconds, and deliberately so: the service
writes some columns in the charge point's **site-local** time — `inbox.received_at`
on a `msg_type 3` frame — while the comparison runs in the database's timezone.
Anything narrower makes case 22 flap. All fixture timestamps live in 2021 or 2099,
years outside the window, so they are never masked. This is why cases must not use
timestamps near the present day.

## Deliberate deviations from production

Documented here because a characterization suite that silently diverges from the
real deployment is worthless.

| Deviation | Why |
|---|---|
| MySQL data on `tmpfs` | Makes the database genuinely disposable; every `up` starts clean |
| `app` has `restart: "no"` | Production uses `unless-stopped`, which would mask a crash. The suite must be able to observe one, and asserts container state |
| `db` pinned to `--default-time-zone=UTC` | Removes host-timezone influence on `NOW()`, which feeds `revisions.at_` |
| `BATCH` 32, `POLL_INTERVAL_US` 50000 | Larger batch keeps a case's frames in one claim, so ids are deterministic; shorter poll keeps runs fast |
| One worker, never scaled | Concurrency would make id allocation non-deterministic |
| Tariff service is a local mock | The real dependency is a third party |

Everything else — the image, the schema, the seed data, the entry point,
credentials, and the claim query — is the real thing.

## Coverage

24 cases. Ordered roughly from simple to pathological.

| Case | Pins |
|---|---|
| 01 | Happy path, Chicago→UTC conversion, duplicated `rollup_at` audit rows |
| 02 | `rd`/`rh` time source, Denver conversion, `rollup_at` never advancing |
| 03 | Link-state toggling, monotonic `flags` bit, control frames writing readings |
| 04, 05 | Rejected frames still bumping `last_seen_at`, and the `src='gw'` asymmetry |
| 06, 07, 21 | The three rejection paths: future timestamp, unknown ident, non-JSON body |
| 08 | Replay idempotency, which is what makes strand recovery safe |
| 09, 10, 11 | The multi-connector defects: zero readings, `last_event_id` corruption, `m.zd` suppression |
| 12–15 | The outbound HTTP call: exact payload, and the four alert-gate outcomes |
| 16 | Absence of an HTTP client timeout, via a lower-bound duration assertion |
| 17 | Silent degradation on a 500 from the rate service |
| 18 | Diagnostic-blob synthesis, including the trailing space left by a discarded `rtrim` |
| 19 | Settlement flag, gateway flag, firmware major-version truncation |
| 20 | Exception mid-frame stranding a row with its claim token, and the `ERR` log line |
| 22 | Rewriting `inbox.received_at`, a column owned upstream |
| 23 | A mixed batch asserted as one unit of processing |
| 24 | `connectors` never being written, including a retired connector |

## Known fragilities

- **Case 20 asserts on an exception message** produced by the language runtime
  when given an unresolvable timezone. A runtime upgrade may reword it; the
  baseline would then drift for an uninteresting reason. The assertion itself
  only requires the substring `ERR`.
- **Ids are positional.** Inserting a frame into an existing phase renumbers
  everything after it and moves several baselines. Prefer adding a new case.
- **Baselines encode DST.** Fixtures sit in March 2021, so Chicago is UTC−6 and
  Denver UTC−7. Moving a fixture across a DST boundary changes the expected UTC
  values, correctly.

## Troubleshooting

**`FAIL (no quiesce)`** means the batch never settled. In practice this almost
always means the service failed to start rather than that it hung — a syntax
error in the source, or a failed image build. Check it directly:

```bash
./run.sh --keep-up '01-*'          # leave the stack up
docker compose -p evse-charz -f docker-compose.test.yml logs app
```

A genuine hang looks different: the container is `running` and stdout is quiet.
The most likely cause is an outbound tariff call with no timeout (see case 16),
in which case raise the phase `timeout`.

**Every case failing at once** usually means a stale image. The runner rebuilds
`app` on each invocation, but a build that fails leaves the previous image in
place; re-run and read the build output.

## Verifying the gate itself

The suite is only worth having if it actually catches behavior changes. Both
directions were checked against real edits to `src/`:

| Edit | Kind | Suite result |
|---|---|---|
| Rename the dead local `$processed_count` to `$processedCount` | pure refactor | **green** — cases 01 and 03 unchanged |
| Change `flags = flags \| 4` to `flags \| 8` | behavior change | **red** — case 03 drifted, showing `flags` 4→8 in both phases |
| Introduce a syntax error | broken build | **red** — `FAIL (no quiesce)`, no false green |

Re-run those checks after any substantial change to the harness. A harness that
cannot fail is worse than no harness.
