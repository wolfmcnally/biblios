---
title: Run the focused format and lint checks before capturing the candidate and opening
  a role dispatch
scope: methodology
proposed_surface: skill
source: kickoff
slug: glaring-clam
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The implementation candidate was captured and the critic's dispatch opened before the focused lint check ran; the check then failed on an import order, the fix moved the candidate, and the opened dispatch row could not be rebound. Under policies/fail-closed-resume.md the attempt was closed as a prelaunch rejection with its advice claim cancelled and a second attempt registered, which cost about six minutes of bookkeeping and an intelligence span recorded in error. Remedy: the implementation resource's pre-dispatch checklist should name the focused format and lint checks explicitly before capture-change and record-role-dispatch, and the capture could refuse when the repository's format check is red.