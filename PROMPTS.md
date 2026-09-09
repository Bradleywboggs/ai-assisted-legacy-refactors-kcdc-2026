# Prompts for building a reviewable and runnable legacy application specification

**Everything in a code block is meant to be pasted verbatim into your agent harness.

## Provide context for the agent.
```bash
/init
```
(or whatever the bootstrapping command is in your particular harness) 

Tells agent(s) to gather full context of the repository, gathering
- architecture and data flows
- key directories and files
- local development commands and workflow
- code conventions and common patterns
- it may even include gotchas/footguns
records in `AGENTS.md`/`CLAUDE.md`/`GEMINI.md`/`copilot-instructions.md` etc. at the root of the repo.

## Generate additional docs for a human unfamiliar with the codebase.
```
Add additional documents in a `docs` directory for humans who may not be familiar with the codebase,
including but not limited to:
-  domain overview: where does this application fit in the larger data plane? 
Break down at several levels of understanding from most rudimentary to most precise.
-  sequence diagrams: lay out the core dataflows in different zoom levels into 
multiple sequence diagrams
-  entity relationship diagram(s)
-  architecture diagram(s): if sufficiently complex, add additional architecture 
diagrams at different zoom levels

Link to these in root README.md and the AGENTS.md

If new docs generated contradict AGENTS.md, verify against src code and attempt to reconcile.
If not reconcileable, call this out in the docs and in AGENTS.md.
```

This step is about bolstering _your_ knowledge of what this code is about 
so that you have a starting point for assessing the accuracy of the LLM's outputs from here.
You should be equipped to judge the LLM's assertions and/or
ask specific questions to be answered by existing docs, SME's, and even the agent itself.  
This step can be VERY iterative. Keep pushing till you get what you need.

## Document the integration points.
```
Search and find all the places where integration points may occur: 
database writes, API calls, message passes, file writes, environment
or anything else I might not be aware of, and document them in an md file.

If new docs generated contradict AGENTS.md or other new docs generated in this session
 verify against src code and attempt to reconcile.
If not reconcileable, call this out in the docs and in AGENTS.md.
```

**Read this before you let it write a single test.** This is the edge inventory, and everything
downstream is built against it. If it is wrong or incomplete, the suite pins the wrong things,
and you _will_ find that out later — a suite built against the wrong edges passes just as
confidently as one built against the right ones.

## Build the characterization test suite.
```
Add a characterization test suite at tests/characterization/
It should capture the behavior at the edges of the system to ensure all externally observable 
behavior is captured as documented above.

As such, the test code should be language agnostic, 
and NOT dependent on the source code language.

For the database, use a disposable container and replicate 
the schema from the real database if 
you have read access; otherwise infer the structure from usage.  
For API calls, message passes, file-writes, standard mocking will suffice — 
but the payloads must be preserved and checked.

Ensure every characterization criteria assert the outputs at the end of a unit of processing.


Ensure a tests directory README.md is generated which explains
the workflow for implementing refactors (where no behavior is changed)
and for bugfixes (where behavior IS changed).
```

This is the critical step, the point where we have our first iteration of runnable specification
for our legacy application.

The assertion-granularity line is load-bearing: without it you get assertions on intermediate
state, which is mechanism rather than specification, and the suite will then fail on any honest
refactor.

The masking line is load-bearing too: snapshots must normalize every value
that varies across runs or refactors without reflecting a behavior change.
Process ids, datetimes near "now", claim tokens, and source line numbers or
file paths emitted by runtime warnings and stack traces must all be masked
to stable tokens. A baseline that pins "on line 159" will go red when a
comment is added two functions above, and that red is noise — it forces a
re-record for a non-behavioral reason, undermining the rule that re-recording
means a deliberate behavior change. Audit every value the snapshot captures
and ask "would this change if I reordered, extracted, or commented the source
without changing behavior?" If yes, mask it.

## Gather test data.
```
If read access to production-quality data exists, gather a substantial sampling of data
inputs for this test to show that the characterization test holds.

If no such access or substantial data exist, then write a generator which
can derive possible input values based on usage,  resulting in a setup akin to poperty based testing. 
```

Your test is as only as good as its data. Get the most robust sample you can reasonably gather.

## Verify the suite is green against unchanged code.
```
If not already done, run the full characterization suite against the current code with no modifications. 
Every assertion must pass. For any that does not, tell me whether the assertion is wrong or the 
documented behavior is wrong — do not change application code to make a test pass.
```

Until this is green you do not have a baseline, you have a hypothesis. A suite that has never
passed cannot tell you whether a later red is real, and *"those were already failing"* is how a
characterization effort quietly dies. This is the cheapest step here and the one most likely to
get skipped.

## Produce a specification document for review.
```
Based on the code and the characterization tests, 
provide a specification document defining what MUST remain true in the application no matter
what else changes in order to preserve system stability

Break into functional and nonfunctional requirements.

Add a section describing obvious bugs, potential latent-bugs, and questionable behavior which may be depended on.
```

This is now a human-reviewable document you can put in front of SMEs. 
Check it against what you and they know about the business.
Then review the runnable assertions against the document and get them aligned. 
Where the tests and the business disagree, you have probably found a bug
 — and possibly one something else already depends on. 
Note it. Do not let it drive an immediate change.

## Ensure developer-driven reproducibility
```
If not already present, add a script file (e.g Makefile or something like it)
which will wrap all the core development workflow commands, especially
the test runs. Update the root README.md, tests/README.md, and AGENTS.md
```

## Get these artifacts committed, pushed and circulated where feasible.

## Begin making changes.
