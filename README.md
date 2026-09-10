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

### Run it

Requires `docker` (compose v2) and `python3`. No PHP, `jq`, or bash 4 on the host — linting
runs in the container.

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

## Session recording

The original agent session ran over ninety minutes, from `/init` to a committed suite and
specification.

```bash
gunzip -c evsc-agent-run.cast.gz > /tmp/run.cast
asciinema play /tmp/run.cast
```

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

Consulting on legacy modernization through **Contravariant LLC**. Contact details are behind
the third QR code from the talk.

Found something wrong in here, or ran the workflow on your own system? Open an issue. I want
to hear how it went.
