---
title: A red witness must enter through the path the planted defect changes
scope: methodology
proposed_surface: policy
source: kickoff
slug: cautious-dodo
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
- date: '2026-11-09'
  ref: 'Conversion-envelope correction: synthetic stat fixture intercepted lstat before
    the changed byte-length read path; survivor corrected to reach the intended loader
    assertion'
- date: '2026-11-09'
  ref: 'Conversion-envelope correction: one-byte snapshot budget refused empty identity
    before publication; witness corrected above empty-state size and below actual
    vector payload'
---

The threaded proof's lock check first read the tree through a listing call that already acquires the handle lock on its own path-resolution read, so removing the lock from the raw entries read could not make the proof fail, and the witness tool correctly refused the receipt. The check was moved to the reader that only the raw read serves, and the witness then held. Remedy: in test governance's witness guidance, say that the proof's observation must go through the code path the planted defect alters, and that a witness refusal of the form 'still passed' is first a question about the proof's entry point, not about the defect.