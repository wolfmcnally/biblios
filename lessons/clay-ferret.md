---
title: Diagnose each failure in a red gate separately before naming a cause
scope: methodology
proposed_surface: skill
source: kickoff
slug: clay-ferret
status: candidate
filed: '2026-11-11'
occurrences:
- date: '2026-11-11'
  ref: 'User-owned-mirrors gate attempt 1: two first-event timeouts were first reported as gpg failures because one setup error in the same run was'
---

A red aggregate gate with several failures was first reported as one cause (gpg signing) because the first diagnostic read named it; the two other failures were first-event timeouts against a test's own one-second budget, a different mechanism. The coordinator caught it. Before reporting, read each failure's own assertion and stderr, group by mechanism, and say which causes one observation cannot distinguish. A message's time label must be the observation time, not a forecast of when it will be read.
