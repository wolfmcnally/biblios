---
title: Bootstrap Castle's derived index before creating a namespace on a fresh store
scope: local
proposed_surface: brief
source: kickoff
slug: speedy-oyster
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

castle.journal.ensure_store creates the journal and identity but not cas.sqlite; castle.namespace.create_namespace then refuses with namespace-storage-recovery-required because the derived index is missing, and the wrapped CasError ('derived authority disagreement') is hidden. Castle's own tests ingest a file first, which builds the index. init_shelf calls cas.rebuild_index after ensure_store; the brief's layout section should say so.