---
title: Test fixtures that commit inherit the operator's personal git signing
scope: methodology
proposed_surface: test
source: kickoff
slug: great-bullfinch
status: candidate
filed: '2026-11-11'
occurrences:
- date: '2026-11-11'
  ref: 'User-owned-mirrors changed-path gate attempt 1: tests/test_check_receipt.py fixture git commit failed with gpg "signing failed: Cannot allocate memory"'
---

Every root methodology fixture that creates a commit (test_check_receipt, test_kickoff_evidence, test_kickoff_tree_id, test_mutation, test_check_catalogs, test_test_governance, test_check, the refactor evaluation fixture) runs under the operator's global git configuration, which here sets commit.gpgsign=true. Fixture commits therefore call the personal gpg-agent, so a gate can fail on the operator's signing service rather than on the code under test; one parallel full-suite run did, and an unchanged rerun passed. The cause of that ENOMEM is unknown. A hermetic correction would disable signing for test processes only, preserving any existing per-process GIT_CONFIG_* entries, with its own proof and review; it must never change user or global signing settings. Deferred by the coordinator as a documented follow-up until a required gate fails on it again.
