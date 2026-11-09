---
title: uv refuses --managed-python together with UV_PYTHON_PREFERENCE; set the preference
  one way
scope: local
proposed_surface: brief
source: kickoff
slug: belligerent-cicada
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The proof exported UV_PYTHON_PREFERENCE=only-managed and also passed --managed-python to uv sync; uv 0.9.26 refuses the combination ('cannot be used with --python-preference'). The project's pyproject already pins only-managed, so the environment variable was dropped and the flag kept, in both the proof script and the instance command.