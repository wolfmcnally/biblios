---
title: Shipped examples are a public interface with no executable check; an API change
  broke all four and only release qualification noticed
scope: local
proposed_surface: test
source: kickoff
slug: rugged-puma
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: 'Biblios 0.3.0 release qualification, 2026-11-12: consumer_binding, shelf_demo,
    seal_demo and stack_demo all failed after the indexing-core change; 548 tests
    and the full gate were green'
---

The indexing-core change replaced the reconcile result type, and every example packaged in the source distribution and named in the README and manual still read the old fields (state, built, converted, indexed, conversion_current). The full suite, both gates, the accepted close and the bare handoff gate all passed, because no test runs an example. Release qualification ran the examples against the installed wheel and found them broken; running stack_demo also exposed a real library race (a status read between the first writer creating the accepted store's file and committing its schema raised a raw SQLite error), now fixed with a witnessed proof. Candidate rule: the shipped examples get a hermetic smoke proof (scripted input, temporary directory, stand-in embedder) in the ordinary suite, so an interface change that breaks them fails the implementation gate rather than the release. This is an observed mechanism and candidate lesson, not a policy change.