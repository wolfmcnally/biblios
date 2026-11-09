---
title: Format only through the repository's check wrapper; an ad-hoc formatter call
  applies the project config to the methodology tests
scope: methodology
proposed_surface: bin
source: kickoff
slug: marigold-mink
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The project's format gate lists its own paths (project package, project tests, lib and named bin scripts) and runs ruff from the project directory with the project's configuration. During implementation a formatter call made by hand with a different working directory resolved `tests` to the repository's methodology tests and rewrote 28 files that belong to the template, under a configuration that is not theirs; the format gate then failed on a fixture file nobody had edited. The files were restored from HEAD by content after their shared mtime proved them the product of one command. Remedy: expose a `--fix` form of the check wrapper that formats exactly the gate's paths, and never call the formatter directly with a bare directory name.