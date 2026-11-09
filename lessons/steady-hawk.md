---
title: Run a planted witness defect once before fixing its expected failure text
scope: methodology
proposed_surface: bin
source: kickoff
slug: steady-hawk
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

Three witnesses were written expecting an assertion message, but the planted defect (reconcile never dropping stale index rows) surfaced earlier as the step's own consistency refusal, IndexLagError, so the expected substring never appeared and the witness tool refused the receipt as NOT WITNESSED. Each needed a second run with the substring the real failure prints. Remedy: the witness tool could print the first E lines of the failed run on an expect mismatch so the author corrects the expectation from evidence, and the governance guidance should say to observe the planted failure before choosing --expect.