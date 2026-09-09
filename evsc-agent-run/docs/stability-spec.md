# Specification

What must remain true about `evse-ingest` no matter what else changes.

Derived from the source, the 26 characterization cases, and the property suite's
invariants. Every requirement carries a **status** and **evidence**, because a
specification in which everything already holds would hide exactly the risks
worth knowing about.

---

## 0. How to read this

**Stability, here, means three things:** the database contract this service
publishes stays honest; telemetry that enters the system is not lost or
double-counted; and a bad input cannot damage a good one.

There is no network API. **The database is the interface**, in both directions —
an upstream intake layer writes `inbox`, and reporting and settlement read the
five tables this service owns. Neither side has a contract beyond raw table
shape, so a requirement broken here surfaces as wrong money or a missing
dashboard, not as a failed request.

Keywords per RFC 2119: **MUST**, **MUST NOT**, **SHOULD**, **MAY**.

Status values:

| Status | Meaning |
|---|---|
| **HOLDS** | Verified by an automated test or property |
| **CONSTRUCTION** | Argued from code structure; not directly exercised by a test |
| **AT RISK** | True today only because of an unrelated accident, not by design |
| **VIOLATED** | Demonstrably broken. Cites the defect |

> **This document is not a description of current behavior.** Present behavior
> that must *not* be treated as a requirement is in [section 4](#4-defects-and-hazards).
> Sections 1 and 2 are the contract; section 4 is the debt.

### Summary

| | Total | HOLDS | CONSTRUCTION | AT RISK | VIOLATED |
|---|---|---|---|---|---|
| Functional (F1–F7) | 37 | 27 | 2 | 3 | 5 |
| Non-functional (N1–N8) | 38 | 15 | 8 | 4 | 11 |
| **Total** | **75** | **42** | **10** | **7** | **16** |

**Sixteen requirements are violated.** Five of those are data-loss class —
F2.2, N2.1, N2.2, N2.3, N6.2 — and they share just two root causes:

1. **The per-frame error handler `return`s out of the batch loop instead of
   continuing** (`:397`, `:400`), so one bad frame abandons every later frame in
   its batch.
2. **No lease expiry or reaper exists**, so an abandoned frame is unreachable
   forever rather than merely delayed.

Fixing both closes all five. Neither touches anything in
[section 4.3](#43-questionable-behavior-that-may-be-depended-upon), so neither
requires a cross-team conversation first. That is the highest-value work
available on this codebase.

A further ten requirements are marked CONSTRUCTION, meaning **nothing mechanical
enforces them** — they are argued from reading the code and can regress in
silence. Eight of the ten are non-functional (concurrency, resources, startup),
which is the predictable blind spot of a suite that runs one worker against a
clean database.

---

## 1. Functional requirements

### F1 — Claim and lease

The lease is the foundation of everything else. If it breaks, idempotency and
exactly-once accounting break with it.

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F1.1 | Two workers MUST NOT hold the same `inbox` row simultaneously | CONSTRUCTION | `SELECT … FOR UPDATE SKIP LOCKED` (`:34-38`) then a status CAS to a per-claim token (`:41`) |
| F1.2 | The claim MUST be committed before any decoding begins | HOLDS | `claimBatch` commits at `:43`, before `processInboxBatch` inspects any body |
| F1.3 | The claim token MUST be unique per claim and MUST NOT collide with any terminal status | HOLDS | `'c'` + 11 hex from `random_bytes` (`:30`); never equals `new`/`done`/`bad` |
| F1.4 | Only rows with `status = 'new'` MUST be claimed | HOLDS | Case 08; claim predicate at `:36` |
| F1.5 | A worker MUST re-read only its own claimed rows | HOLDS | Re-read by token at `:45` |

**Why F1.2 is load-bearing:** were the lease not durable before decode, a crash
mid-frame would leave the row `new` *and* possibly leave a `meter_events` row
behind. The next claim would then double-count it. Durable-lease-then-work is
what makes the idempotency guard (F3.1) sufficient rather than merely likely.

### F2 — Classification and terminality

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F2.1 | An undecodable frame MUST be marked `bad`, not retried forever | HOLDS | Cases 04, 06, 07, 21; property P12 |
| F2.2 | **Every claimed frame MUST reach `done` or `bad`** | **VIOLATED** | [Issue 5](known-issues.md#5-stranded-frames-are-unrecoverable-without-manual-sql), [issue 14](known-issues.md#14-one-malformed-frame-strands-the-rest-of-its-batch); cases 20, 25, 26 |
| F2.3 | A rejected frame MUST NOT produce a reading | HOLDS | Property P13; cases 04–07, 21 |
| F2.4 | Classification MUST be deterministic given the frame and the fleet state | HOLDS | Three consecutive identical suite runs |
| F2.5 | An unknown `cp_ident` MUST be rejected, never auto-provisioned | HOLDS | Case 07; property P7 |

### F3 — Event durability and idempotency

This is the settlement contract. Violations here are billing errors.

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F3.1 | At most one `meter_events` row MUST exist per `inbox_id` | **AT RISK** | Property P4 passes, but see [latent bug L1](#l1--idempotency-has-no-database-backstop) |
| F3.2 | Replaying a terminal frame MUST NOT create a second reading | HOLDS | Case 08; guard at `:70` |
| F3.3 | An accepted frame on `model_code != 7` MUST produce exactly one reading | HOLDS | Property P15; cases 01, 02, 19 |
| F3.4 | `meter_events` MUST be append-only | HOLDS | No `UPDATE`, `DELETE`, `TRUNCATE`, or `REPLACE` against it anywhere in the codebase |
| F3.5 | Every reading MUST reference a real charge point and a real inbox row | HOLDS | FKs `fk_ev_cp`, `fk_ev_inbox`; property P5 |
| F3.6 | The service MUST NOT delete any row it did not create | HOLDS | No `DELETE` statement exists |

### F4 — Normalization

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F4.1 | `utc_event_at` MUST be the event time in UTC, converted from the unit's own timezone | HOLDS | Property P17 recomputes it independently with `zoneinfo`; cases 01, 02 |
| F4.2 | Timezone resolution MUST fall back to a fixed default when `tz` is null or empty | HOLDS | `America/Chicago` at `:127` |
| F4.3 | An unresolvable timezone MUST NOT silently yield a wrong timestamp | HOLDS | It throws (`:139`) — correct in isolation, though it then trips F2.2. Case 20 |
| F4.4 | `local_event_at` MUST remain the unconverted site-local value | HOLDS | Case 01; property P18 |
| F4.5 | DST MUST be observed rather than a fixed offset applied | HOLDS | Fixtures in March 2021 yield UTC−6 for Chicago, UTC−7 for Denver |
| F4.6 | Conversion MUST be a pure function of the frame and `charge_points.tz` | AT RISK | Holds today; would break the moment any timestamp derives from the process clock. See [Q7](#q7--two-clocks-feed-the-same-tables) |

**Why F4.6 matters:** reporting buckets energy by `utc_event_at`. If that value
ever depended on when the worker happened to run rather than on when the event
happened, replay and backfill would produce different numbers from the original
run, and the two would be indistinguishable after the fact.

### F5 — Audit trail

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F5.1 | Every `revision_details` row MUST reference a valid header | HOLDS | FK `fk_rdt_rev`; property P6 |
| F5.2 | Audit rows MUST NOT be deleted or rewritten | HOLDS | Insert-only; no `UPDATE`/`DELETE` |
| F5.3 | **The audit trail MUST NOT record a change that was not persisted** | **VIOLATED** | [Issue 2](known-issues.md#2-multi-connector-parent-rows-are-never-updated-except-on-msg_type-3): model-7 frames write details while the row is never updated. Cases 09, 12 |
| F5.4 | **The audit trail MUST NOT double-count a single change** | **VIOLATED** | [Issue 13](known-issues.md#13-every-rollup-advance-writes-two-identical-audit-rows); case 01 asserts the duplicate |
| F5.5 | A revision MUST identify its target unambiguously | HOLDS | `tname` + `target_id`; always `'ChargePoint'` and a `cp_ident` |

**F5.3 and F5.4 together mean the audit trail is currently not a reliable
reconstruction of history.** Anyone replaying `revision_details` to rebuild
`charge_points` state will diverge from the actual table. That is the practical
cost of these two entries, and it is invisible without doing the replay.

### F6 — Ownership boundaries

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F6.1 | The service MUST NOT insert or delete `charge_points` rows | HOLDS | Property P7 |
| F6.2 | The service MUST NOT write the `connectors` table | HOLDS | Property P3; case 24. One SELECT in the entire codebase |
| F6.3 | The service MUST NOT add foreign keys from `inbox` | HOLDS | Upstream may land frames for unprovisioned units |
| F6.4 | Writes to `inbox` SHOULD be confined to status transitions | **VIOLATED** | It also rewrites `received_at` (`:161`, case 22) and inserts synthetic rows (`:218`, case 09). See [Q1](#q1--the-service-manufactures-rows-in-someone-elses-table) |
| F6.5 | Columns that downstream reads MUST NOT change type or meaning without coordination | CONSTRUCTION | No schema-version marker exists; enforcement is social |

### F7 — Outbound dependency

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| F7.1 | The tariff lookup MUST be optional; an unset `LOOKUP_URL` MUST NOT fail frames | HOLDS | Cases 01–11 run with it unset |
| F7.2 | A failing rate service MUST NOT corrupt state or lose frames | HOLDS | Case 17 (HTTP 500 degrades silently to `done`) |
| F7.3 | **A slow rate service MUST NOT block a worker indefinitely** | **VIOLATED** | [Gotcha 13](../AGENTS.md); no `CURLOPT_TIMEOUT`. Case 16 proves the worker waits |
| F7.4 | The request payload MUST remain `GET {LOOKUP_URL}/r/{lat}/{lon}` | HOLDS | Property P20; case 12 asserts the exact path |
| F7.5 | The lookup MUST NOT be on the critical path for accepting a frame | AT RISK | A frame is still accepted when the lookup fails, but the call happens inside the write transaction |

---

## 2. Non-functional requirements

### N1 — Concurrency

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N1.1 | Adding workers MUST NOT duplicate work or require coordination beyond the database | CONSTRUCTION | `SKIP LOCKED` + token CAS |
| N1.2 | Workers MUST NOT deadlock each other under normal operation | CONSTRUCTION | Deadlock is detected by substring match (`:391`) and treated as retryable |
| N1.3 | Per-charge-point ordering MUST hold only when sharding is configured | HOLDS | `CRC32(cp_ident) % SHARDS` (`:36`); undocumented in the README |
| N1.4 | A worker MUST NOT assume it is the only worker | CONSTRUCTION | No global state, no lock files |

### N2 — Failure containment

The weakest area of the system.

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N2.1 | **A defective frame MUST NOT affect any other frame** | **VIOLATED** | [Issue 14](known-issues.md#14-one-malformed-frame-strands-the-rest-of-its-batch); case 25 shows three valid frames destroyed by one bad one |
| N2.2 | **Process termination MUST NOT lose acknowledged work** | **VIOLATED** | No signal handling; `SIGTERM` mid-frame strands the batch |
| N2.3 | A crash MUST be recoverable without manual intervention | **VIOLATED** | Recovery requires a manual `UPDATE inbox SET status='new' …` |
| N2.4 | Partial writes MUST NOT be visible | HOLDS | One transaction per frame; commit at `:387` |
| N2.5 | Rollback MUST NOT leave the row in a state that implies success | AT RISK | Rollback discards the `bad` write too, which is why frames strand rather than reject |
| N2.6 | Database unavailability MUST NOT corrupt state | CONSTRUCTION | `ERRMODE_EXCEPTION` plus transactional writes; no reconnect logic, so the process must die and be restarted |

### N3 — Observability

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N3.1 | Diagnostics MUST go to stdout so the container runtime captures them | HOLDS | `say()` writes `STDOUT` (`db.php:69`); every case snapshots it |
| N3.2 | Log lines MUST identify the emitting process | HOLDS | `[<pid>]` prefix |
| N3.3 | **Every lost or abandoned frame MUST produce a diagnostic** | **VIOLATED** | Only the throwing frame logs `ERR`; its collateral is silent (case 25) |
| N3.4 | Frame rejection SHOULD be observable | **VIOLATED** | `bad` transitions log nothing at all (cases 04–07) |
| N3.5 | The service MUST NOT write files | HOLDS | Property P8; `docker diff` empty in all 26 baselines |
| N3.6 | Configuration in effect SHOULD be observable at startup | **VIOLATED** | Nothing is logged; a misconfigured worker is indistinguishable from a correct one |

### N4 — Resources

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N4.1 | Connections per worker MUST remain bounded and small | HOLDS | Exactly two, opened once per batch call |
| N4.2 | Worker count MUST stay within the server connection budget | CONSTRUCTION | 2 × workers ≤ `max_connections` (observed 151), so ~75 workers |
| N4.3 | **A transaction MUST NOT stay open across a network call** | **VIOLATED** | The tariff lookup runs inside the frame transaction, holding row locks |
| N4.4 | Memory MUST NOT grow with the number of frames processed | CONSTRUCTION | Per-frame state is rebuilt each iteration |
| N4.5 | Log volume MUST be bounded | AT RISK | Rotation is a property of the host Docker daemon, not of this repo |

### N5 — Configuration

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N5.1 | All configuration MUST come from the environment | HOLDS | `getenv` only; no `$_ENV`, no config file |
| N5.2 | Every variable MUST have a documented default, or fail loudly | **VIOLATED** | `DB_USER`/`DB_PASS` silently default to `ingest_svc`; `LOOKUP_URL`, `SHARD`, `SHARDS` are undocumented |
| N5.3 | Integer configuration MUST be coerced before use in SQL | HOLDS | `(int)` casts at `:31-32`, `:59` — the only defence against injection in the claim query |
| N5.4 | Startup MUST wait for its dependencies | CONSTRUCTION | Compose `depends_on: service_healthy`; covers startup only, not later restarts |

### N6 — Deployment and runtime

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N6.1 | The service MUST run as a single foreground process | HOLDS | `CMD ["php","bin/worker.php"]`, no supervisor |
| N6.2 | The service MUST tolerate being killed at any instant | **VIOLATED** | See N2.2 |
| N6.3 | MySQL 8.0+ MUST be available | HOLDS | `SKIP LOCKED` and `CRC32` are hard requirements |
| N6.4 | The schema MUST be applied before first run | CONSTRUCTION | `docker-entrypoint-initdb.d`, empty-volume only |
| N6.5 | Restart MUST be safe and idempotent | HOLDS | Guard at `:70`; `restart: unless-stopped` is the recovery strategy |

### N7 — Security

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N7.1 | All SQL taking external data MUST use bound parameters | HOLDS | Only three interpolations exist, all integer-cast or code-literal |
| N7.2 | Credentials MUST NOT be logged | HOLDS | Nothing is logged at startup |
| N7.3 | Credentials SHOULD NOT be embedded in source or compose | **VIOLATED** | `ingest_svc/ingest_svc` in `db.php` defaults, compose, and the healthcheck command line |
| N7.4 | The container SHOULD NOT run as root | **VIOLATED** | No `USER` directive |
| N7.5 | Externally supplied values MUST NOT reach a URL unvalidated | AT RISK | `LOOKUP_URL`, latitude and longitude are interpolated into the request path unencoded |

### N8 — Performance envelope

| ID | Requirement | Status | Evidence |
|---|---|---|---|
| N8.1 | Throughput MUST scale by adding processes, not by tuning one | HOLDS | ~`BATCH / POLL_INTERVAL_US` ≈ 16 frames/sec/worker at defaults |
| N8.2 | An idle worker MUST NOT busy-spin | HOLDS | Unconditional `usleep` (`worker.php:16`) |
| N8.3 | Per-frame cost MUST NOT depend on total table size | AT RISK | Claim is index-backed by `k_status`; `meter_events` has no index beyond its PK and FKs, so the guard at `:70` degrades as it grows |

---

## 3. Verification map

Which artefact pins which requirement. Anything marked CONSTRUCTION has no
mechanical enforcement and can regress silently.

| Requirement | Pinned by |
|---|---|
| F1.4, F3.2, N6.5 | Case 08 (replay idempotency) |
| F2.1, F2.3, F2.5 | Cases 04, 05, 06, 07, 21; properties P12, P13 |
| F2.2, N2.1, N2.3, N3.3 | Cases 20, 25, 26 pin the **violation**, not the requirement |
| F3.1, F3.5 | Properties P4, P5 |
| F3.3 | Property P15; cases 01, 02, 19 |
| F4.1, F4.4, F4.5 | Property P17, P18; cases 01, 02 |
| F5.1 | Property P6 |
| F5.4 | Case 01 pins the duplicate |
| F6.1, F6.2 | Properties P7, P3; case 24 |
| F6.4 | Cases 09, 22 pin the boundary crossing |
| F7.1, F7.2, F7.4 | Cases 12–17; properties P19, P20 |
| F7.3 | Case 16 (duration lower bound) |
| N3.1, N3.5 | Every baseline snapshots stdout and `docker diff` |
| N1.1, N1.2, N1.4, N4.2, N4.4, N6.4, N5.4 | **Nothing.** Argued from code only |

---

## 4. Defects and hazards

### 4.1 Obvious bugs

Unambiguous defects. No consumer can sensibly depend on these.

| ID | Defect | Impact | Detail |
|---|---|---|---|
| B1 | The per-frame `catch` `return`s out of the batch loop, abandoning every later frame | **Data loss**, up to `BATCH-1` frames per incident | [Issue 14](known-issues.md#14-one-malformed-frame-strands-the-rest-of-its-batch) |
| B2 | Stranded frames have no reaper, lease expiry, or dead-letter path | **Data loss**, permanent and silent | [Issue 5](known-issues.md#5-stranded-frames-are-unrecoverable-without-manual-sql) |
| B3 | Multi-connector frames write zero readings when connector rows are absent, yet report `done` | **Data loss** | [Issue 1](known-issues.md#1-a-multi-connector-frame-can-be-marked-done-having-written-no-readings) |
| B4 | `charge_points.last_event_id` can be set to an `inbox` id | **Corruption**; no FK catches it | [Issue 3](known-issues.md#3-last_event_id-can-be-set-to-an-inbox-id) |
| B5 | An empty or non-numeric `as` segment throws on insert | **Data loss** via B1 | [Issue 15](known-issues.md#15-an-empty-as-field-strands-multi-connector-frames) |
| B6 | Every rollup advance writes two identical audit rows | Audit double-counting | [Issue 13](known-issues.md#13-every-rollup-advance-writes-two-identical-audit-rows) |
| B7 | `strpos()` used without `!== false`, so a fault marker at offset 0 is missed | Missed fault detection | [Issue 10](known-issues.md#10-strpos-truthiness-bug-in-fault-detection) |
| B8 | `rtrim()` return value discarded, so the trailing space survives | Cosmetic, but baked into stored data | [Issue 11](known-issues.md#11-minor-defects-and-dead-code) |
| B9 | No HTTP client timeout | Unbounded stall holding row locks | [Gotcha 13](../AGENTS.md) |
| B10 | No signal handling | Loses in-flight work on every ordinary shutdown | N2.2 |

### 4.2 Latent bugs

Not currently triggered. Each will surface under a specific, foreseeable change.

#### L1 — Idempotency has no database backstop

`meter_events.inbox_id` has **no unique index**. The guard is a check on
`$readConnection` (`:70`) followed by an insert on `$writeConnection` (`:239`) —
two separate sessions, so the read cannot see the write, and the pair is not
atomic. It is safe today *only* because the claim token guarantees a single
worker holds the row.

**Surfaces if:** anyone replaces or weakens the claim mechanism, adds a
retry-in-place path, or runs a backfill script alongside the worker. The failure
mode is duplicated energy readings — a billing error with no error message.

**Mitigation:** add `UNIQUE KEY (inbox_id)` on `meter_events`. It costs nothing
today and converts a silent correctness risk into a loud constraint violation.

#### L2 — Fixing B3 will expose B5

With no per-connector `charge_points` rows, the insert that would throw on a bad
`as` segment is skipped entirely. Repair the fan-out and the crash appears.
Characterization case 26 provisions those rows so this is pinned in advance.

#### L3 — `$existing_rollup_row` can be read undefined

Assigned only inside `if (array_key_exists('wh', …))` at `:267`, read
unconditionally at `:276`. A frame reaching that branch without `wh` reads an
undefined variable. Currently masked because the branch is entered mainly by
frames that carry `wh`.

#### L4 — A numeric frame identifier may match every charge point

`"1"` is bound into `WHERE cp_ident = ?` against a `VARCHAR` column. A JSON
numeric such as `0` makes MySQL coerce the column to a number; every
`CP-…` ident numifies to `0`, so `{"1": 0}` can match an arbitrary row. **Not
covered by any test** — the property generator explicitly excludes numeric
identifiers, and no characterization case supplies one.

**Mitigation:** cast to string before binding, or add a case that pins whatever
it does today.

#### L5 — `SHARDS` is interpolated, not bound

`(int)` casts are the only protection on `:36-37`. Any refactor that moves the
cast, reads the value from a new source, or makes it a string breaks both safety
and correctness. `SHARDS=0` would additionally produce `% 0`.

#### L6 — No reconnect after database restart

`depends_on: service_healthy` covers startup only. A MySQL restart throws, which
strands the batch (B1) and the process keeps looping against a dead connection
until it happens to exit. There is no `app` healthcheck to notice.

#### L7 — Connection budget is undocumented

Two connections per worker against a default `max_connections` of 151 caps the
fleet near 75 processes. Nothing enforces or documents this; exceeding it
produces connection failures that look like database outages.

### 4.3 Questionable behavior that may be depended upon

**Do not change any of these without asking the consumers first.** Each looks
like a defect from inside the service, but is externally visible and plausibly
load-bearing for someone. This is the list to take into that conversation.

#### Q1 — The service manufactures rows in someone else's table

Multi-connector fan-out **inserts new `inbox` rows** (`:218`) pre-marked `done`,
and heartbeats **rewrite `inbox.received_at`** (`:161`) — a column authored
upstream. Whether the intake team knows is unestablished.

*Depended on if:* they reconcile counts, trust `received_at` as arrival time, or
assume they authored every row. *Risk of changing:* per-connector readings
currently hang off these synthetic rows; removing them changes the shape of
`meter_events.inbox_id`. Cases 09, 22.

#### Q2 — `done` does not mean a reading was recorded

A frame can be `done` with zero `meter_events` rows (B3, and `m.zd` suppression).

*Depended on if:* anyone treats `inbox.status = 'done'` as an acknowledgement of
receipt rather than of persistence — which is a defensible reading. *Risk:*
making `done` imply a reading turns today's silent loss into visible failures,
which is correct but will look like a new outage.

#### Q3 — Rejected frames still update liveness

A frame that fails to decode still bumps `charge_points.last_seen_at`, but only
when `src != 'gw'` (case 04 vs 05).

*Depended on if:* an availability dashboard or alert uses `last_seen_at`. The
behavior is arguably right — "the radio works, the payload is malformed" is
still contact — but the transport asymmetry is not. A unit emitting pure garbage
over a direct connection looks healthy; the identical unit behind a gateway
looks offline.

#### Q4 — A high tariff withholds the fault note and raises an alert instead

When the alert gate passes, `fault_note` is deliberately **not** persisted to the
parent row and `alerted_at` is set. When it fails, the note **is** persisted and
no alert is raised. Cases 12 and 13 pin both directions.

*Depended on if:* an alerting pipeline reads `alerted_at`, or a UI reads
`fault_note`. This inverted design looks like a bug and is the single easiest
thing in the codebase to "fix" wrongly. It may well be intentional
tariff-sensitive alert suppression.

#### Q5 — Multi-connector parent rows are frozen

For `model_code = 7`, `wh`, `fault_note`, and `firmware` are never persisted to
the parent row except by heartbeats (B3, [issue 2](known-issues.md#2-multi-connector-parent-rows-are-never-updated-except-on-msg_type-3)).

*Depended on if:* reporting deliberately reads multi-connector energy from
`meter_events` and treats the parent row as identity-only. Starting to populate
it could double-count energy for anyone summing both.

#### Q6 — Lossy transformations are already baked into stored data

Three values are stored reduced, and consumers have had to adapt:

| Transformation | Where |
|---|---|
| `firmware` truncated to a major version (`v7.2-beta` → `7`) | `:319`, case 19 |
| `meter_events.raw` truncated to 250 characters | `:229`, `:241` |
| Synthesized fault notes carry a trailing space | B8, case 18 |

*Depended on if:* anything string-matches these. Widening `firmware` to the full
version would break equality checks against `'7'`.

#### Q7 — Two clocks feed the same tables

`revisions.at_` comes from MySQL `NOW()`; every other timestamp comes from the
PHP process clock. Both are UTC today, configured independently.

*Depended on if:* anything correlates `revisions.at_` with `meter_events`
timestamps. Unifying them is correct but shifts historical comparisons.

#### Q8 — `flags` bit 2 is monotonic

Set on any link-state frame (`:154`), never cleared. Effectively "this unit has
reported connectivity at least once".

*Depended on if:* used as a commissioning signal. Adding a clear path would
change its meaning from historical to current.

#### Q9 — `model_code = 253` follows the single-connector path

Only `7` is special-cased, so the legacy vendor build is treated as
single-connector despite `connector_count = 2` in the seed data (case 23).

*Depended on if:* legacy hardware genuinely reports one aggregate meter. Worth
confirming rather than assuming.

#### Q10 — Configuration fails soft, always

Every variable defaults, including credentials. A misconfigured deployment
connects as `ingest_svc/ingest_svc` and runs.

*Depended on if:* any deployment relies on the defaults instead of setting the
variables. Making these fail loudly is correct but may break a working
environment on the next restart.

---

## 5. Change-safety checklist

Before merging any change to `src/`:

1. **Both suites green.** `tests/characterization/run.py` byte-identical, and a
   property run of at least 30 scenarios.
2. **If a baseline moved**, it is a behavior change, not a refactor. Follow the
   bugfix workflow in [`tests/README.md`](../tests/README.md): read every diff,
   re-record deliberately, commit code and baseline together.
3. **Check this document.** Did the change touch a VIOLATED requirement? If it
   fixed one, move it to HOLDS and add the test that pins it. If it broke a
   HOLDS requirement, stop.
4. **Check section 4.3.** If the change alters anything in Q1–Q10, it needs a
   conversation with the intake, reporting, or settlement teams — named in
   [`integration-points.md`](integration-points.md#implicit-cross-team-contracts)
   — before it ships.
5. **Check for CONSTRUCTION-only requirements** in the area you touched. Those
   have no mechanical enforcement; add a test rather than trusting the argument.

### Recommended order of repair

Ordered by stability gained per unit of risk:

| # | Change | Closes | Risk |
|---|---|---|---|
| 1 | `continue` instead of `return` in the per-frame `catch` | B1, N2.1, most of F2.2 | Low. One line, well covered by case 25 |
| 2 | Add `UNIQUE KEY (inbox_id)` to `meter_events` | L1 | Low. Fails loudly if ever violated |
| 3 | Set `CURLOPT_TIMEOUT` and `CURLOPT_CONNECTTIMEOUT` | B9, F7.3, part of N4.3 | Low |
| 4 | Add signal handling that finishes the frame and exits the loop | B10, N2.2, N6.2 | Low |
| 5 | Add a stranded-frame reaper or lease expiry | B2, N2.3 | Medium. Needs a lease-age column or convention |
| 6 | Move the tariff lookup outside the transaction | N4.3, F7.5 | Medium. Changes lock duration and ordering |
| 7 | Repair the multi-connector path | B3, B4, Q2, Q5 | **High. Requires resolving Q2 and Q5 with consumers first, and will expose B5** |

Items 1–4 are individually small, together close four data-loss-class defects,
and touch nothing in section 4.3. They are the obvious first move.
