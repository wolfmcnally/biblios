---
title: A removal requirement phrased over every record must start from a column inventory
  of every store, not from the records the reporter named
scope: local
proposed_surface: test
source: kickoff
slug: pragmatic-labradoodle
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: 'Biblios 0.3.1 excision redaction, 2026-11-12: two independent reviews found
    identifier-bearing records the first and second fixes missed'
---

A consuming service reported two records that still named excised identifiers. The first fix redacted exactly those two, although the requirement was 'no selected identifier in any Biblios-managed record'; the first review found three more record types. The second fix scanned every column generically, which the second review showed hid the keyed residue, missed grants and creation intents, read whole files into memory and could alter unrelated rows on a digest coincidence. The keyed redesign started from an explicit inventory of every column of both stores, with a schema proof that the inventory is complete. Candidate rule: for any 'nothing survives anywhere' requirement, first enumerate every storage column and file kind, classify each, and add a proof that the classification covers the schema; then write keyed actions; keep any generic scan as a bounded detector only.