---
title: Refresh a plan's Definitions Read line numbers before recapturing it after
  implementation
scope: methodology
proposed_surface: bin
source: kickoff
slug: wild-quail
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

A plan captured before implementation cites definition lines that the implementation moves. Recapturing the plan afterwards (to record adopted advice or a dropped test) re-runs the concreteness check against the implemented tree, which refuses every drifted `modify` row and every identifier the implementation removed. The rows were refreshed programmatically from the current tree and removed identifiers were reworded as prose. Remedy: give check-plan-concreteness a post-implementation mode that tolerates line drift for rows marked modify, or have capture-plan refresh those rows itself; and say in planning.md that a recapture after implementation needs the refresh.