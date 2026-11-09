---
title: A second terminal block in one run needs a fresh block file; the evidence close
  consumes whatever file it is handed
scope: methodology
proposed_surface: bin
source: kickoff
slug: ochre-buffalo
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The run's first park (operator input) had composed park-block.md; the later authority-drift park composed to the same path, compose-terminal refused to overwrite it, and the close consumed the stale file, appending the earlier park's text as the failure park's log entry. The signature ledger and failure record were correct; the log needed a corrective entry. Remedy: compose every terminal block to a fresh, park-specific file name, and have the close compare the block it is given with the content just composed (title and body digest) before appending.