---
title: An archive packed on the host records the host's user; extracted as root with
  ownership preserved, git refuses the staged repositories as dubiously owned and
  uv tries to fetch offline
scope: local
proposed_surface: brief
source: kickoff
slug: electric-salamander
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The bundle tar was created on macOS from a Docker-seeded staging directory, so every entry carried uid 501; the proof container extracted it as root with -p, git then refused the uv git cache as a repository owned by another user, uv treated the cached commit as absent and attempted a network fetch, which the offline proof correctly refused. Remedy, applied: extract with --no-same-owner on the instance and in the proof so files belong to the extracting user; the lane command and the proof script share that line.