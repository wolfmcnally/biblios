---
title: Register a role attempt and read its acceptance before opening the attempt's
  span; attempts are numbered per run, passes per phase
scope: methodology
proposed_surface: skill
source: kickoff
slug: hulking-wasp
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The orchestrator opened the critic's intelligence span for attempt 3 (the phase-wide pass count) before register-role-attempt refused that number as non-contiguous in the fresh run; the span was finished as cancelled, attempt 1 was registered and dispatched correctly, but a finished span is immutable and acceptance validation refused the trace. The advice launch claim was also recorded after the dispatch. Remedy: register the attempt (run-local numbering from 1, with --cause for a later phase-wide pass), record start-advice, and only then open the span and dispatch; the dispatch resource should state that order explicitly.