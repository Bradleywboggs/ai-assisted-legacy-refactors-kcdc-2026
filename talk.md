**Talk v2 — working script**

At a company where I've done a lot of work over the last couple of years, at the epicenter of nearly every on-call incident, there is one application.

The incidents go like this:

- PagerDuty goes off due to slow data ingestion
- Message count in the ingestion queue is in the tens of thousands
- The ingestion app's logs indicate one of the following:
  - Lock-wait timeout due to DB row contention
  - Runtime error due to a violation of some implicit data contract
  - Large gaps between log timestamps due to slow queries

I remember one such incident well, as I later realized that work that I was doing was to blame: I was in the middle of a data replication task in preparation for some database maintenance, which would save us lots of money over several months. The replication was trigger-based. These triggers ran on every insert on key ingestion rows. We reached a tipping point in the replicated table size, and the queries got expensive enough to slow the ingestion app to a near grinding halt.

We didn't figure that out at the time. Our resident ingestion wizard ended up reading the runes of the ingestion app's code, found a slow SQL query, and he uttered this mysterious incantation: "WHERE call\_reason = 12". The magic number saved the day. Ingestion sped up, the message queue plummeted toward zero, and we resumed our normal lives. (Now obviously there's nothing magic about a proper use of a database index, which is what this was. It's just there was nothing meaningfully obvious about this change: it required specific knowledge of our data access patterns across the platform.) In any case this resolved the one issue, but there were more to come.

So if this app causes so much churn, why haven't we fixed it? Why hasn't it been rewritten?

Well, a rewrite was attempted. And aborted.

I wasn't part of the effort, but I've talked to two people who were.

The engineer who did the rewrite emphasized one blocker: an implementation detail in a third-party library caused a significant performance regression.

His team lead at the time — now the engineering manager — pointed at the same problem from a different angle, and then offered a second one, much less tractable:

_"We weren't 100% confident in how we were going to test and approve the new version."_

The first blocker was technical, specific, and manageable. A candidate fix was identified later — and left unimplemented.

It was left unimplemented because of the second blocker, and that one is why this app, and probably several you have worked in, sit with a long list of improvements, upgrades and fixes in a state of indefinite deferral. Legacy apps are change resistant.

Legacy apps resist change because of a lack of understanding, a lack of tests to protect us in the absence of understanding, and the potential business cost of getting it wrong.

Michael Feathers famously defined legacy code this way: _"to me, legacy code is simply code without tests."_ He goes on in his preface to ask: without tests, how can we know whether we are making the code better or worse? Implied there is that understanding is the bottleneck.

John Ousterhout, fourteen years later, on software design in general, describes the same bottleneck.

_"…the greatest limitation in writing software is our ability to understand the systems we are creating." — John Ousterhout, 2018_

Recast for legacy work: the greatest limitation in fixing, extending or rewriting legacy software is that we don't understand the system we are maintaining.

And none of this would matter except for one other implicit fact: legacy applications are valuable to the business. Brad Ediger, a consultant who specializes in legacy systems, in a 2022 post:

_"Legacy code is code that delivers more in value than it costs to maintain." — Brad Ediger, 2022_

Legacy applications resist change because of the cost of the change.

Changes made without full understanding, and without guardrails to protect us from that lack of understanding, lead to system instability. System instability wakes us up at 2am, disrupts product roadmaps, and can surface all the way to the customer — which puts revenue at risk.

And in the legacy canon, and in most of our personal experience, getting to the point where a change carries a reasonably low risk profile is expensive, too. It takes significant investment: writing tests, building confidence over time.

By understanding, what are we talking about?

If I only mean some sense of coherence we carry around in our heads, that's not sufficient, and temporary. Ultimately what I'm driving toward is an _artifact_ which documents and protects our ability to **verify and approve that a change is correct.**

**_[SLIDE — back to the EM's quote.]_**

A specification.

Webster defines _specification_ as:

_a detailed description of work to be done or materials to be used in a project: an instruction that says exactly how to do or make something_

And a technical specification translates approved needs into an implementable and verifiable description of a system. It defines how the system will behave or be built — interfaces, data, constraints, engineering decisions. _(Adapted from ISO/IEC/IEEE 29148:2018.)_

In a greenfield app we often have one. A PRD, with explicit _shoulds_ and _musts_, functional and non-functional requirements, or at least acceptance criteria. We are handed a definition of correct, and we build to a declarative intent.

In a legacy app, if an external spec exists, it has been stale for years. So the specification is buried in the behavior of the app. Getting to it is an act of archaeology — digging through years of feature additions, edge-case handling and hotfixes to find out which behaviors other parts of the system depend on.

**The definition of what should be true collapses to a subset of what is.**

Now — which subset? There is a law for this.

**_[SLIDE — Hyrum's Law, exact wording.]_**

_With a sufficient number of users of an API, it does not matter what you promise in the contract: all observable behaviors of your system will be depended on by somebody._

Hyrum's law specifically refers to API users. More generally in a legacy app, who are our "users?" ANY other user or system that _depends on this application's outputs._ Usage quietly promotes your implementation into your interface. Nobody signs off on that; it accrues. And you will never have the list of who is standing on what — so under uncertainty, if another system can see it, assume something is standing on it.

Any external system watching that database sees every write. Many of which may be incidental. But anything _downstream_ — waiting on the rows this app produces, the calls it makes — sees only a handful of things, and those are the ones that matter. If something outside your process can see it, it is specification.

The exception is the behavior nobody is standing on because it is the thing they're complaining about. Nobody depends on this app deadlocking. They depend on it finishing.

So, in a legacy system, the specification is:

**What must remain true at the edges, no matter what else changes.**

This is the understanding that is expensive. It's expensive to get it wrong, and historically, it's been expensive to get right.

**The day after an incident**

The day after one of our app's incidents I decided I was fed up, and that I would try to get to a useful understanding — enough to see whether we could get a real fix planned and implemented. I spent a solid two hours reading every line of a 1,489-line method, trying to trace the data path.

**_[SLIDE — all nine measurements. Say three.]_**

Fifteen hundred lines in one method. One transaction covering ninety-two percent of it, with a synchronous HTTP call inside it. Twenty-three writes, four reads — and the median distance from writing a value to reading it back is four hundred and sixty-four lines.

About forty-eight local variables live at once at the deepest point. Felienne Hermans, in _The Programmer's Brain_, puts human short-term memory at two to six chunks. Not forty-eight. This was solidly outside the bounds of the human context window.

I got to the end and realized I didn't have the brain power to hold it. Any bravado I had going in — _I_ can fix this — was gone. With all those database writes, in a system whose fundamental point of integration _is_ the database, any app connecting to that database is potentially in the blast radius of a change. The risk is too high, and the cost of mitigating it — uncovering the hidden specification — was also very high.

**Enter AI assistance**

It was at this moment of mild despair about my embarrassingly small context window that I turned to AI.

Now, I've been hesitant about AI-augmented development. I like determinism — pure functions, expressive type systems, formal methods tooling from a distance. I think those things lead to better software. LLMs are inherently probabilistic. Auto-complete on steroids. So how do you get high-quality, maintainable code out of one?

Since then I've had that question answered with pretty profound results. But at the time, I realized it was the wrong question for what I needed.

A probabilistic tool is a bad way to produce something you have to trust. It is a very good way to produce **a candidate you're going to check.** And I wasn't asking it to write production code. I was asking it to read code.

Using it this way is neither revolutionary nor new, but in that moment it was a game-changer for me. I spent the next hour — and, as bugfixes, requirements and time allowed, many hours since — prompting my way toward a verifiable understanding of this app's specification: its ins and outs, its dependencies and its dependents. Producing artifacts that let me check the understanding. Delivering a few bugfixes and improvements along the way.

**And planning for the remodel is greenlit, with a clear path to testing and approving what replaces it.**

For the time remaining I'm going to show you my current workflow for approaching changes to a legacy application, using an example app that carefully reproduces the pathologies of the one I've described but recasts the domain to protect IP.

**DEMO**

**The approach**

**1. Capture the existing behavior at the edges.** Feathers calls these characterization tests. They pin current behavior as-is — they make no claim that it's correct, and that is the point. On their own they pin _everything_ equally, which is why the edges matter: the edges tell you which of it counts. That is the baseline.

What changed since 2004 is that we can prompt our way there.

I used a very similar method against the real app to test changes needed for a database schema migration that was already in the pipeline.

**The concrete workflow**

Create a new branch.

**1. `/init`** — give the agent full context of the app, as AGENTS.md, CLAUDE.md, or whatever your harness reads.

**2. Review and document, for a human unfamiliar with the codebase:**

- the core data flows, with diagrams
- important invariants in the system
- unexpected but potentially depended-on behaviors

**Read these.** This is the first checkpoint and it is not optional.

**3. Prompt for the characterization suite:**

_We need to add a characterization test suite. It should capture the behavior at the edges of the system to ensure observable behavior is preserved. Search and find all the places where integration points occur — database writes, API calls, message passes, file writes — and document them. Ensure every characterization assertion preserves the outputs at the end of a unit of processing. For the database, use a disposable container and replicate the schema from the real database if you have read-only access; otherwise infer the structure from usage. For API calls and message passes, standard mocking will suffice — but the payloads must be preserved and checked._

**4. Feed it real inputs.** A test is only as good as the data you give it. If you have a record of input types and observed values, use it heavily.

This app is triggered by a queue, but the input is all stored in the database, and I have read-only credentials. So I had the agent sample a wide breadth of real message shapes from production and bake those in as fixtures.

**If you don't have that record, getting one is your first step, and what it takes depends entirely on what your app ingests.** Happy to go into options in Q&A.

**5. Produce the specification document.** Have the agent write an initial formal spec from the suite, in whatever format your org uses — give it an example and let it go. That is now a human-reviewable document you can put in front of SMEs. Check it against what you and they know about the business.

Then review the runnable assertions against the document and get them aligned. **Where the tests and the business disagree, you have probably found a bug — and probably one something else already depends on.** Note it. Do not let it drive an immediate change.

Commit the documents and the tests. Merge them.

**6. Now decide on the change.** You may have a backlog of real tickets, or just a list in your head.

**The changes**

I'll start with something easy: refactor the input parsing into a dedicated function that takes the input and validates it.

Then a second refactor — a consolidation of some duplicated SQL that looks every bit as safe as the first one.

Then a bugfix.

**_[NOTE — the second refactor is UNDECIDED and this wording is deliberately neutral.]_** _The candidate is consolidating the three byte-identical revision-insert literals at_ _ingest.php:149/263/287_, _which a fourth site at_ _:301_ _re-executes without declaring — so it crosses a hidden order-dependency._ **_Whether the checks go green or red is unknown; the probe has not been run._** _demo-runs/PROBE-pin-and-change.md_ _has the full assessment._

**Do not commit to the trap on a slide until it has run.** The earlier draft said _"a second refactor that looks just as safe and isn't,"_ which promises a red before you know there is one. The line above works either way: **green** proves the suite tolerates real structural change, which is the surprising result; **red** proves the obvious cleanup wasn't, which is the better beat. Say which one happened, not which one you expected.

**Fallback if the probe is inconclusive:** drop to one refactor plus the bugfix. The arc still demonstrates sensitivity and the method survives intact.

For the demo I'm going to let the agent execute the changes — but the agent's real value here was in step 3, not step 6.

I'll acknowledge that this codebase is small and these changes are small. But I've used this at greater scale, across much larger cross-cutting codebases, targeting specific changes, with promising results.

**This method — find the edges, generate characterization tests at the edges, then make the change — holds at multiple resolutions: module level, whole app, and even across interconnected systems.**

Is AI the silver bullet for our legacy systems? No. But it took the most expensive step in that canon — recovering enough understanding to know what "correct" means — and made it affordable.

**_[CONCLUSION — not written.]_**
