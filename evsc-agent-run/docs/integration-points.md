# Integration Points

Every place this service touches something outside its own process memory. Compiled by grepping `src/`, `bin/`, `Dockerfile`, and `docker-compose.yml` for all I/O primitives, then confirming behavior against a running stack.

Each entry has a stable id so it can be referenced from code review or incident notes.

## Summary

| Category | Count | Blast radius |
|---|---|---|
| [Database reads](#database-reads) | 7 statements | Contained |
| [Database writes](#database-writes) | 20 statements across 5 tables | **Shared with two other teams** |
| [Outbound network](#outbound-network) | 1 HTTP call | **Unbounded — no timeout** |
| [Environment variables](#environment-variables) | 10 | Silent defaults, no validation |
| [Filesystem](#filesystem) | 3 (2 read, 1 write) | Contained |
| [Process and OS](#process-and-os) | 6 | Contained |
| [Clocks](#clocks-and-timezones) | 2 independent clocks | **Correctness-relevant** |
| [Container / orchestration](#container-and-orchestration) | 8 | Deploy-time |
| [Implicit cross-team contracts](#implicit-cross-team-contracts) | 4 | **Organizational** |

**The three that should worry you:** the untimed HTTP call inside an open write transaction ([NET-1](#net-1--posted-tariff-lookup)), the two independently-configured clocks writing into the same tables ([CLK-1/CLK-2](#clocks-and-timezones)), and the fact that `inbox` is a **shared, bidirectionally-written table** with an upstream team ([X-1](#x-1--inbox-is-a-shared-write-surface)).

There is **no** message broker, queue, cache, object store, mail, DNS-service-discovery, gRPC, webhook, cron, signal handler, subprocess, temp file, or lock file anywhere in this codebase. Absences confirmed by grep — see [Confirmed absences](#confirmed-absences).

---

## Database reads

All via `Cx::q()`. Connection column matters: reads on `$readConnection` are a **separate MySQL session** and cannot see the current frame's uncommitted writes.

| id | Line | Connection | Statement | Notes |
|---|---|---|---|---|
| DB-R1 | `ingest.php:34` | write | `SELECT id FROM inbox WHERE status='new' AND (CRC32(cp_ident) % {shards}) = {shard} ORDER BY id LIMIT {limit} FOR UPDATE SKIP LOCKED` | The claim query. `{shards}`, `{shard}`, `{limit}` are **string-interpolated**, safe only via `(int)` casts. Requires MySQL 8.0+. |
| DB-R2 | `ingest.php:45` | write | `SELECT * FROM inbox WHERE status = ?` | Re-reads own batch by claim token |
| DB-R3 | `ingest.php:70` | read | `SELECT id FROM meter_events WHERE inbox_id = ?` | The idempotency guard. No unique index backs it — application-level only |
| DB-R4 | `ingest.php:120` | read | `SELECT * FROM charge_points WHERE cp_ident = ?` | Uses `UNIQUE(cp_ident)` |
| DB-R5 | `ingest.php:196` | read | `SELECT * FROM connectors WHERE cp_id = ? AND retired = 0` | **The only reference to `connectors` in the entire codebase** |
| DB-R6 | `ingest.php:225` | read | `SELECT id FROM charge_points WHERE cp_ident = ?` | Looks up a *connector ident* as a charge point. Usually finds nothing — see [Known Issues #1](known-issues.md#1-a-multi-connector-frame-can-be-marked-done-having-written-no-readings) |
| DB-R7 | `ingest.php:268` | read | `SELECT rollup_date FROM meter_events WHERE id = ?` | Dereferences `charge_points.rollup_event_id`, which has no FK |

---

## Database writes

All via `Cx::ex()` on `$writeConnection`. **Twenty statements against five tables.**

### `inbox` — 8 write sites (shared table, see [X-1](#x-1--inbox-is-a-shared-write-surface))

| id | Line | Statement | Trigger |
|---|---|---|---|
| DB-W1 | `:41` | `UPDATE inbox SET status = <token> WHERE id IN (...)` | Claim. Placeholder list built with `array_fill` + `implode` |
| DB-W2 | `:74` | `UPDATE inbox SET status='done'` | Replay detected |
| DB-W3 | `:94` | `UPDATE inbox SET status='bad'` | JSON decode failure or missing key `1` |
| DB-W4 | `:114` | `UPDATE inbox SET status='bad'` | Timestamp >2 days future |
| DB-W5 | `:122` | `UPDATE inbox SET status='bad'` | Unknown `cp_ident` |
| DB-W6 | `:161` | `UPDATE inbox SET received_at = ?` | `msg_type 3` with no `la`. **Rewrites upstream-owned data** with a site-local timestamp |
| DB-W7 | `:218` | `INSERT INTO inbox (src, status, cp_ident, body, body_hash, received_at) VALUES (?, 'done', ...)` | Multi-connector fan-out. **This service manufactures rows in the upstream team's intake table**, pre-marked `done`, carrying the parent's `body_hash` |
| DB-W8 | `:247` | `UPDATE inbox SET status='done'` | Normal completion |

### `charge_points` — 3 write sites

| id | Line | Statement | Trigger |
|---|---|---|---|
| DB-W9 | `:88` | `UPDATE charge_points SET last_seen_at = ? WHERE cp_ident = ?` | Any frame with `src != 'gw'`. Runs **before** validation, so rejected frames still land it |
| DB-W10 | `:154` | `UPDATE charge_points SET link_state = ?, flags = flags \| 4 WHERE id = ?` | `msg_type` 9/11/14. Read-modify-write on `flags` done in SQL, so it is atomic |
| DB-W11 | `:378` | `UPDATE charge_points SET \`col\` = ?, ... WHERE id = ?` | **Dynamically assembled.** Column names interpolated from `array_keys($chargePointUpdates)`, backtick-quoted, filtered by `array_key_exists($fieldName, $chargePoint)`. Values bound. Skipped entirely for `model_code == 7` unless `msg_type == 3` |

### `meter_events` — 2 write sites

| id | Line | Columns | Trigger |
|---|---|---|---|
| DB-W12 | `:227` | 8 columns; omits `rollup_date`, `rollup_hour`, `fault_note` | Per-connector, multi-connector path only |
| DB-W13 | `:239` | 11 columns | Simple path (`model_code != 7`) |

Both truncate `raw` to `substr($body, 0, 250)` against a `VARCHAR(255)` column.

### `revisions` — 4 write sites, all identical SQL

| id | Line | `target_id` | Trigger |
|---|---|---|---|
| DB-W14 | `:152` | parent `cp_ident` | Link-state frame |
| DB-W15 | `:264` | parent `cp_ident` | Rollup advancement |
| DB-W16 | `:288` | parent `cp_ident` | Any field change |
| DB-W17 | `:300` | **connector ident** | Per-connector audit header |

All four call sites execute the same string, `INSERT INTO revisions (who, tname, target_id, op, at_) VALUES (0, 'ChargePoint', :t, 'U', NOW())` — the only **named** placeholder in the codebase, and the only use of MySQL's `NOW()`. Three sites assign the literal (`:149`, `:263`, `:287`); DB-W17 reuses the variable. See [Clocks and timezones](#clocks-and-timezones).

### `revision_details` — 3 write sites

| id | Line | Trigger |
|---|---|---|
| DB-W18 | `:156` | Link-state change, written immediately |
| DB-W19 | `:361` | Per-connector fan-out flush |
| DB-W20 | `:382` | Simple-path flush |

DB-W19 and DB-W20 both skip rows whose `rev_id` is `NULL`, silently dropping audit entries queued before their header existed.

### Tables never written

**`connectors` is read-only to this service.** It appears exactly once in `src/` (DB-R5) and is never inserted, updated, or deleted. Connector provisioning and retirement happen entirely outside this codebase — in practice only via the seed block in `sql/schema.sql`. Adding a connector to a live multi-connector unit is a manual DBA operation, and the fan-out path additionally expects a matching `charge_points` row that nothing creates.

### Connection-level integration

| id | Detail |
|---|---|
| DB-C1 | DSN: `mysql:host={h};port={p};dbname={d};charset=utf8mb4` (`db.php:59`). Non-persistent |
| DB-C2 | `ATTR_ERRMODE = ERRMODE_EXCEPTION` — every SQL error throws |
| DB-C3 | `ATTR_EMULATE_PREPARES = false` — real server-side prepares. Every `ex()`/`q()` call re-prepares; nothing is cached |
| DB-C4 | `ATTR_DEFAULT_FETCH_MODE = FETCH_ASSOC` |
| DB-C5 | **Two connections per worker process.** Observed `max_connections = 151`, so roughly 75 workers is the ceiling before connections are exhausted |
| DB-C6 | Observed isolation `REPEATABLE-READ`. Combined with two sessions, `$readConnection` holds a snapshot and will not observe the write session's uncommitted rows |
| DB-C7 | Observed `innodb_lock_wait_timeout = 50` seconds. Relevant because DB-W9 can contend across workers for the same `cp_ident` while [NET-1](#net-1--posted-tariff-lookup) blocks with no timeout |
| DB-C8 | Version coupling: `FOR UPDATE SKIP LOCKED` requires **MySQL 8.0+**. Verified running 8.0.46. `CRC32()` is also MySQL-specific — the claim query is not portable |

---

## Outbound network

### NET-1 — Posted tariff lookup

The **only** outbound network call in the service. `fetchPostedTariff()`, `src/ingest.php:8-22`.

```php
$rateServiceUrl = getenv('LOOKUP_URL');
if (!$rateServiceUrl) { usleep(120000 + random_int(0, 90000)); return null; }
$rateRequest = curl_init();
curl_setopt($rateRequest, CURLOPT_URL, "{$rateServiceUrl}/r/{$latitude}/{$longitude}");
curl_setopt($rateRequest, CURLOPT_RETURNTRANSFER, 1);
$rateResponse = curl_exec($rateRequest);
```

| Property | Value |
|---|---|
| Protocol | HTTP(S) GET via ext-curl (enabled in `php:8.2-cli`, verified) |
| URL shape | `{LOOKUP_URL}/r/{latitude}/{longitude}` |
| Response contract | JSON; the code reads `$decoded->now->v` |
| Called from | `ingest.php:173`, only when a fault note matches and `charge_points.tariff` is not null |
| Call site context | **Inside the open write transaction**, after `begin()` at `:83` |

Risks, in order of severity:

1. **No timeout of any kind.** Neither `CURLOPT_TIMEOUT` nor `CURLOPT_CONNECTTIMEOUT` is set; the curl default is `0`, meaning no limit (verified). A hung rate service blocks the worker **indefinitely** while holding row locks on `inbox` and `charge_points`. Peer workers contending on those rows hit `innodb_lock_wait_timeout` after 50s and throw, which sends their frames down the strand path in [Known Issues #5](known-issues.md#5-stranded-frames-are-unrecoverable-without-manual-sql).
2. **No retry, no backoff, no circuit breaker.** A failed call returns `null`, which the caller treats as "tariff unknown" and proceeds.
3. **Errors are invisible.** `curl_exec` returning `false` is indistinguishable from a legitimately empty tariff. Nothing is logged; no HTTP status code is inspected.
4. **`LOOKUP_URL` is unvalidated and interpolated.** No scheme or host check. Latitude and longitude are also interpolated straight into the path with no encoding, defaulting to `0` when absent (`:173`).
5. **The unconfigured path still costs latency.** With `LOOKUP_URL` unset the function sleeps 120–210 ms and returns `null` — inside the transaction. This is the *normal* configuration: `LOOKUP_URL` is not set in `docker-compose.yml`.

There is no inbound network integration at all. The worker opens no socket, binds no port, and exposes no health or metrics endpoint.

---

## Environment variables

Every read uses `getenv('X') ?: default`. There is no `$_ENV`, no `$_SERVER`, no `putenv`, no `.env` file, and no validation anywhere — **every variable fails soft**.

| id | Variable | Line | Default | Documented in README? | Set in compose? |
|---|---|---|---|---|---|
| ENV-1 | `DB_HOST` | `db.php:54` | `127.0.0.1` | yes | yes |
| ENV-2 | `DB_NAME` | `db.php:55` | `evse` | yes | yes |
| ENV-3 | `DB_USER` | `db.php:56` | `ingest_svc` | yes, but shown as having **no** default | yes |
| ENV-4 | `DB_PASS` | `db.php:57` | `ingest_svc` | yes, but shown as having **no** default | yes |
| ENV-5 | `DB_PORT` | `db.php:58` | `3306` | yes | yes |
| ENV-6 | `BATCH` | `ingest.php:59` | `4` | yes | no |
| ENV-7 | `POLL_INTERVAL_US` | `worker.php:12` | `250000` | yes | no |
| ENV-8 | `LOOKUP_URL` | `ingest.php:11` | unset | **no** | no |
| ENV-9 | `SHARD` | `ingest.php:31` | `0` | **no** | no |
| ENV-10 | `SHARDS` | `ingest.php:32` | `1` | **no** | no |

Behavioral notes:

- **Credential fallback is a security-relevant default.** A deployment that forgets `DB_USER`/`DB_PASS` silently connects as `ingest_svc/ingest_svc` rather than failing fast.
- **`POLL_INTERVAL_US` is read once**, at startup (`worker.php:12`). `BATCH` is re-read **every cycle** (`ingest.php:59`), so it responds to a changed environment on restart-free config reload — except nothing reloads it.
- **`SHARD`/`SHARDS` reach SQL by interpolation**, not binding (DB-R1). The `(int)` casts are the only injection defense.
- **`SHARDS=0` would produce `% 0`** in MySQL. The `?: 1` guard prevents it for an unset variable, but an explicit `SHARDS=0` passes the falsy check and also yields `1` — so this is safe by accident, not by validation.
- No variable is logged at startup. There is no way to confirm from the logs which configuration a worker is running.

---

## Filesystem

| id | Operation | Line | Notes |
|---|---|---|---|
| FS-1 | `require_once __DIR__ . '/../src/ingest.php'` | `worker.php:10` | Read. `__DIR__`-relative, so CWD-independent |
| FS-2 | `require_once __DIR__ . '/db.php'` | `ingest.php:3` | Read. The worker never requires `db.php` directly |
| FS-3 | `fwrite(STDOUT, ...)` | `db.php:69` | **The only write to the filesystem or any stream in the entire service** |

No `file_put_contents`, `fopen`, `unlink`, `mkdir`, `rename`, `copy`, `tmpfile`, `tempnam`, PID file, lock file, or temp directory usage. Confirmed by grep.

---

## Process and OS

| id | Integration | Line | Notes |
|---|---|---|---|
| OS-1 | `fwrite(STDOUT, '[' . getmypid() . '] ' . $s)` | `db.php:69` | The sole logging facility. Format `[<pid>] <message>`. Nothing goes to STDERR |
| OS-2 | `getmypid()` | `db.php:69` | Only used for the log prefix |
| OS-3 | `random_bytes(6)` | `ingest.php:30` | Kernel CSPRNG. Generates the claim token. Throws on entropy failure — which would propagate uncaught out of `claimBatch()` |
| OS-4 | `random_int(0, 90000)` | `ingest.php:12` | CSPRNG jitter for the unconfigured-tariff sleep |
| OS-5 | `usleep($pollIntervalUs)` | `worker.php:16` | Unconditional per-cycle sleep |
| OS-6 | `usleep(120000 + ...)` | `ingest.php:12` | Sleep inside an open transaction |

**No process integration beyond this.** Grep confirms no `exec`, `shell_exec`, `system`, `passthru`, `proc_open`, `popen`, `pcntl_*`, or `posix_*`. Consequences:

- **No signal handling.** `SIGTERM` from `docker compose stop` or a Kubernetes eviction kills the process mid-frame. In-flight transactions are rolled back by MySQL on connection loss, which strands the claimed frames.
- **No graceful shutdown, no drain, no exit code discipline.** `bin/worker.php` cannot exit; the loop has no break condition.
- **No `set_error_handler`, `set_exception_handler`, or `register_shutdown_function`.** PHP warnings — of which the code generates several, e.g. undefined array keys at `:151` and `:183` — go to PHP's default output and are neither captured nor suppressed.

---

## Clocks and timezones

The service reads **two independent clocks** and writes both into the same tables. This is an integration point people miss.

| id | Clock | Used at | Writes into |
|---|---|---|---|
| CLK-1 | **PHP process clock**, in PHP's default timezone | `ingest.php:111` (`date('Y-m-d H:i:s', time())`), `:110`, `:139-141`, `:160` | `meter_events.utc_event_at`, `meter_events.local_event_at`, `charge_points.rollup_at`, `inbox.received_at` |
| CLK-2 | **MySQL server clock**, in MySQL's session timezone | `NOW()` in DB-W14…W17 | `revisions.at_` |

Verified defaults in the shipped stack:

| Setting | Observed |
|---|---|
| PHP `date.timezone` in `php:8.2-cli` | `UTC` (explicitly set in the image ini) |
| MySQL `@@system_time_zone` | `UTC` |
| MySQL `@@global.time_zone` / `@@session.time_zone` | `SYSTEM` → resolves to UTC |
| `TZ` env var on the `app` container | not set |

So today both clocks agree. But they are configured through **entirely separate mechanisms** — a `TZ` environment variable or a `php.ini` override moves CLK-1; a MySQL `my.cnf` or `--default-time-zone` moves CLK-2. Nothing detects divergence, and `revisions.at_` would silently drift away from the `meter_events` timestamps it audits.

A second, subtler mismatch exists independent of configuration. The future-timestamp guard at `:109-118` compares the frame's `la` field — the **charger's local wall clock**, carrying no timezone — against `date('Y-m-d H:i:s', time())`, which is **PHP's default timezone**. For a charge point on `America/Chicago` the effective rejection window is therefore 2 days plus the site's UTC offset, not 2 days. The conversion that *does* respect `charge_points.tz` happens later, at `:139`.

Additional time coupling: the IANA timezone database inside the PHP container resolves `charge_points.tz` strings such as `America/Denver`. An unknown or misspelled `tz` value makes `new DateTimeZone($siteTimezone)` throw, sending the frame down the strand path.

---

## Container and orchestration

| id | Integration | Defined in | Notes |
|---|---|---|---|
| CONT-1 | `COPY . .` at image build | `Dockerfile:6` | Bakes the whole working tree in. No `.dockerignore`, so any local file present at build time ships. Code changes need `docker compose up -d --build`, not `restart` |
| CONT-2 | `docker-php-ext-install pdo_mysql` | `Dockerfile:3` | The only extension added. `ext-curl` is inherited from the base image |
| CONT-3 | `CMD ["php", "bin/worker.php"]` | `Dockerfile:8` | Single foreground process, PID 1. No init, no supervisor, so no zombie reaping — harmless given no subprocesses |
| CONT-4 | Schema bind mount `./sql/schema.sql → /docker-entrypoint-initdb.d/00-schema.sql:ro` | `docker-compose.yml:10` | Executed by the MySQL entrypoint **only on an empty data volume**. Schema edits require `docker compose down -v` |
| CONT-5 | `db` healthcheck `mysqladmin ping -h 127.0.0.1 -uingest_svc -pingest_svc` | `docker-compose.yml:11-15` | 3s interval, 20 retries. Credentials are in the healthcheck command line |
| CONT-6 | `depends_on: db: condition: service_healthy` | `docker-compose.yml:19-21` | Startup ordering only. Does **not** protect against MySQL restarting later — the worker has no reconnect logic |
| CONT-7 | `restart: unless-stopped` on `app` | `docker-compose.yml:28` | The entire crash-recovery strategy. No `app` healthcheck exists, so a hung-but-alive worker is never restarted |
| CONT-8 | Compose default network + DNS name `db` | implicit | `DB_HOST=db` resolves via Docker's embedded DNS. No `ports:` on either service, so MySQL is **not reachable from the host** — inspection must go through `docker compose exec` |

Runtime facts observed by inspecting the running container, which are **host-daemon defaults rather than repo configuration**:

- Log driver `json-file` with `max-size=10m, max-file=3`. Rotation is a property of *this* Docker daemon, not of the repo. On a daemon without those defaults, stdout grows without bound.
- The container runs as **root** — the `Dockerfile` has no `USER` directive.
- Zero mounts on `app`, confirming no source bind-mount.

Credentials `ingest_svc/ingest_svc` appear in plaintext in `docker-compose.yml` and `src/db.php` defaults. There is no secret manager integration.

---

## Implicit cross-team contracts

Integration points with no code and no API — the ones that break in a meeting rather than in a stack trace.

### X-1 — `inbox` is a shared write surface

`inbox` is owned by the upstream field-gateway intake layer, but this service does **not** merely read it. It performs seven distinct writes: five status transitions (DB-W1…W5, W8), an **`UPDATE` of upstream-authored `received_at`** (DB-W6), and an **`INSERT` of entirely new rows** (DB-W7).

The synthetic rows in DB-W7 are pre-marked `done`, carry a duplicated `body_hash`, and hold a body mutated by string surgery rather than JSON re-encoding. Any upstream reconciliation that counts rows, trusts `body_hash` uniqueness, or assumes it authored every row in the table will disagree with reality.

### X-2 — Downstream readers with no contract

Reporting and settlement read `charge_points`, `meter_events`, and `revisions` directly. They own no migrations against this schema, so any column change here is a silent breaking change for them. There is no view layer, no API, and no schema version marker — coupling is at the raw table level.

Two live hazards for these consumers, both documented in [Known Issues](known-issues.md): `charge_points.last_event_id` can point into the `inbox` table (#3), and multi-connector parent rows are never updated for ordinary telemetry (#2).

### X-3 — Connector provisioning is unowned

`connectors` is read-only to this service (DB-R5) and to every other component in this repo. Nothing creates, updates, or retires connector rows, and nothing creates the per-connector `charge_points` rows that the fan-out path at DB-R6 requires. Whoever provisions multi-connector hardware does so out of band.

### X-4 — Charge point provisioning is unowned

`charge_points.id` is a plain `INT PRIMARY KEY` with no `AUTO_INCREMENT`. This service never inserts a charge point — it only marks frames `bad` when `cp_ident` is unknown (DB-W5). Row creation and id allocation belong to an unidentified upstream process.

---

## Confirmed absences

Searched for and **not present** anywhere in `src/`, `bin/`, or the container definition. Listed because their absence is itself load-bearing information.

| Category | Searched for | Result |
|---|---|---|
| Message brokers | AMQP, Kafka, Redis, SQS, NATS, pub/sub, any queue client | none |
| Inbound network | socket, bind, listen, HTTP server, port publication | none |
| Caching | Redis, Memcached, APCu, in-process memoization | none |
| Object / file storage | S3, GCS, blob clients, uploads | none |
| Email / notifications | mail, SMTP, webhooks, Slack, PagerDuty | none |
| Subprocesses | `exec`, `shell_exec`, `system`, `passthru`, `proc_open`, `popen` | none |
| Signals / IPC | `pcntl_*`, `posix_*`, shared memory, semaphores, file locks | none |
| File writes | `file_put_contents`, `fopen`, `unlink`, `mkdir`, temp files, PID files | none (only `fwrite(STDOUT)`) |
| Observability | metrics, Prometheus, StatsD, tracing, OpenTelemetry, `error_log`, syslog | none |
| Health / readiness | `app` healthcheck, HTTP probe, liveness file | none |
| Error handling hooks | `set_error_handler`, `set_exception_handler`, `register_shutdown_function` | none |
| Auth / identity | tokens, JWT, OAuth, credential rotation, secret manager | none — static credentials only |
| Scheduling | cron, timers, at-jobs | none — the `while(true)` loop is the only scheduler |
| Feature flags | flag client, config service, dynamic reload | none |
| Migrations | any migration tool or version table | none — `sql/schema.sql` is the only artifact |

---

## Risk register

| Risk | Integration | Severity | Mitigation if this service matters |
|---|---|---|---|
| Hung tariff service blocks workers indefinitely while holding row locks | NET-1 | **High** | Set `CURLOPT_TIMEOUT` and `CURLOPT_CONNECTTIMEOUT`; move the call outside the transaction |
| No reconnect after MySQL restart; `depends_on` only covers startup | CONT-6, DB-C1 | **High** | Rely on `restart: unless-stopped` today; the process must die to recover |
| `SIGTERM` mid-frame strands claimed rows | OS (no signals) | **High** | Add a signal handler that finishes the current frame and breaks the loop |
| Missing `DB_USER`/`DB_PASS` silently uses default credentials | ENV-3, ENV-4 | Medium | Fail fast when unset |
| Two independently-configured clocks write into the same tables | CLK-1, CLK-2 | Medium | Use one clock; prefer PHP `UTC_TIMESTAMP` equivalents or MySQL for both |
| Hung-but-alive worker never restarted | CONT-7 | Medium | Add an `app` healthcheck |
| Connection ceiling ~75 workers | DB-C5 | Medium | Raise `max_connections` or share one connection |
| Synthetic `inbox` rows confuse upstream reconciliation | X-1, DB-W7 | Medium | Agree the contract with the intake team, or move fan-out to an owned table |
| Unbounded stdout on daemons without rotation defaults | OS-1, CONT-7 | Low | Set explicit `logging:` options in compose |
| Container runs as root | CONT-1 | Low | Add a `USER` directive |
| Plaintext credentials in compose, code defaults, and healthcheck | CONT-5, ENV-3/4 | Low | Use secrets |

---

## Consistency with AGENTS.md and the other documents

Cross-checked against [`AGENTS.md`](../AGENTS.md), [`architecture.md`](architecture.md), [`data-model.md`](data-model.md), [`sequence-diagrams.md`](sequence-diagrams.md), and [`known-issues.md`](known-issues.md), and re-verified against `src/`.

**No contradictions found.** All statement counts, line numbers, connection semantics, and environment defaults agree with what those documents already state.

This document adds five integration facts that were not previously recorded anywhere. All five were verified — the first four by grep over the full source, the fifth against the running stack:

1. **NET-1 sets no curl timeout.** The blocking-inside-a-transaction risk was noted before; the *absence of any timeout* was not. Confirmed: only `CURLOPT_URL` and `CURLOPT_RETURNTRANSFER` are set, and curl's default timeout is `0`.
2. **`connectors` is read-only to this service** (X-3). One SELECT, zero writes, in the whole codebase.
3. **This service writes to the upstream-owned `inbox` table in three ways**, not just status transitions (X-1) — including `UPDATE`ing `received_at` and `INSERT`ing new rows.
4. **`charge_points` is never inserted by this service** (X-4); id allocation is external.
5. **Two independent clocks** feed the same tables (CLK-1, CLK-2), with `revisions.at_` alone coming from MySQL.

Items 1 and 2 have been added to [`AGENTS.md`](../AGENTS.md) as gotchas 17 and 18, since both change how you would safely edit `src/ingest.php`. Items 3–5 are cross-team and schema-level rather than editing hazards, so they live here and are linked from `AGENTS.md`.

---

## Related documents

- [Domain Overview](domain-overview.md) — ownership boundaries in the data plane
- [Architecture](architecture.md) — transaction and connection model
- [Data Model](data-model.md) — the tables these writes target
- [Sequence Diagrams](sequence-diagrams.md) — write ordering within a frame
- [Known Issues](known-issues.md) — defects that exploit these integration points
- [Stability Spec](stability-spec.md) — the invariants that must survive any change
