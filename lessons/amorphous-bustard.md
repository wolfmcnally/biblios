---
title: Take a red witness's expected text from an observed run of the planted defect,
  not from the assertion message the proof would print
scope: local
proposed_surface: skill
source: kickoff
slug: amorphous-bustard
status: candidate
filed: '2026-11-19'
occurrences:
- date: '2026-11-19'
  ref: 'palace-cache-exposure, 2026-11-19: three witnesses refused (receipts not recorded)
    because the planted defect failed first with TypeError or KeyError, not the expected
    assertion message'
---

Three of sixteen planted defects failed their proof earlier than the assertion the witness expected: a dropped cache made `vector_routes` None, so iterating it raised TypeError; a leaked admission left no eviction, so reading the counter raised KeyError. The witness tool correctly refused each (nothing recorded) and the source was restored. Containment: rerun with the observed failure line as the expectation. Candidate rule: before recording a witness, run the planted defect once and copy the expectation from the actual `E` line. This is an observed mechanism and candidate lesson, not a policy change.