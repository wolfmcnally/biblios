---
slug: primitive-rottweiler
title: Try the bound-shelf example and judge the library interface
status: pending
category: acceptance
urgency: low
blocks:
  - Human usability acceptance only; does not block gate-proved source delivery
filed: 2026-11-09
needed_at: After consumer-adoption delivery, when convenient
source: kickoff
refs:
  - manual/src/consumer_adoption.md
  - project/ADOPTION.md
  - project/examples/consumer_binding.py
---

The six library capability families have executable qualification. The remaining judgment is whether the generic interface and its status/error results are clear enough to adopt. Existing CLI usability and other product decisions remain separate open actions.

User Demo: Review the generic bound-shelf example and its truthful status/error results; consumer-specific parity and security demonstration remain in the consumer. Human usability judgments are separate from executable acceptance and are not claimed by delivery.

Run from the repository root:

```sh
./bin/python project/examples/consumer_binding.py
```

The example creates and removes a disposable local shelf, uses deterministic local embedding and reranking implementations, and makes no external service calls. It starts no daemon and needs no credentials. Expect stored text, `Exact replay: True`, one pending derivation, `Derivation: indexed`, at least one strict search hit, then zero pending derivations.

Read the consumer-adoption guide alongside the example. Report whether ownership, read-only use, retry results, derivation publication, strict search refusals, and coordinated history operations are understandable. This is a usability review, not a request to exercise destructive history operations on a real shelf. Automated checks and bounded desktop/mobile guide inspection do not settle this human judgment.
