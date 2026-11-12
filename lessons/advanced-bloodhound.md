---
title: Run ruff from project/ so the library's own configuration applies; from the
  repository root it reformatted unrelated lines
scope: local
proposed_surface: skill
source: kickoff
slug: advanced-bloodhound
status: candidate
filed: '2026-11-12'
occurrences:
- date: '2026-11-12'
  ref: 'Indexing-core-publication, 2026-11-12: formatter run from the repository root
    rewrote unrelated lines in two files'
---

Formatting two edited files with ruff from the repository root picked up a different configuration than project/pyproject.toml and rewrote lines the change did not touch, which would have put unattributable edits into the candidate. Containment: both files were rebuilt from HEAD plus only the intended blocks and reformatted from project/. Candidate rule: run ruff (check and format) from project/, or through the repository's own check wrapper, never from the root against project files. This is an observed mechanism and candidate lesson, not a policy change.