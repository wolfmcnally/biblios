---
title: A coordination report's time label was typed instead of read; run date -u immediately
  before preparing each message
scope: methodology
proposed_surface: skill
source: kickoff
slug: steady-octopus
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: 'Indexing-core-publication coordination, 2026-11-12: a typed time label caught
    before submit'
---

A status report to the coordinator carried a time label written from memory rather than from the clock; it was caught before the prepared envelope was submitted and the message was re-prepared with a measured label. Coordinator reports are timed evidence, and a typed label can silently disagree with the envelope's own sent_at. Candidate rule: every report label comes from date -u run in the same step that prepares the message, never from an earlier reading or an estimate. This is an observed mechanism and candidate lesson, not a policy change.