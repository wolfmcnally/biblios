---
title: A model safety-classifier stop on one witness batch was handled as a stop-and-wait;
  diagnose it from the stop metadata and present the operator's real routes in the
  same report
scope: methodology
proposed_surface: policy
source: kickoff
slug: perfect-nautilus
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: Pre-release development observation, 2026-11-12; original operational records
    excluded from the public baseline
---

While planning official red witnesses for a cryptographic membership-proof module, one model response was stopped by the provider's output safety classifier (category cyber); the harness said not to produce that content again. The response was correctly held and nothing in it ran, but the follow-up reports only restated the hold and asked for a ruling, and the operator had to ask twice before the stop was diagnosed. The diagnosis was cheap and available the whole time: the transcript's stop record names the category, the explanation and the verification-program route, and the repository's own governance code shows exactly which records the hold blocks. Correction: on any classifier stop, read the stop record's metadata (never the withheld text), state what ran and what did not, read the governing policy for honest routes that need no model-written content (an operator-planted defect, a mechanically generated mutant through the repository's own tool, an explicit operator waiver, account verification), and put the smallest concrete choice and its consequences in the first report. A hold forbids reproducing content; it does not forbid diagnosis or finding legitimate routes.