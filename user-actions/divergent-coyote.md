---
slug: divergent-coyote
title: Close the brief's duplicate-content question with the behaviour the library now has
status: pending
category: decision
urgency: low
blocks:
  - Nothing; the library reports duplicates by raising DuplicateContentError unless allow_duplicate is passed
filed: 2026-11-06
needed_at: "Phase stack-identity-federation"
source: kickoff
refs:
  - briefs/BRIEF.md
  - project/biblios/shelf.py
---
The brief lists "Duplicate content within a shelf" under Unresolved and proposes "report, then let the user choose". The first phase implemented exactly that: `add` raises `DuplicateContentError` naming the live entries that already hold the bytes, and `add(..., allow_duplicate=True)` creates the second entry. Decide whether that closes the item, and if so move it from Unresolved to Rulings in `briefs/BRIEF.md`; the brief is yours to edit.
