---
title: run-gate --artifact names an artifact the child command must produce; a path
  the command never writes voids the gate row
scope: methodology
proposed_surface: skill
source: kickoff
slug: axiomatic-trogon
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The final gate was dispatched with --artifact pointing at a log the child never creates; the gate ran green (209 passed, exit 0) but the row carried no artifact digest, the tool reported 'required artifact capture is incomplete', and acceptance validation found no successful final gate, so the gate had to be rerun under a superseding manifest. Remedy: pass --artifact only for commands that write a diagnostic file, otherwise capture stdout outside the tool; the acceptance resource should say so beside the run-gate description.