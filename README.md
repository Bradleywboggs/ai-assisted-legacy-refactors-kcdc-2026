# AI-Assisted Legacy Refactors

Talk materials and worked example for **KCDC 2026**.

**Brad Boggs** — Senior Software Engineer, Ag Growth International · Principal, Contravariant LLC

---

## The problem

Legacy systems resist change for three reasons that stack: we don't understand them, we
don't have tests that codify the understanding when it isn't in our heads, and there is a
real business cost to getting it wrong. So the fixes get deferred, indefinitely.

The literature's answer — write the tests, build confidence over time — is correct and has
always been too expensive to actually do.

For a legacy system with no external specification, correctness is not an external target.
It is whatever preserves system stability. Which, per Hyrum's Law, means:

> **The specification is what must remain true at the edges, no matter what else changes.**

Recovering that has historically been weeks of archaeology. It now takes an afternoon.

This repository is the worked example.

---

## What's here

| Path | What it is |
|---|---|
| [`PROMPTS.md`](PROMPTS.md) | The nine prompts, verbatim and in order. **Start here.** |
| [`evsc-agent-run/`](evsc-agent-run/) | The example legacy application, its generated docs, and both test suites |
| `AI_Assisted_Legacy_Refactors.pdf` | The slides — also as `.pptx` and Keynote `.key` |
| `*.cast.gz` | Three asciinema recordings — full session, refactor, bug fix |

---

## The workflow

Nine prompts, in order. Full text in **[`PROMPTS.md`](PROMPTS.md)** — copy them, they are
not harness-specific.

| # | Step | Output |
|---|------|--------|
| 1 | Give the agent context | `AGENTS.md` (or `CLAUDE.md`) |
| 2 | Generate docs for a human | `docs/` — domain, architecture, sequence, data model |
| 3 | Document the integration points | `docs/integration-points.md` — **the edge inventory** |
| 4 | Build the characterization suite | `tests/characterization/` |
| 5 | Gather real test data | fixtures + property suite |
| 6 | Verify green against unchanged code | the baseline |
| 7 | Produce the specification | `docs/stability-spec.md` |
| 8 | Wrap it in a script | `Makefile` |
| 9 | Commit, push, circulate | — |

Then, and only then, start making changes.

### Two rules the whole thing rests on

**The test suite is the artifact. The spec document is derived commentary** — reviewable,
useful, and unverified. If the two disagree, the suite wins.

**Characterization pins the behavior your inputs exercise. Nothing else.** Paths you never
hit stay unpinned, and the suite will sit there green while you break them. Seed from
production-shaped data where you can; that is what the property suite is for where you
can't.

---

## The example application

[`evsc-agent-run/`](evsc-agent-run/) — `evse-ingest`, a charge point telemetry ingestion
worker in PHP against MySQL 8.

It is a scaled-down, IP-scrubbed reconstruction of a real production application, recast
into an unrelated domain. Same pathologies, none of the source: one long method carrying the
entire unit of work, a transaction spanning nearly all of it with a synchronous HTTP call
inside, an audit trail written from mutating locals, and no tests.

Fifteen verified defects. Every one is reproduced, documented, and **pinned as-is** by the
characterization suite — including the ones that are obviously wrong. That is the point.

### Requirements

Everything runs from the host with **no `pip install` step** — the Python here is stdlib
only, and PHP, MySQL, and the linter all live in containers.

| Requirement | Minimum | Why |
|---|---|---|
| **Docker** with **Compose v2** | any current release | MySQL 8.0 and the PHP 8.2 app image; the stack uses `depends_on: condition: service_healthy` |
| **Python 3.9+** | 3.9 | Both suite runners and the smoke test. The floor is `zoneinfo`, added in 3.9 |
| **GNU Make** | 3.81 | The `Makefile` is the only entry point |
| **bash** | 3.2 | `make` sets `SHELL := /usr/bin/env bash` with `pipefail`. bash 4 is **not** required |
| `gzip`, `awk`, `printf` | — | Standard POSIX userland |
| **asciinema** | any | Optional — only to replay the session recordings |

You do **not** need PHP, MySQL, Composer, `jq`, or any Python package on the host.

### Platform support

**macOS and Linux** — works as-is. Docker Desktop, Colima, OrbStack, or Docker Engine with
the Compose v2 plugin are all fine.

**Windows — use WSL2.** Native Windows shells are not supported, and this is not a
portability oversight I can paper over: the `Makefile` sets `SHELL := /usr/bin/env bash`,
both suite runners are invoked through their `#!/usr/bin/env python3` shebangs and an
executable bit, and the help target is an `awk` script. None of that resolves under
PowerShell or `cmd`. Git Bash gets you bash but still has no `make`, and does not honor
shebang execution the way the Makefile expects.

The supported path on Windows:

1. Install **WSL2** with a current Ubuntu (or equivalent) distribution.
2. Install **Docker Desktop** and enable **WSL2 integration** for that distribution
   (Settings → Resources → WSL Integration).
3. Clone this repository **inside the WSL2 filesystem** — `~/src/...`, not `/mnt/c/...`.
   Cloning onto the Windows drive works but is slow enough to matter here, because the
   characterization suite does a lot of small-file snapshot IO across the 9p mount.
4. Run `sudo apt install make python3` if your distribution image is minimal, then follow
   the commands below unchanged.

Everything in this repository has been exercised on macOS (arm64) and Linux. The WSL2 path
is the same POSIX environment and is expected to work identically; if it doesn't, open an
issue and say which step broke.

### Run it

```bash
cd evsc-agent-run
make            # target list and the common flows
make up         # bring up MySQL + app
make smoke      # end-to-end sanity check
```

### The suites

```bash
make test-char           # 26 characterization cases, byte-exact baselines (~6 min)
make test-prop           # generated scenarios vs. invariants (~10 min)
make test                # both — the regression gate
make verify              # lint + docs + both suites (pre-merge)

make char-list           # list cases with descriptions
make test-char CASES='14-*'   # run a subset
```

### The two workflows

**Refactor** — you are not changing observable behavior, so:

```bash
make test-char    # green
# ... make the change ...
make test-char    # green, byte-identical baselines
```

Any red is a real finding. Do not re-record.

**Bug fix** — you are changing observable behavior on purpose:

```bash
make test-char                 # green baseline
# ... make the change ...
make test-char                 # RED — snapshot drift, expected
# ... READ EVERY DIFF ...
make char-record CONFIRM=1     # only once every diff is accounted for
make test-char                 # green
```

**Reading the diffs is the entire safety argument.** Every drifted snapshot should be a
change you predicted. If one shows up that you didn't, stop — the suite just told you your
change reaches an edge you didn't know about. `char-record` refuses to run without
`CONFIRM=1` for exactly this reason.

### Worked example: the `strpos` bug

`src/ingest.php:168`. `strpos()` returns the index of a match, or `false` if absent. A match
at position **zero** is falsy in PHP, so a fault note beginning with the search term is
indistinguishable from no match at all — and the outbound tariff call never fires.

The buggy behavior is pinned by
`tests/characterization/cases/14-tariff-not-called-when-note-starts-at-offset-zero.json`.
Change the condition to `!== false`, and case 14 drifts. That drift is the workflow working.

---

## Documentation

Everything below was generated by the workflow above, then reviewed.

| Document | What it is |
|---|---|
| [`AGENTS.md`](evsc-agent-run/AGENTS.md) | Repository guidelines — read into agent context on every session |
| [`docs/domain-overview.md`](evsc-agent-run/docs/domain-overview.md) | The domain at five levels of precision, starting from level zero |
| [`docs/architecture.md`](evsc-agent-run/docs/architecture.md) | System, container, and component diagrams |
| [`docs/sequence-diagrams.md`](evsc-agent-run/docs/sequence-diagrams.md) | Core dataflows at six zoom levels |
| [`docs/data-model.md`](evsc-agent-run/docs/data-model.md) | ERD, per-table reference, `inbox.status` lifecycle |
| [`docs/integration-points.md`](evsc-agent-run/docs/integration-points.md) | **The edge inventory.** Every write, call, and env dependency |
| [`docs/known-issues.md`](evsc-agent-run/docs/known-issues.md) | Fifteen verified defects with reproductions |
| [`docs/stability-spec.md`](evsc-agent-run/docs/stability-spec.md) | **The specification.** What must remain true, with evidence |
| [`tests/README.md`](evsc-agent-run/tests/README.md) | Suite layout and both change workflows |

---

## Slides

| File | Format |
|---|---|
| [`AI_Assisted_Legacy_Refactors.pdf`](AI_Assisted_Legacy_Refactors.pdf) | PDF — read this one |
| [`AI_Assisted_Legacy_Refactors.pptx`](AI_Assisted_Legacy_Refactors.pptx) | PowerPoint — editable on any platform |
| [`AI_Assisted_Legacy_Refactors.key`](AI_Assisted_Legacy_Refactors.key) | Keynote — the original source |

The PDF is the portable copy and the one to read. The `.pptx` export is there if you want to
edit or reuse the slides and don't have Keynote — exported from the `.key`, so expect minor
layout and font drift. The Keynote file is the authoritative original.

---

## Session recordings

Three asciinema casts, gzipped. Raw `.cast` files exceed GitHub's 100 MiB blob limit, so
only the compressed form is committed and `*.cast` is gitignored.

| Cast | What it shows |
|---|---|
| `evsc-agent-run.cast.gz` | The full original session — over ninety minutes, `/init` through a committed suite and specification |
| `refactor.cast.gz` | The refactor workflow: green baseline, extract a pure function, green again with byte-identical baselines |
| `bugfix.cast.gz` | The bug fix workflow: green, the `strpos` fix, **red**, diff review, `char-record CONFIRM=1`, green |

Replay any of them:

```bash
gunzip -c bugfix.cast.gz > /tmp/run.cast
asciinema play /tmp/run.cast
```

`bugfix.cast.gz` is the one to watch if you only watch one. The diff-review step between the
red and the re-record is the entire safety argument, and it is the part that is easiest to
skip when you do this yourself.

---

## References

Feathers, Michael. *Working Effectively with Legacy Code*. Prentice Hall, 2004.
ISBN 978-0131177055. — Both "legacy code is simply code without tests" and the term
*characterization test*.

Ediger, Brad. ["How to Find the Value in Legacy Code."](https://8thlight.com/insights/how-to-find-the-value-in-legacy-code)
8th Light, 22 July 2022.

Wright, Hyrum. ["Hyrum's Law."](https://www.hyrumslaw.com) — Named by Titus Winters for
Wright's observation at Google; also in Winters, Manshreck & Wright, *Software Engineering
at Google*, O'Reilly, 2020, ch. 1.

Hermans, Felienne. *The Programmer's Brain: What Every Programmer Needs to Know About
Cognition*. Manning, 2021. ISBN 978-1617298677. — Short-term memory at two to six chunks,
and the chunking argument.

Fowler, Martin. *Refactoring: Improving the Design of Existing Code*. 2nd ed.,
Addison-Wesley, 2018. [refactoring.com](https://refactoring.com) — Refactoring as change
that does not alter observable behavior.

Chesterton, G.K. *The Thing*. Sheed & Ward, 1929, ch. "The Drift from Domesticity." — The
fence.

ISO/IEC/IEEE 29148:2018, *Systems and software engineering — Life cycle processes —
Requirements engineering.* — Basis for the technical-specification definition.

---

## Tooling used

Warp terminal, Oh My Pi as the agent harness, driven by Claude (Opus). Nothing in the
workflow depends on any of these — every step is a prompt.

---

## Contact

**Brad Boggs** — [brad@contravariant.tech](mailto:brad@contravariant.tech)

Consulting on legacy modernization through **Contravariant LLC**.

Found something wrong in here, or ran the workflow on your own system? Open an issue. I want
to hear how it went.
