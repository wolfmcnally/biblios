---
title: Keep disposable product fixtures under pytest's own temporary root, outside
  any synced folder
scope: methodology
proposed_surface: test
source: kickoff
slug: heavy-nyala
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

tests/conftest.py redirects tmp_path into .kickoff/proof-runtime so qualifying runs persist, and that override reaches a product package named tests whenever both suites are collected in one session (verified with --fixtures-per-test). The checkout lives in a Dropbox-synced folder, and a Castle object written there was found with mode 0644, its ctime 388 ms after its mtime, which Castle refuses; Castle's own source says a sync daemon may rewrite a published object's mode, and the established practice in sibling projects is to mark Castle stores and caches with com.dropbox.ignored and com.apple.fileprovider.ignore#P. A product-scoped tmp_path fixture under tmp_path_factory removed the exposure (3 of 3 full runs green after 5 failures in 10). The template should either scope its override to its own suite or exclude the runtime directory from sync.