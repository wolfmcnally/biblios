---
title: A phase dashboard renders the phase's whole trace bundle, so a later accepted
  run on a new UTC date silently stales every earlier archived date of the same phase;
  the close step renders only the current date and the handoff gate then refuses
scope: methodology
proposed_surface: skill
source: kickoff
slug: aggressive-guan
status: candidate
filed: '2026-11-08'
occurrences:
- date: '2026-11-08'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The AWS qualification phase parked on 2026-11-07 with an archived dashboard for that date, then closed on 2026-11-08 with a new accepted trace. The close resource's dashboard step renders one page for the current date from the current accepted trace. The renderer computes each page from the phase's entire trace bundle and rewrites the archive index to point the phase at the page just rendered, while the checker regenerates every archived page in sorted date order and compares bytes. So the 2026-11-07 page, committed with a seven-trace bundle, became stale the moment the eighth trace was finalized, and nothing in the close procedure said to re-render it; the full gate found it after 217 tests had passed. A second trap sits inside the repair: re-rendering only the stale earlier date moved the index to that date and staled index-data.js instead, because the index follows the last render. Containment: the gate failure was preserved, nothing retried, the window released. Correction: re-render each archived date of the phase from its own archived handoff and accepted trace, oldest first and the current date last, then run bin/check-execution-dashboards directly before any gate. Candidate rule for the close resource: when a phase has archived dashboards on earlier dates, the dashboard step renders every one of them in chronological order, ending with the current date; or the renderer itself re-renders a phase's whole archive so the index and every page share one bundle.