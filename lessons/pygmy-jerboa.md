---
title: Finish the setup span immediately after initialization, before any step that
  can refuse
scope: methodology
proposed_surface: skill
source: kickoff
slug: pygmy-jerboa
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The initialization script chained carry-advice before the setup span's finish; the carry refused and the fail-fast script stopped with the stage span open, and the implementation and acceptance spans opened afterwards made the overlap unrepairable. Second occurrence of the generator that parked the first phase's run. Remedy: finish the setup span as the very next command after init, and treat every later step as something that may refuse.