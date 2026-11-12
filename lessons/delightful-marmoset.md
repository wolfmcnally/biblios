---
title: A test-wrapper timeout must kill the whole descendant tree; killing the wrapper's
  process group missed pytest in its own group
scope: methodology
proposed_surface: bin
source: kickoff
slug: delightful-marmoset
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: 'Indexing-core-publication, 2026-11-12 (05:06Z): a timeout''s killpg of the
    test wrapper missed pytest'
---

A timeout around the repository test wrapper called killpg on the wrapper's process group, but pytest runs in its own process group, so pytest was not signalled by that kill. In the same session a test's unbounded paging loop had turned a mutant into a hang, which is how a long-running pytest arises in the first place. Candidate rule: any timeout around a repository test wrapper signals and then verifies the whole descendant tree (recursive pgrep -P), not one process group; and tests that traverse pages bound the traversal so a fault fails instead of hanging. This is an observed mechanism and candidate lesson, not a policy change.