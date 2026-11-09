---
title: Finish the setup stage span immediately after evidence-run initialization
scope: methodology
proposed_surface: skill
source: kickoff
slug: fierce-alpaca
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The evidence run's initial orchestration.setup span is opened before kickoff-evidence init and must be closed before the planning span opens; the second phase's run left it open until acceptance, so it enclosed every later stage, command zero refused with 'orchestration stage spans must not overlap', and because closed spans are immutable the only truthful route was a park and a fresh corrective trace carrying the prior run's advice. Remedy: preflight.md Step 2 should state the close explicitly next to the init command, and kickoff-command-zero or capture-plan could refuse earlier (at plan capture) when the setup span is still open, so the defect surfaces before any review budget is spent.