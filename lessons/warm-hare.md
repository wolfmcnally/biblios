---
title: Validate the dashboard handoff against the dashboard tool's limits before the
  accepted close seals it
scope: methodology
proposed_surface: bin
source: kickoff
slug: warm-hare
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The accepted close seals every file in the run directory, dashboard-handoff.json included, yet the dashboard command that consumes that file runs only afterwards and refuses text over 500 characters or containing angle brackets. The second phase's handoff was drafted before close, refused by the dashboard, trimmed in place, rendered, and then the policy-log check in the bare handoff gate refused the mutated sealed input. The sealed bytes were restored from their source copy and the rendered variant kept beside them. Remedy: have compose-handoff or a dashboard --validate-only run as part of close preparation, before the close call, or have close exclude the handoff from the sealed snapshot until it is rendered.