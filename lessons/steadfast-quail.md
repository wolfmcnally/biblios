---
title: The witness receipt prefixes the defect text itself; do not repeat 'Completed
  failure exit 1:'
scope: methodology
proposed_surface: bin
source: kickoff
slug: steadfast-quail
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

test-governance witness finish prepends 'Completed failure exit N: ' to the --defect text when it writes the receipt. A defect written with that prefix already in it is recorded twice ('Completed failure exit 1: Completed failure exit 1: …'). Either the help text should say the prefix is added, or the tool should not add it when present.