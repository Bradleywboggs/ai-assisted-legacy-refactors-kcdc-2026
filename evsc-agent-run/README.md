# evse-ingest

Ingest and normalization service for charge point telemetry. Consumes raw
frames landed by the field gateway, decodes them, and updates charge point
and connector state.

## What it does

Charge points and OCPP-speaking gateways post frames to a shared `inbox`
table (owned by the field-gateway intake layer — not part of this repo).
This service claims batches from that table, decodes each frame, and:

- updates the charge point's live state (link state, firmware, fault note,
  reported model, tariff-adjusted alert flags)
- writes one `meter_events` row per energy reading, including per-connector
  readings on multi-connector hardware
- records a `revisions` / `revision_details` audit trail of every field
  change it makes
- marks the inbox frame `done`, or `bad` if it can't be decoded

It runs as a long-lived worker daemon, one process per core allotted to the
deployment. There is no coordination between instances beyond what the
database enforces.

## Documentation

Full documentation for newcomers lives in [`docs/`](docs/README.md):

- [Domain Overview](docs/domain-overview.md) — what this service is for and where it
  sits in the wider data plane, explained at five levels of precision
- [Architecture](docs/architecture.md) — system, container, and component diagrams,
  plus the control flow inside `processInboxBatch()`
- [Sequence Diagrams](docs/sequence-diagrams.md) — the core dataflows at six zoom
  levels, from platform-wide down to individual SQL statements
- [Data Model](docs/data-model.md) — entity relationship diagram, per-table
  reference, and the `inbox.status` lifecycle
- [Known Issues](docs/known-issues.md) — fifteen verified defects with reproductions;
  read before changing `src/ingest.php`
- [Stability Spec](docs/stability-spec.md) — the invariants that must hold
  through any change, and the behaviors consumers may depend on
- [Integration Points](docs/integration-points.md) — every external touchpoint:
  all SQL statements, the one outbound HTTP call, environment variables, clocks,
  container coupling, and cross-team contracts
- [Test suites](tests/README.md) — the regression gates, and the rule that a
  refactor must never re-record a baseline

[`AGENTS.md`](AGENTS.md) covers conventions and commands for editing the code;
the [`Makefile`](Makefile) wraps every development command.

## Requirements

- PHP 8.2, `pdo_mysql` — inside the container only; **not needed on the host**
- MySQL 8.0, InnoDB
- To develop: `docker` (with compose v2), `make`, `bash`, `python3`, and `jq`

## Running locally

All development commands are wrapped by the [`Makefile`](Makefile). Run `make`
with no arguments for the full target list.

```bash
make up          # start the stack (builds the image)
make smoke       # inject one frame end-to-end and report what happened
make logs        # follow worker output
make down        # stop, keeping data
make nuke        # stop and drop the data volume — required to apply schema changes
```

`make smoke` replaces the `bin/seed.php` fixture that `README` used to document:
**that script is not present in this repository.** The smoke target injects a
frame directly into `inbox`, waits for the worker to claim it, and prints the
resulting rows from `inbox`, `meter_events`, and the audit trail.

Because the `Dockerfile` uses `COPY . .` and the `app` service has no bind
mount, a code change needs `make rebuild`, not a restart.

### Testing

```bash
make test        # both suites — the regression gate
make verify      # lint + doc links + both suites (pre-merge)
```

Behavior is pinned by two suites: a characterization suite at
[`tests/characterization/`](tests/characterization/) and a property suite at
[`tests/property/`](tests/property/). Read [`tests/README.md`](tests/README.md)
before changing anything — it explains why a refactor must never re-record a
baseline, and what to do when behavior changes on purpose.

## Configuration

| variable | default | meaning |
|---|---|---|
| `DB_HOST` | `127.0.0.1` | MySQL host |
| `DB_PORT` | `3306` | MySQL port |
| `DB_NAME` | `evse` | database name |
| `DB_USER` | — | database user |
| `DB_PASS` | — | database password |
| `BATCH` | `4` | inbox frames claimed per cycle |
| `POLL_INTERVAL_US` | `250000` | sleep between claim attempts when idle |

## Schema

Owned by this service — `sql/schema.sql` is the source of truth. Six tables:

- `charge_points` — one row per physical unit; connector-level fields on
  multi-connector hardware are folded onto the parent row
- `connectors` — per-connector identity for multi-connector hardware
- `inbox` — raw frames, pre-decode
- `meter_events` — decoded energy readings
- `revisions` / `revision_details` — field-level change audit

Downstream services that read this schema (reporting, settlement) do not own
migrations against it; changes here should be coordinated with them
separately.
