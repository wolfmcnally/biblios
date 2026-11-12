---
title: Open every orchestration stage span with category reconciliation; a wrong category
  passes every check until the finalized trace's timing summary refuses it
scope: methodology
proposed_surface: bin
source: kickoff
slug: purple-zebra
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: 'Indexing-core-publication run A4: timing-summary refused ''orchestration stages
    must be reconciliation children of the run root'' after finalization'
---

Run A4 opened orchestration.acceptance with category intelligence (plus harness, role and model fields) instead of reconciliation. Every later step accepted that span: the primary decision, command zero, two changed-path gates, gate review and the acceptance-level evidence validation all passed. Only timing-summary, which by procedure runs after the root is closed and the trace finalized, checks stage categories, and it refused. A finalized trace is immutable, so A4 could not close; it was preserved unchanged and a successor run on the same candidate repeated the decision, command zero and one gate (about seven minutes). Containment: nothing published, no retry of unchanged input. Correction: successor run A5 with every stage opened as reconciliation. Candidate rule: the telemetry start command or the evidence validator at acceptance level should refuse an orchestration.* span whose category is not reconciliation or whose parent is not the root, so the defect surfaces while it is still repairable; meanwhile, copy the stage-open command from the resource text rather than from a neighbouring role-span command. This is an observed mechanism and candidate lesson, not a policy change.