---
title: The kickoff-config cap-hit continuation test's 1.2 s first-event budget fails
  under full parallel load
scope: local
proposed_surface: test
source: kickoff
slug: diamond-snail
status: candidate
filed: '2026-11-19'
occurrences:
- date: '2026-11-19'
  ref: 'palace-cache-exposure, 2026-11-19: changed-path gate attempt 1 failed 2 of
    612 with exit 124 (first-event timeout) while a uv script installed its packages;
    both leaves passed serially twice and in gate attempt 2'
---

Both leaves of tests/test_kickoff_config.py::test_cap_hit_continuation_requires_assessment_and_preserves_budget give the watched fake reviewer a 1.2 second first-event budget, and the child's uv script dependency install alone can exceed it when eighteen workers share the host. The failure is timing, not behaviour: the same leaves pass serially and in a quieter full run. Containment: the failed execution was recorded and reviewed, and the gate was rerun as a new attempt. Candidate correction: give the fake child a budget that does not include package installation, or pre-provision its script environment. Not changed in this phase, which does not own the methodology tests.