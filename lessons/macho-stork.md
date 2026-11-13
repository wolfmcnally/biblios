---
title: The command supervisor can report a trivially completed child as incomplete
  (124) when the machine is heavily loaded
scope: methodology
proposed_surface: bin
source: kickoff
slug: macho-stork
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: 'Biblios 0.3.2 handoff gate, 2026-11-12: tests/test_test_governance.py::test_live_reset_validates
    saw _witness_run("/usr/bin/true") return 124 at load average about 14; it passed
    five times alone'
---

In the 0.3.2 handoff gate the witness runner returned 124 for `/usr/bin/true`, failing `test_live_reset_validates`; the test passed five times in isolation, and the suite ran under a load average near 14. `run_command` turns a normal exit into 124 when `os.killpg(pid, 0)` still succeeds after the child exits, and it treats `PermissionError` as the group existing. I cannot distinguish a lingering process-group member from a check that saw another owner's process under a reused id with what I observed; reproducing under synthetic load with the killpg result logged would settle it. Containment: the full gate is rerun on the release tree after this record, with product bytes unchanged. Candidate correction belongs in the Starter template's runner, not this project. This is an observed mechanism and candidate lesson, not a policy change.
