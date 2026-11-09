---
slug: sociable-locust
title: Decide whether a shelf's Castle store carries sync-exclusion attributes
status: pending
category: decision
urgency: medium
blocks:
  - ../RELEASE.md#public-baseline-and-provenance (static shipping of a shelf that lives in a synced folder)
filed: 2026-11-06
needed_at: "Phase seal-snapshot-readonly"
source: kickoff
refs:
  - briefs/BRIEF.md
  - lessons/heavy-nyala.md
---
Castle refuses any published object whose read-only mode has been rewritten, and its source anticipates a sync daemon doing exactly that; the established practice in your other projects marks Castle stores and disposable caches with `com.dropbox.ignored` and `com.apple.fileprovider.ignore#P` and verifies the read-back. A shelf is meant to be carried and copied, so a user may well create one inside a synced folder. Decide which of these the brief should rule: `init_shelf` marks `castle/` with both attributes (the store never syncs; the shelf travels by copy, tar or snapshot export), or the brief documents that a shelf must not live in a synced folder, or neither. Record the ruling in `briefs/BRIEF.md`.
