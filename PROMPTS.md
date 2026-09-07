# Prompts for building a reviewable and runnable legacy application specification

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
```bash
Add additional documents for humans who may not be familiar with the codebase, 
including but not limited to:
-  domain overview: where does this application fit in the larger data plane? Break down at several levels of understanding from most rudimentary to most precise.
-  sequence diagrams: lay out the core dataflows in different zoom levels into multiple sequence diagrams
-  entity relationship diagram(s)
-  architecture diagram(s): if sufficiently complex, add additional architecture diagrams at different zoom levels

Link to these in the AGENTS.md
```

This step is about bolstering _your_ knowledge of what this code is about 
so that you have a starting point for assessing the accuracy of the LLM's outputs from here.
You should be equipped to judge the LLM's assertions and/or
ask specific questions to be answered by existing docs, SME's, and even the agent itself.  
This step can be VERY iterative. Keep pushing till you get what you need.

## Document the integration points and build Characterization Test suite.
```bash
**Search and find all the places where integration points may occur: database writes, API calls, message passes, file writes, or anything else I might not be aware of, and document them in an md file.**

Next We need to add a characterization test suite. 
It should capture the behavior at the edges of the system to ensure all 
externally observable is captured as documented above.

For the database, use a disposable container and replicate the schema from the real database if 
you have read-only access; otherwise infer the structure from usage.  
For API calls and message passes, standard mocking will suffice — 
but the payloads must be preserved and checked.
```

This is the critical step, the point where we have our first iteration of runnable specification for our legacy application.
spec.

## Gather test data.
```bash
Using read-only database credentials provided, gather a substantial sampling of production data
inputs for this test to show that the characterization test holds. 
```

Your test is as only as good as its data. Get the most robust sample you can reasonably gather.


## Produce a specification document for review.
```bash
Based on the code and the characterization tests, 
provide a specifaction document defining what MUST remain true in the application no matter
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

## Get these artifacts committed, pushed and circulated where feasible.

## Begin making changes.