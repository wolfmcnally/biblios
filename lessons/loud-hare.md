---
title: A successor run's governing inputs must be separated from the predecessor's
  tool-generated run metadata, and close preconditions audited before any gate is
  spent
scope: methodology
proposed_surface: skill
source: kickoff
slug: loud-hare
status: candidate
filed: '2026-11-10'
occurrences:
- date: '2026-11-10'
  ref: 'Responsive-provider-reads successor run fef81761: accepted close refused ''phase
    close requires an init-time captured canonical phase claim'' after two gate attempts'
- date: '2026-11-12'
  ref: 'Indexing-core-publication run A3, 2026-11-12: accepted close refused on two
    phase-custody rows after the predecessor''s init rows were replayed as explicit
    authorities'
---

The first successor run was initialized by replaying every authority row of the source run as an explicit --authority, including the two rows init itself generates (the phase-custody event and the phase-definition locator). Init then generated the current rows as well, so the run captured two phase-custody records; close requires exactly one and refused before writing anything, after both gate attempts had been spent. The disposable rehearsal stopped before close, so it never exercised that precondition. Containment: the run, its gates and evidence stay untouched; nothing was retried unchanged. Correction: a fresh successor initialized with only the forty governing documents, after which the captured authority showed exactly one current custody row. Candidate rule: when deriving a successor's init inputs from an earlier run, pass only governing source documents and exclude every row the tool generates itself (any located row), verify the captured authority shape immediately after init, and read the close command's preconditions end to end before running the first gate. This is an observed mechanism and candidate lesson, not a policy change.