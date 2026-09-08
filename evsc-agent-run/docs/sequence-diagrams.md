# Sequence Diagrams

Six views of the same system, from platform-wide down to individual SQL statements. Every diagram below was checked against a running stack; the concrete values in the walkthroughs are real observed output, not illustrations.

| Zoom | Diagram | Answers |
|---|---|---|
| 1 | [Platform lifecycle](#zoom-1--platform-lifecycle) | How does a physical event become a billable number? |
| 2 | [One worker cycle](#zoom-2--one-worker-cycle) | What does the daemon do every 250 ms? |
| 3 | [One frame, simple hardware](#zoom-3--one-frame-single-connector-hardware) | What happens to a normal frame? |
| 4 | [One frame, multi-connector](#zoom-4--one-frame-multi-connector-hardware-model_code--7) | Why is the multi-connector path dangerous? |
| 5 | [Audit trail construction](#zoom-5--audit-trail-construction) | How are `revisions` rows built? |
| 6 | [Concurrency and failure](#zoom-6--concurrency-and-failure-paths) | What happens with N workers, or when things break? |

---

## Zoom 1 — Platform lifecycle

The whole journey, with this repo as a single box.

```mermaid
sequenceDiagram
    autonumber
    participant Car as Vehicle / driver
    participant CP as Charge point
    participant GW as OCPP gateway
    participant IN as Intake layer
    participant DB as MySQL
    participant W as evse-ingest worker
    participant RPT as Reporting / settlement

    Car->>CP: plugs in, draws energy
    CP->>CP: meter accumulates Wh
    alt direct reporting
        CP->>IN: telemetry frame
    else via gateway
        CP->>GW: OCPP message
        GW->>IN: translated frame
    end
    IN->>DB: INSERT inbox (status='new')
    Note over DB: frame is now queued,<br/>opaque and undecoded

    loop every POLL_INTERVAL_US
        W->>DB: claim a batch of 'new' frames
        W->>W: decode + normalize
        W->>DB: write meter_events, update charge_points
        W->>DB: write revisions + revision_details
        W->>DB: status = 'done' or 'bad'
    end

    RPT->>DB: SELECT for invoices and dashboards
```

The handoff at step 5 is the only contract between the intake layer and this service. Everything before it belongs to another team; everything after step 6 belongs to this repo.

---

## Zoom 2 — One worker cycle

`bin/worker.php` is a bare loop. It never inspects the result of the call it makes.

```mermaid
sequenceDiagram
    autonumber
    participant Main as bin/worker.php
    participant PIB as processInboxBatch()
    participant CB as claimBatch()
    participant DB as MySQL

    Note over Main: POLL_INTERVAL_US read once at startup

    loop forever
        Main->>PIB: processInboxBatch(true)
        PIB->>DB: conn() x2 — write + read connections
        PIB->>PIB: BATCH re-read from env each cycle
        PIB->>CB: claimBatch(writeConnection, BATCH)
        CB->>DB: BEGIN
        CB->>DB: SELECT id FROM inbox WHERE status='new'<br/>AND CRC32(cp_ident) % SHARDS = SHARD<br/>ORDER BY id LIMIT n FOR UPDATE SKIP LOCKED
        CB->>DB: UPDATE inbox SET status = '<token>' WHERE id IN (...)
        CB->>DB: COMMIT
        CB->>DB: SELECT * FROM inbox WHERE status = '<token>'
        CB-->>PIB: claimed rows

        alt no rows claimed
            PIB-->>Main: return true (idle)
        else rows claimed
            loop each frame
                PIB->>PIB: process one frame (see Zoom 3)
            end
            PIB-->>Main: return true / false — DISCARDED
        end
        Main->>Main: usleep(POLL_INTERVAL_US)
    end
```

Three things to notice:

- **The claim is its own committed transaction.** The lease is durable before any decoding starts, which is what makes `SKIP LOCKED` sufficient for multi-process safety.
- **The token is the lease.** It is `'c'` plus 11 hex characters, unique per `claimBatch()` call. A frame carrying a token is invisible to every other worker's claim query, because the query filters `status = 'new'`.
- **The sleep is unconditional.** Even after processing a full batch, the worker sleeps. Throughput is capped at roughly `BATCH / POLL_INTERVAL_US` frames per second per process — with defaults, about 16 frames/sec.

---

## Zoom 3 — One frame, single-connector hardware

The happy path, `model_code != 7`. This walkthrough is a real observed run.

**Input:** `{"1":"CP-0004","msg_type":1,"wh":4242,"la":"2026-09-07 14:00:00"}`, `src='cp'`, `CP-0004` has `model_code = 253`, `tz = America/Chicago`.

```mermaid
sequenceDiagram
    autonumber
    participant PIB as processInboxBatch()
    participant RC as readConnection
    participant WC as writeConnection
    participant DB as MySQL

    PIB->>RC: SELECT id FROM meter_events WHERE inbox_id = 11
    RC-->>PIB: empty — not a replay

    PIB->>WC: BEGIN (depth 0 to 1)

    Note over PIB: src != 'gw', so stamp liveness FIRST
    PIB->>WC: begin() — depth 1 to 2, NO-OP
    PIB->>WC: UPDATE charge_points SET last_seen_at = ?<br/>WHERE cp_ident = 'CP-0004'
    PIB->>WC: commit() — depth 2 to 1, NO-OP

    PIB->>PIB: json_decode(body) — has key '1', OK
    PIB->>PIB: la = 2026-09-07 14:00:00, not > now+2d, OK
    PIB->>RC: SELECT * FROM charge_points WHERE cp_ident = 'CP-0004'
    RC-->>PIB: row: id=4, model_code=253, tz=America/Chicago, wh=0

    Note over PIB: dt = la, interpreted in America/Chicago<br/>converted to UTC → 2026-09-07 19:00:00
    Note over PIB: msg_type 1 — no special dispatch

    PIB->>WC: INSERT INTO meter_events<br/>(inbox_id, cp_id, msg_type, wh, raw,<br/>local_event_at, utc_event_at,<br/>rollup_date, rollup_hour, fault_note, flags)
    WC-->>PIB: lastInsertId() = 3
    PIB->>WC: UPDATE inbox SET status = 'done' WHERE id = 11

    Note over PIB: build chargePointUpdates:<br/>wh=4242, local_event_at=..., last_event_id=3, last_seen_at=...

    PIB->>WC: INSERT INTO revisions<br/>(who=0, tname='ChargePoint', target_id='CP-0004', op='U', at_=NOW())
    WC-->>PIB: revisionId

    PIB->>RC: SELECT rollup_date FROM meter_events WHERE id = <rollup_event_id>
    PIB->>WC: UPDATE charge_points SET `wh`=?, `local_event_at`=?,<br/>`last_event_id`=?, `last_seen_at`=?, `rollup_at`=? WHERE id = 4
    loop each changed field
        PIB->>WC: INSERT INTO revision_details (rev_id, col, before_, after_)
    end

    PIB->>WC: COMMIT (depth 1 to 0)
```

**Observed result:** `inbox.status='done'`; one `meter_events` row `(id=3, inbox_id=11, cp_id=4, wh=4242)`; `charge_points.last_event_id=3` correctly pointing at that row; `revision_details` recording `wh: 0 → 4242`.

### Timezone conversion, verified twice

| Unit | `tz` | Frame time source | Stored `utc_event_at` |
|---|---|---|---|
| `CP-0001` | `America/Chicago` | `la = 12:00:00` | `17:00:00` (CDT, UTC−5) |
| `CP-0003` | `America/Denver` | `rd=2026-09-06`, `rh=14` | `2026-09-06 20:00:00` (MDT, UTC−6) |

Two asymmetries fall out of this, both verified:

- `meter_events.local_event_at` is populated **only** from `la`. A frame using `rd`/`rh` stores `NULL` there.
- `charge_points.rollup_at` only advances when `la` is present, because the guard requires `local_event_at` to be in the update set. An `rd`/`rh` frame leaves `rollup_at` untouched — observed as `NULL` on `CP-0003` after a successful rollup frame.

### Link-state frames also write readings

`msg_type` 9, 11, and 14 are not pure control messages. On single-connector hardware they take the same path and insert a `meter_events` row with `wh` defaulting to `0`:

```mermaid
sequenceDiagram
    autonumber
    participant PIB as processInboxBatch()
    participant WC as writeConnection

    Note over PIB: msg_type = 14 (disconnect)
    PIB->>WC: INSERT INTO revisions (target_id='CP-0001', op='U')
    PIB->>WC: UPDATE charge_points SET link_state = 0, flags = flags | 4 WHERE id = 1
    PIB->>WC: INSERT INTO revision_details (col='link_state', before_=1, after_=0)
    Note over PIB: falls through to the normal path
    PIB->>WC: INSERT INTO meter_events (msg_type=14, wh=0, ...)
    PIB->>WC: UPDATE inbox SET status='done'
```

Observed: `link_state` moved `0 → 1` on `msg_type 9` and `1 → 0` on `msg_type 14`; `flags` became `4` and **stayed** `4` — the bit is never cleared.

---

## Zoom 4 — One frame, multi-connector hardware (`model_code = 7`)

This is the path to be careful with. Observed run: `{"1":"CP-0002","msg_type":3,"wh":900,"as":"100:200:300","la":"2026-09-07 12:00:00"}` against `CP-0002`, which has three connectors `CP-0002-1..3`.

```mermaid
sequenceDiagram
    autonumber
    participant PIB as processInboxBatch()
    participant RC as readConnection
    participant WC as writeConnection

    Note over PIB: decode, validate, resolve tz — same as Zoom 3
    PIB->>RC: SELECT * FROM charge_points WHERE cp_ident='CP-0002'
    RC-->>PIB: model_code = 7

    alt m.zd present in metadata
        Note over PIB: ENTIRE fan-out block skipped.<br/>No meter_events written at all.
    else m.zd absent
        PIB->>RC: SELECT * FROM connectors WHERE cp_id=2 AND retired=0
        RC-->>PIB: CP-0002-1 (no 1), CP-0002-2 (no 2), CP-0002-3 (no 3)
        Note over PIB: split as="100:200:300" by connector_no - 1

        loop each connector
            Note over PIB: patch body by splitting on ',' and<br/>replacing the '"1"' fragment — string surgery,<br/>not JSON re-encoding
            PIB->>WC: INSERT INTO inbox<br/>(src, status='done', cp_ident='CP-0002-N',<br/>body=<patched>, body_hash=<PARENT's hash>, received_at)
            WC-->>PIB: synthetic inbox id
            PIB->>RC: SELECT id FROM charge_points WHERE cp_ident='CP-0002-N'
            alt a charge_points row exists for the connector ident
                PIB->>WC: INSERT INTO meter_events (inbox_id=<synthetic>, cp_id=<connector's cp id>, ...)
            else no such row — TRUE FOR SEED DATA
                Note over PIB: silently skipped, no log line
            end
        end
    end

    PIB->>PIB: insertedEventId = lastInsertId()
    Note over PIB: this is the last INSERT on the connection —<br/>a synthetic INBOX id, not a meter_events id
    PIB->>WC: UPDATE inbox SET status='done' WHERE id = <parent>

    alt msg_type != 3
        Note over PIB: per-connector revision_details only.<br/>Parent charge_points row NOT updated.
    else msg_type == 3
        PIB->>WC: UPDATE charge_points SET `wh`=900, ..., `last_event_id`=<synthetic inbox id> WHERE id=2
    end
    PIB->>WC: COMMIT
```

**Observed result for the `msg_type = 3` run above:**

| Table | Observed |
|---|---|
| `inbox` | 4 rows: parent `CP-0002` plus synthetic `CP-0002-1`, `CP-0002-2`, `CP-0002-3`, **all `done`, all sharing one `body_hash`** |
| `meter_events` | **0 rows** |
| `charge_points` id 2 | `wh=900`, `last_event_id=4` — and `4` is a synthetic **`inbox`** id, while `meter_events` is empty |

So a frame that recorded no energy reading whatsoever was acknowledged as `done`, and the parent row now carries a `last_event_id` pointing into the wrong table. `charge_points.last_event_id` has no foreign key, so nothing catches it.

With `msg_type != 3` the same frame leaves the parent row entirely untouched — a frame carrying `wh: 777` was observed to leave `charge_points.wh` at its previous value.

Full reproductions are in [Known Issues](known-issues.md).

---

## Zoom 5 — Audit trail construction

Every field change is meant to produce a `revisions` header plus one `revision_details` row per field. Details are accumulated in memory throughout processing and flushed at the end.

```mermaid
sequenceDiagram
    autonumber
    participant PIB as processInboxBatch()
    participant Mem as revisionDetailRows (in memory)
    participant WC as writeConnection

    Note over PIB: revisions rows are ALWAYS<br/>who=0, tname='ChargePoint', op='U'

    opt msg_type is 9 / 11 / 14
        PIB->>WC: INSERT INTO revisions
        PIB->>WC: INSERT INTO revision_details (col='link_state') — written immediately
    end

    opt fault note detected
        PIB->>Mem: append [null, 'fault_note', null, <note>]
        Note over Mem: rev_id is NULL at this point —<br/>the header does not exist yet
    end

    Note over PIB: later: create the header if anything changed
    opt updates non-empty OR received_at > last_seen_at
        PIB->>WC: INSERT INTO revisions
        WC-->>PIB: revisionId
    end

    loop each field in chargePointUpdates
        alt model_code 7 and field in PER_CONNECTOR_FIELDS (wh, raw, firmware)
            PIB->>Mem: append with connector-split value
        else model_code 7 and field in PER_POINT_FIELDS (fault_note, alerted_at)
            PIB->>Mem: append unsplit
        else value differs from the DB row
            PIB->>Mem: append [revisionId, field, before, after]
        end
    end

    loop each accumulated detail
        alt rev_id is NULL
            Note over PIB: SKIPPED — silently discarded
        else
            PIB->>WC: INSERT INTO revision_details (rev_id, col, before_, after_)
        end
    end
```

Two consequences worth internalizing:

- **A detail queued before its header exists is dropped.** Rows appended with a `NULL` `rev_id` — the fault-note case above — are filtered out at flush time. The audit trail can therefore be incomplete without any error.
- **Details are only written for fields whose value actually changed**, compared against the row read at the start of processing.

Observed audit output for a clean single-connector run:

```
target_id  op  col             before_              after_
CP-0003    U   wh              0                    5000
CP-0003    U   last_event_id   NULL                 4
CP-0003    U   last_seen_at    2026-09-07 21:46:59  2026-09-07 21:47:38
```

---

## Zoom 6 — Concurrency and failure paths

### Two workers racing for the same batch

```mermaid
sequenceDiagram
    autonumber
    participant W1 as Worker 1
    participant W2 as Worker 2
    participant DB as MySQL

    par
        W1->>DB: BEGIN
        W1->>DB: SELECT ... WHERE status='new' ORDER BY id<br/>LIMIT 4 FOR UPDATE SKIP LOCKED
        DB-->>W1: rows 1,2,3,4 — now row-locked by W1
    and
        W2->>DB: BEGIN
        W2->>DB: SELECT ... WHERE status='new' ORDER BY id<br/>LIMIT 4 FOR UPDATE SKIP LOCKED
        Note over DB: rows 1-4 locked, SKIP LOCKED passes over them
        DB-->>W2: rows 5,6,7,8
    end

    W1->>DB: UPDATE inbox SET status='cAAAA...' WHERE id IN (1,2,3,4)
    W2->>DB: UPDATE inbox SET status='cBBBB...' WHERE id IN (5,6,7,8)
    W1->>DB: COMMIT
    W2->>DB: COMMIT
    W1->>DB: SELECT * FROM inbox WHERE status='cAAAA...'
    W2->>DB: SELECT * FROM inbox WHERE status='cBBBB...'
    Note over W1,W2: disjoint batches, no blocking
```

Neither worker waits on the other: `SKIP LOCKED` turns contention into partitioning. The re-read by token is what lets each worker recover exactly its own set.

### Frame rejection — the three `bad` paths

All three were reproduced.

```mermaid
sequenceDiagram
    autonumber
    participant PIB as processInboxBatch()
    participant WC as writeConnection

    opt src != 'gw'
        PIB->>WC: UPDATE charge_points SET last_seen_at = ?
        Note over WC: happens BEFORE validation —<br/>a rejected frame still bumps liveness
    end

    alt body is not JSON, or key '1' missing
        PIB->>WC: UPDATE inbox SET status='bad'
    else la is more than 2 days in the future
        PIB->>WC: UPDATE inbox SET status='bad'
    else cp_ident not found in charge_points
        PIB->>WC: UPDATE inbox SET status='bad'
    end
    PIB->>WC: COMMIT
    Note over PIB: continue to next frame. Nothing logged.
```

Observed: a garbage frame for `CP-0010` with `src='cp'` moved `last_seen_at` from `NULL` to a real timestamp and *then* was marked `bad`. The identical frame for `CP-0011` with `src='gw'` left `last_seen_at` as `NULL`. Rejections produce no log output at all.

### Exception — the strand path

```mermaid
sequenceDiagram
    autonumber
    participant PIB as processInboxBatch()
    participant WC as writeConnection
    participant Log as stdout via say()
    participant Main as bin/worker.php

    PIB->>WC: ... mid-frame work ...
    WC--xPIB: Exception
    PIB->>Log: "[pid] ERR <message>"
    PIB->>WC: rollBack() — discards the WHOLE outermost transaction
    alt message contains "Deadlock"
        PIB->>Log: "[pid] DEADLOCK caught, not crashing"
        PIB-->>Main: return true
    else any other error
        PIB-->>Main: return false
    end
    Main->>Main: ignores the return value, sleeps, loops
    Note over WC: frame still carries the claim token.<br/>Not 'new', not 'done', not 'bad'.<br/>Invisible to every future claim. STRANDED.
```

The rollback undoes the `bad`/`done` status write along with everything else, because `Cx` has no savepoints. The frame is now unreachable. Find and recover stranded frames with:

```sql
SELECT id, cp_ident, status, received_at
FROM inbox WHERE status NOT IN ('new','done','bad');

UPDATE inbox SET status='new' WHERE status NOT IN ('new','done','bad');
```

### Replay is safe

```mermaid
sequenceDiagram
    autonumber
    participant Op as Operator
    participant W as Worker
    participant DB as MySQL

    Op->>DB: UPDATE inbox SET status='new' WHERE id=11
    W->>DB: claims frame 11 again
    W->>DB: SELECT id FROM meter_events WHERE inbox_id=11
    DB-->>W: row exists
    W->>DB: UPDATE inbox SET status='done' WHERE id=11
    Note over W: no duplicate meter_events row written
```

Verified: after resetting a processed frame to `new`, it returned to `done` and `meter_events` was unchanged. This makes the strand-recovery `UPDATE` above safe to run even if some of those frames had partially succeeded.

---

## Related documents

- [Domain Overview](domain-overview.md) — vocabulary and platform context
- [Architecture](architecture.md) — static structure behind these flows
- [Data Model](data-model.md) — the tables being written
- [Known Issues](known-issues.md) — reproductions of the defects noted above
- [Integration Points](integration-points.md) — every external touchpoint and its blast radius
- [Stability Spec](stability-spec.md) — the invariants that must survive any change
