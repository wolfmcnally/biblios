---
title: A zsh orchestration script assigned to `status`, zsh's read-only exit-status
  alias, and died between a paid deploy and its teardown
scope: methodology
proposed_surface: skill
source: kickoff
slug: berserk-guppy
status: candidate
filed: '2026-11-07'
occurrences:
- date: '2026-11-07'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

The example sequence script set `status=none` to hold a Session Manager ping state; zsh refused the assignment to its read-only `status` and the script exited 1 with the example instance and four prerequisite stacks live, after a `zsh -n` syntax check had passed (the error is a runtime one). The operator's own global rule names exactly this trap. Containment: the observations were taken by hand and both teardowns run immediately; cost impact a few minutes of one instance. Prevention candidates: name shell variables for their task (ping_state, exit_code) never status or path; and a sequence that deploys billing resources must either run its teardown from a trap on exit or be split so that a scripting fault between deploy and teardown cannot leave resources up without a visible park.