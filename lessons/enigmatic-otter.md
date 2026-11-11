---
title: Adopting a donor's tool release mid-run must carry the donor's paired test
  changes for those tools, found before any gate
scope: methodology
proposed_surface: skill
source: kickoff
slug: enigmatic-otter
status: candidate
filed: '2026-11-10'
occurrences:
- date: '2026-11-10'
  ref: 'Responsive-provider-reads successor continuation: changed-path gate attempt
    1 red on a stale registration test after adopting Starter 1.5.0 tools'
---

To make fresh phase admission accept the receiving repository's own tool code, the successor adopted three Starter 1.5.0 tool files (two verbatim, one merged to keep a Biblios-local Folded preflight). The adoption was rehearsed in a disposable copy for mechanics only: init, carry-advice and primary acceptance all passed there, and the test suite was never run on the merged tree. The live changed-path gate, widened to the full suite by the tool change, then failed one test asserting the pre-1.5.0 behaviour that a later registration may sit over a never-dispatched attempt; Starter 1.5.0 had changed that same test in the same release. Containment: the red attempt and its full diagnostics stay recorded; nothing was retried unchanged. Correction: the exact three-line donor hunk was applied with the operator coordinator's go, preserving every Biblios-local test difference, and a distinct second gate attempt passed 321 tests. Candidate rule: when adopting donor tool bytes, diff the donor release's tests that exercise those tools against the recipient's, carry the paired expectation changes in the same delta, and run the recipient's test selection in the rehearsal copy before touching the live tree. This is an observed mechanism and candidate lesson, not a policy change.