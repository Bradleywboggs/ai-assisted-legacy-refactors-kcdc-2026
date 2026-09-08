# Data Model

Six tables, defined in `sql/schema.sql`, which is the source of truth. Five are owned by this service; `inbox` is owned upstream.

---

## Entity relationship diagram — enforced constraints

Only four foreign keys actually exist in the schema. This diagram shows exactly those.

```mermaid
erDiagram
    charge_points ||--o{ meter_events : "fk_ev_cp (cp_id)"
    charge_points ||--o{ connectors   : "fk_conn_cp (cp_id)"
    inbox         ||--o{ meter_events : "fk_ev_inbox (inbox_id)"
    revisions     ||--o{ revision_details : "fk_rdt_rev (rev_id)"

    charge_points {
        INT id PK "not auto-increment"
        VARCHAR_32 cp_ident UK "e.g. CP-0001"
        INT wh "cumulative watt-hours"
        INT connector_count
        TINYINT_UNSIGNED model_code "0 single, 7 multi, 253 legacy"
        TINYINT flags "bit 2 set on link events, never cleared"
        TINYINT link_state "1 up, 0 down"
        TINYINT via_gateway
        VARCHAR_48 tz "default America/Chicago"
        VARCHAR_16 tariff "nullable"
        VARCHAR_24 firmware "major version only"
        DATETIME last_seen_at
        DATETIME local_event_at
        DATETIME rollup_at
        BIGINT rollup_event_id "logical ref, no FK"
        BIGINT last_event_id "logical ref, no FK"
        VARCHAR_255 fault_note
        DATETIME alerted_at
        TINYINT settled
    }

    inbox {
        BIGINT id PK
        VARCHAR_16 src "'gw' means via gateway"
        VARCHAR_16 status "new, done, bad, or a claim token"
        VARCHAR_32 cp_ident "logical ref, no FK"
        TEXT body "raw JSON frame"
        CHAR_40 body_hash "SHA-1, not unique"
        DATETIME received_at
    }

    meter_events {
        BIGINT id PK
        BIGINT inbox_id FK
        INT cp_id FK
        TINYINT msg_type
        INT wh
        VARCHAR_255 raw "body truncated to 250 chars"
        DATETIME local_event_at "only from frame key la"
        DATETIME utc_event_at "normalized"
        DATE rollup_date "only from frame key rd"
        TINYINT rollup_hour "only from frame key rh"
        VARCHAR_255 fault_note
        TINYINT flags
    }

    connectors {
        INT id PK
        INT cp_id FK
        VARCHAR_32 connector_ident "e.g. CP-0002-1"
        INT connector_no "1-based"
        INT panel
        TINYINT retired
    }

    revisions {
        BIGINT id PK
        INT who "always 0 — no user context"
        VARCHAR_32 tname "always 'ChargePoint'"
        VARCHAR_32 target_id "a cp_ident, not a numeric id"
        CHAR_1 op "always 'U'"
        DATETIME at_ "trailing underscore avoids reserved word"
    }

    revision_details {
        BIGINT id PK
        BIGINT rev_id FK
        VARCHAR_48 col "column name"
        VARCHAR_255 before_
        VARCHAR_255 after_
    }
```

## Relationships the schema does *not* enforce

These joins are real in the code but invisible to the database. Nothing stops them from going stale or pointing at nothing.

```mermaid
graph LR
  IN["inbox.cp_ident"] -.->|"string match, no FK"| CP["charge_points.cp_ident"]
  CPL["charge_points.last_event_id"] -.->|"no FK"| ME["meter_events.id"]
  CPR["charge_points.rollup_event_id"] -.->|"no FK"| ME
  RV["revisions.target_id"] -.->|"string match, no FK"| CP
  CN["connectors.connector_ident"] -.->|"expected to exist as its own<br/>charge_points row — often does NOT"| CP
```

| Logical link | Why there is no FK | Risk |
|---|---|---|
| `inbox.cp_ident` → `charge_points.cp_ident` | `inbox` is written by an upstream service that may land frames for units not yet provisioned | Unknown idents are caught in application code and the frame is marked `bad` |
| `charge_points.last_event_id` → `meter_events.id` | Would be circular with `fk_ev_cp` | **Verified to be corruptible** — the multi-connector path can write a synthetic `inbox` id here. See [Known Issues](known-issues.md) |
| `charge_points.rollup_event_id` → `meter_events.id` | Same | A dangling value silently degrades the rollup comparison |
| `revisions.target_id` → `charge_points.cp_ident` | `revisions` is polymorphic by design: `tname` + `target_id` | Cannot be enforced; `tname` is nonetheless always `'ChargePoint'` in practice |
| `connectors.connector_ident` → a `charge_points` row | Not modelled at all | **The fan-out path depends on it and the seed data violates it.** See [Known Issues](known-issues.md) |

---

## `inbox.status` — the lifecycle

`status` is `VARCHAR(16)`, **not an enum**. There is no database constraint on its contents, and at any given moment it may hold a value that appears in no documentation: a per-claim lease token.

```mermaid
stateDiagram-v2
    [*] --> new: intake layer INSERTs
    new --> token: claimBatch stamps<br/>'c' + 11 hex chars
    token --> done: processed, or replay detected
    token --> bad: undecodable
    token --> token: exception — STRANDED
    done --> new: manual replay (safe)
    bad --> [*]
    done --> [*]

    note right of token
      Matches neither new, done, nor bad.
      Invisible to every claim query.
      No reaper exists.
    end note
```

| Value | Meaning |
|---|---|
| `new` | Awaiting processing. **The only value the worker will claim.** |
| `c…` (12 chars) | In flight, leased by one worker process |
| `done` | Terminal success — but see the caveat below |
| `bad` | Terminal rejection: undecodable, missing `1` key, timestamp >2 days future, or unknown `cp_ident` |

Two cautions when querying this column:

1. **Never assume three values.** `WHERE status IN ('new','done','bad')` silently excludes in-flight and stranded rows. To find strays: `WHERE status NOT IN ('new','done','bad')`.
2. **`done` does not imply a reading was stored.** On multi-connector hardware, `done` is reachable with zero `meter_events` rows written.

---

## Table reference

### `charge_points`

One row per physical unit. On multi-connector hardware, connector-level fields are *intended* to be folded onto this parent row.

- **`id INT PRIMARY KEY` is not `AUTO_INCREMENT`.** Every insert must supply an explicit id. Provisioning happens outside this service.
- `cp_ident` is the real join key — `inbox` and `revisions` both reference it by string, not by `id`.
- `model_code` is the branch discriminator for the entire ingest pipeline: `0` single-connector, `7` multi-connector, `253` legacy vendor build. Only `7` is special-cased; `253` follows the simple path.
- `flags` bit 2 (`| 4`) is set on any link-state frame and never cleared.
- `tz` drives all UTC conversion, defaulting to `America/Chicago` when null or empty.
- `firmware` stores a *truncated* value: `ltrim(explode('-', $fw)[0], 'v')`, so `v2-beta` is stored as `2`.
- Indexes: primary key and `UNIQUE(cp_ident)` only.

### `inbox`

Raw frames, pre-decode. **Owned by the upstream field-gateway intake layer.**

- All of `src`, `cp_ident`, `body`, `body_hash`, `received_at` are `NOT NULL` with no default. Any manual insert must supply all five.
- `body_hash CHAR(40)` is a SHA-1 of the body and is **not** unique — and the fan-out path copies the parent's hash onto rows with different bodies, so it cannot be used for deduplication.
- `KEY k_status (status, id)` exists specifically to serve the claim query's `WHERE status='new' ORDER BY id`.
- Deliberately has **no foreign keys**, because frames may arrive for units that do not exist yet.
- The worker writes to this table in three ways: status transitions, `received_at` correction on `msg_type 3` frames lacking `la`, and **inserting new synthetic rows** during multi-connector fan-out.

### `meter_events`

One decoded reading per row. The append-only output that settlement consumes.

- `raw` holds the first 250 characters of the originating frame body — truncated, so it is a diagnostic aid, not a faithful archive.
- `local_event_at` comes only from frame key `la`; `rollup_date`/`rollup_hour` come only from `rd`/`rh`. A frame supplying one style leaves the other style's columns `NULL`.
- `utc_event_at` is the normalized timestamp and the column downstream reporting should use.
- The uniqueness of `inbox_id` is what makes replay safe, but it is enforced **in application code only** — there is no unique index on `inbox_id`. Concurrent processing of the same `inbox` row would create duplicates; the claim token is what prevents that.
- Fan-out rows omit `rollup_date`, `rollup_hour`, and `fault_note` — the multi-connector insert has a shorter column list than the simple one.

### `connectors`

Per-connector identity for multi-connector hardware.

- `connector_no` is 1-based and is the index used to split the frame's colon-delimited `as` field.
- `retired = 1` excludes a connector from fan-out.
- `panel` is stored but never read by this service.
- **Implicit requirement:** the fan-out path expects a `charge_points` row whose `cp_ident` equals this table's `connector_ident`. The shipped seed data does not create those rows.

### `revisions` / `revision_details`

Field-level change audit. Header plus one detail row per changed column.

- The header is polymorphic in design (`tname` + `target_id`), which is why no FK is possible. In practice every row written by this service is `who = 0`, `tname = 'ChargePoint'`, `op = 'U'`.
- `target_id` holds a `cp_ident` string — for fan-out, a `connector_ident`.
- `at_`, `before_`, `after_` carry trailing underscores to dodge SQL reserved words. Keep the convention if you add columns.
- `before_`/`after_` are `VARCHAR(255)`, so all values are stringified and long values are truncated.
- Details are accumulated in memory and flushed at the end of the frame; any detail still holding a `NULL` `rev_id` at flush time is **silently discarded**.
- `KEY k_t (tname, target_id)` supports "show me the history of this unit".

---

## Seed data

`sql/schema.sql` ends with fixture rows. This is what a fresh `docker compose up` gives you.

| `id` | `cp_ident` | `model_code` | `connector_count` | `tz` | Exercises |
|---|---|---|---|---|---|
| 1 | `CP-0001` | 0 | 1 | `America/Chicago` | the simple path |
| 2 | `CP-0002` | **7** | 3 | `America/Chicago` | multi-connector fan-out |
| 3 | `CP-0003` | 0 | 1 | **`America/Denver`** | non-default timezone conversion |
| 4 | `CP-0004` | 253 | 2 | `America/Chicago` | legacy model code, still the simple path |
| 10–21 | `CP-0010`…`CP-0021` | 0 | 1 | `America/Chicago` | bulk fleet, generated by a cross-join |

Note the id gap: **ids 5–9 do not exist.** The bulk generator starts at 10.

`connectors` is seeded with three rows, all under `cp_id = 2`: `CP-0002-1` (no 1, panel 0), `CP-0002-2` (no 2, panel 0), `CP-0002-3` (no 3, panel 1).

There are **no** `charge_points` rows for `CP-0002-1`, `CP-0002-2`, or `CP-0002-3`, which is why the fan-out path writes no readings on stock seed data.

---

## Working with the schema

`sql/schema.sql` is mounted read-only into the MySQL container's `/docker-entrypoint-initdb.d/`. That directory is only executed **when the data volume is empty**, so:

```bash
# editing sql/schema.sql then restarting does NOTHING
docker compose down -v && docker compose up -d     # required to apply schema changes
```

The script itself is re-runnable by design — it opens with `SET FOREIGN_KEY_CHECKS = 0`, drops all six tables in dependency order, then recreates them. Running it against a live database destroys all data.

There is no migration tooling. The file is the only schema artifact, and changes to it must be coordinated with the downstream reporting and settlement services, which read these tables but do not own migrations against them.

---

## Related documents

- [Domain Overview](domain-overview.md) — what these tables represent
- [Sequence Diagrams](sequence-diagrams.md) — the write patterns against them
- [Architecture](architecture.md) — transaction and connection model
- [Known Issues](known-issues.md) — where the model and the code disagree
- [Integration Points](integration-points.md) — every external touchpoint and its blast radius
- [Stability Spec](stability-spec.md) — the invariants that must survive any change
