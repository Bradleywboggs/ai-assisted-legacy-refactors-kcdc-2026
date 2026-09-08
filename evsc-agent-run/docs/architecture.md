# Architecture

Four zoom levels, loosely following the C4 model: system context, containers, components, and the internal branch structure of the one function that does the work.

---

## Level 1 — System context

Who talks to what. This service has **no network interface**; every arrow crossing its boundary is a database operation.

```mermaid
graph TB
  OP["Operations engineer"]
  CP["Charge points / OCPP gateways<br/>field hardware"]
  IN["Field-gateway intake layer<br/>upstream service, not this repo"]
  ING["evse-ingest<br/>THIS REPO<br/>PHP 8.2 worker daemon"]
  DB[("MySQL 8.0<br/>shared database")]
  RPT["Reporting service<br/>downstream, not this repo"]
  SET["Settlement service<br/>downstream, not this repo"]

  CP -->|"telemetry frames"| IN
  IN -->|"INSERT inbox status=new"| DB
  ING -->|"claim, decode, normalize"| DB
  DB --> RPT
  DB --> SET
  OP -->|"docker compose logs"| ING
  OP -->|"SQL inspection"| DB
```

Notes that matter:

- The intake layer and this service share the `inbox` table but never call each other. The table is the contract.
- Reporting and settlement read tables this service owns, but do not own migrations against them.
- The only observability surface is the worker's stdout and direct SQL queries. There is no metrics endpoint, no structured logging, and no health endpoint on the worker.

---

## Level 2 — Containers

What actually runs, as defined by `docker-compose.yml`.

```mermaid
graph TB
  subgraph compose["docker compose project"]
    subgraph app["app service"]
      W1["php bin/worker.php<br/>process 1"]
      W2["php bin/worker.php<br/>process N<br/>via --scale app=N"]
    end
    subgraph dbc["db service"]
      MY["mysql:8.0"]
      INIT["/docker-entrypoint-initdb.d/<br/>00-schema.sql<br/>read-only bind mount"]
    end
  end
  SQL["sql/schema.sql"]

  SQL -.->|"bind mount"| INIT
  INIT -->|"runs ONLY on empty volume"| MY
  W1 -->|"2 PDO connections each"| MY
  W2 -->|"2 PDO connections each"| MY
  W1 -.->|"stdout"| LOG["docker compose logs"]
  W2 -.->|"stdout"| LOG
```

| Container | Image | Lifecycle |
|---|---|---|
| `app` | Built from `Dockerfile`, `php:8.2-cli` + `pdo_mysql` | `restart: unless-stopped`. No healthcheck — a hung worker loop is undetectable to compose. |
| `db` | `mysql:8.0` | Healthchecked with `mysqladmin ping` every 3s, 20 retries. `app` waits on `service_healthy`. |

Two operational consequences of this topology:

1. **`Dockerfile` uses `COPY . .` and `app` has no bind-mount.** Source is baked in at build time, so code changes need `docker compose up -d --build app`. A plain `restart` runs the old code.
2. **Schema init only fires on an empty data volume.** Editing `sql/schema.sql` and restarting does nothing. Applying schema changes requires `docker compose down -v && docker compose up -d`, which destroys all data.

Each worker process opens **two** connections, not one — see Level 3.

---

## Level 3 — Components

The internals of one worker process. Three source files, no autoloader, no namespaces, no classes beyond a single PDO wrapper.

```mermaid
graph TB
  subgraph binw["bin/worker.php"]
    LOOP["while true<br/>processInboxBatch true<br/>usleep POLL_INTERVAL_US"]
  end
  subgraph ing["src/ingest.php"]
    PIB["processInboxBatch<br/>line 48<br/>~365 lines, all logic"]
    CB["claimBatch<br/>line 28"]
    FPT["fetchPostedTariff<br/>line 8"]
    CONST["PER_CONNECTOR_FIELDS<br/>PER_POINT_FIELDS<br/>lines 5-6"]
  end
  subgraph dbf["src/db.php"]
    CONN["conn<br/>line 52<br/>PDO factory"]
    CX["class Cx<br/>line 3<br/>refcounted transactions"]
    SAY["say<br/>line 67<br/>stdout logger"]
  end
  EXT["Tariff rate service<br/>optional, LOOKUP_URL"]
  MY[("MySQL")]

  LOOP -->|"require_once"| PIB
  PIB --> CB
  PIB --> FPT
  PIB -.->|"reads"| CONST
  PIB --> CONN
  CB --> CX
  PIB --> CX
  PIB --> SAY
  CX --> MY
  FPT -->|"curl, if configured"| EXT
```

### Component responsibilities

| Component | Responsibility |
|---|---|
| `bin/worker.php` | Nothing but the loop. Reads `POLL_INTERVAL_US` once at startup, calls `processInboxBatch(true)`, sleeps unconditionally, repeats. No signal handling, no top-level try/catch, ignores the return value. |
| `claimBatch()` | Leases frames. Generates a random token, `SELECT ... FOR UPDATE SKIP LOCKED`, stamps the token into `inbox.status`, commits, re-reads by token. |
| `processInboxBatch()` | Everything else: decode, validate, dispatch, state update, fan-out, audit, status transition, error handling. One function, five-plus nesting levels. |
| `fetchPostedTariff()` | Optional HTTP lookup of the posted tariff for a coordinate. When `LOOKUP_URL` is unset it sleeps 120–210 ms and returns `null` — inside the open write transaction. |
| `Cx` | Reference-counted transaction wrapper over PDO, plus `ex()`/`q()` (byte-identical) and `lastId()`. |
| `conn()` | PDO factory. `ERRMODE_EXCEPTION`, `FETCH_ASSOC`, `EMULATE_PREPARES=false`, `utf8mb4`. |
| `say()` | The only logging facility. `[<pid>] message` to **stdout**. |

### The two-connection design

`processInboxBatch()` opens two independent `Cx` instances, `$writeConnection` and `$readConnection` (`src/ingest.php:50-51`). All mutations go through the write connection; all lookups through the read connection.

Because these are separate MySQL sessions, **reads do not see uncommitted writes from the same frame's transaction**. Anything the current frame has written is invisible to subsequent `$readConnection` queries until commit. This is load-bearing for the multi-connector path, where a lookup of a just-inserted row would fail.

### Transaction shape

`Cx` counts nesting depth rather than using savepoints:

```mermaid
stateDiagram-v2
  [*] --> Depth0
  Depth0 --> Depth1: begin, real BEGIN
  Depth1 --> Depth2: begin, NO-OP
  Depth2 --> Depth1: commit, NO-OP
  Depth1 --> Depth0: commit, real COMMIT
  Depth1 --> Depth0: rollBack, real ROLLBACK
  Depth2 --> Depth0: rollBack, real ROLLBACK of everything
```

The consequence: an inner `begin()`/`commit()` pair inside an already-open transaction does nothing at the database level, and `rollBack()` from any depth discards the entire outermost transaction. There are no partial rollbacks. The nested `begin`/`commit` around the `last_seen_at` update (`src/ingest.php:85-90`) is a logical no-op that folds into the surrounding frame transaction.

---

## Level 4 — Control flow inside `processInboxBatch()`

The single most important thing to understand before editing `src/ingest.php`: there are **two independent branches on `model_code == 7`**, at different points, with different secondary conditions. They do not agree with each other.

```mermaid
graph TB
  START["frame claimed"] --> IDEM{"meter_events row<br/>for this inbox_id?"}
  IDEM -->|yes| DONE1["status = done<br/>skip"]
  IDEM -->|no| SEEN{"src != 'gw'?"}
  SEEN -->|yes| BUMP["UPDATE last_seen_at<br/>line 88<br/>BEFORE validation"]
  SEEN -->|no| DEC
  BUMP --> DEC{"JSON decodes<br/>and has key '1'?"}
  DEC -->|no| BAD1["status = bad"]
  DEC -->|yes| FUT{"la > now + 2 days?"}
  FUT -->|yes| BAD2["status = bad"]
  FUT -->|no| CPL{"cp_ident known?"}
  CPL -->|no| BAD3["status = bad"]
  CPL -->|yes| TZ["resolve tz, build dt,<br/>convert to UTC"]
  TZ --> DISP["msg_type dispatch<br/>lines 151-164"]
  DISP --> FAULT["fault note synthesis<br/>lines 166-190"]

  FAULT --> B1{"BRANCH 1, line 194<br/>model_code == 7?"}
  B1 -->|no| SIMPLE["INSERT 1 meter_events<br/>line 239"]
  B1 -->|yes| ZD{"m.zd present?"}
  ZD -->|yes| NOOP["NO inserts at all"]
  ZD -->|no| FAN["fan out per connector<br/>lines 194-237"]

  SIMPLE --> LID["insertedEventId = lastInsertId<br/>line 245"]
  NOOP --> LID
  FAN --> LID
  LID --> MARK["status = done, line 247"]
  MARK --> UPD["build chargePointUpdates<br/>lines 249-257"]
  UPD --> ROLL["rollup advancement<br/>lines 261-283"]
  ROLL --> DIFF["field-level audit diff<br/>lines 323-333"]

  DIFF --> B2{"BRANCH 2, line 335<br/>model_code == 7<br/>AND msg_type != 3?"}
  B2 -->|yes| CONNAUD["per-connector revision_details<br/>NO charge_points UPDATE"]
  B2 -->|no| PARENT["UPDATE charge_points<br/>+ revision_details<br/>lines 371-384"]
  CONNAUD --> COMMIT["commit, line 387"]
  PARENT --> COMMIT
```

### Why the two branches disagree

| Frame | Branch 1 (line 194) | Branch 2 (line 335) | Net effect |
|---|---|---|---|
| `model_code != 7` | simple insert | parent UPDATE | Correct. One reading, one state update. |
| `model_code == 7`, `msg_type != 3` | fan out | connector audit only | Readings fan out; **parent row never updated**. |
| `model_code == 7`, `msg_type == 3` | fan out | **parent UPDATE** | Fan-out happens *and* the parent is updated with `last_event_id` taken from `lastInsertId()` after the fan-out loop — which is a synthetic `inbox` id, not a `meter_events` id. |
| `model_code == 7`, `m.zd` present | nothing inserted | depends on `msg_type` | Frame marked `done` having written no readings. |

The third row is a verified data-corruption path; the fourth is verified silent data loss. Both are reproduced in [Known Issues](known-issues.md).

### Error handling and the strand path

```mermaid
graph LR
  TRY["per-frame try<br/>line 69"] --> EXC{"exception?"}
  EXC -->|no| OK["commit, next frame"]
  EXC -->|yes| LOG["say ERR message"]
  LOG --> RB["rollBack if in tx"]
  RB --> DL{"message contains<br/>'Deadlock'?"}
  DL -->|yes| RET1["say DEADLOCK<br/>return true"]
  DL -->|no| RET2["return false"]
  RET1 --> STRAND
  RET2 --> STRAND["frame still holds<br/>the claim token<br/>NO worker will retry it"]
```

Both exit paths abandon the frame mid-flight. Its `inbox.status` is still the claiming worker's random token, which matches neither `new` nor `done` nor `bad`, so it is invisible to every future claim query. Deadlock detection is a substring match on the exception message, and changes only the return value — which `bin/worker.php` discards anyway.

Recovery is manual:

```sql
UPDATE inbox SET status = 'new' WHERE status NOT IN ('new','done','bad');
```

---

## Scaling

Horizontal scaling is safe and is the intended deployment shape: one process per allotted core.

```bash
docker compose up -d --scale app=4
```

`SKIP LOCKED` plus the per-claim token means two workers cannot take the same frame. What you lose by default is per-unit ordering: with `SHARDS=1`, two frames from one charge point may be handled concurrently by different workers and land out of order.

To preserve ordering per charge point, partition by identity — give each replica a distinct `SHARD` and a common `SHARDS`:

```
SHARD=0 SHARDS=4   # worker 0 handles CRC32(cp_ident) % 4 == 0
SHARD=1 SHARDS=4
...
```

`SHARD` and `SHARDS` are read at `src/ingest.php:31-32` and are **not documented in the root README**, nor set in `docker-compose.yml`. Note that these values are interpolated directly into the claim SQL rather than bound as parameters; the `(int)` casts are what make that safe.

---

## Related documents

- [Domain Overview](domain-overview.md) — what the service is for
- [Sequence Diagrams](sequence-diagrams.md) — the same flows over time
- [Data Model](data-model.md) — ERD and table reference
- [Known Issues](known-issues.md) — verified defects
- [Integration Points](integration-points.md) — every external touchpoint and its blast radius
- [Stability Spec](stability-spec.md) — the invariants that must survive any change
