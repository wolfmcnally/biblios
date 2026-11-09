---
title: The governance changed-path selection proof failed once under the full gate
  with changed-path-enumeration-failed inside its own temporary repository
scope: methodology
proposed_surface: test
source: kickoff
slug: economic-mammoth
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

Under the full gate the proof's selection widened with 'changed-path-enumeration-failed:HEAD' inside the temporary git repository it builds, so the bystander family was selected and the assertion failed; alone and on the next full run it passed. One observation; the cause (a git invocation failing under eighteen parallel workers, or a race in the temporary repository) is not isolated. Watch for recurrence before changing the proof.