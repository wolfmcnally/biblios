---
title: A configuration refusal is the operator's problem, never a document's conversion
  status
scope: local
proposed_surface: brief
source: kickoff
slug: wonderful-bettong
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

Drawbridge's ConfigurationError subclasses DrawbridgeError. A conversion loop that catches DrawbridgeError to mark a document unconverted silently turned an invalid composed configuration into an 'unconverted' entry and hid the real cause. Load the configuration outside the per-document try so a refusal propagates.