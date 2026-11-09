---
title: ingest-findings refuses while the role's dispatch row is still open; read the
  refusal instead of assuming the report landed
scope: methodology
proposed_surface: skill
source: kickoff
slug: righteous-ibis
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The first ingest of the code critique printed 'interrupted role dispatch remains opened' and recorded nothing; the primary decision later refused with 'required independent advisory report is missing' until the dispatch was recorded accepted and the ingest repeated. Remedy: record the accepted dispatch first, then ingest, and treat any ingest message other than ADVICE RECORDED as a refusal.