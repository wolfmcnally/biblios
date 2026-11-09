---
title: A report that lives only in a parked run with an orphaned span can be neither
  carried nor re-ingested; preserve it there and name it in the next decision
scope: methodology
proposed_surface: policy
source: kickoff
slug: remarkable-jackrabbit
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

carry-advice refused the parked run because its trace holds an orphaned cancelled span, and ingest-findings refused the same report in the successor because advice must join an accepted dispatch of the ingesting run. The pass-2 report, its dispositions and its consumed launch claim therefore stay in the parked run only; the successor's primary decision names them and the END block says where they are. Either the carry should admit a parked source whose advisory records are themselves consistent, or the policy should say plainly that such a report is preserved, not carried.