---
title: A Folded-dispatched role has no watcher; expose the watcher's prelaunch cancellation
  as an evidence command
scope: methodology
proposed_surface: bin
source: kickoff
slug: poetic-okapi
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

When a review role is dispatched through Folded, the orchestrator plays the watcher: it starts the advice claim, opens the dispatch, launches the worker and records the result. The watcher cancels an advice claim when no child was created (advisory.cancel_unstarted), but that function is library-only and its docstring says only the watcher calls it, so a prelaunch rejection on the Folded path had to be recorded by calling the pinned module directly from Python. Remedy: give kickoff-evidence a 'cancel-advice --registration' command with the same precondition (an active claim, no opened dispatch result, no launch), so the Folded path has a first-class, logged way to do what the watcher does.