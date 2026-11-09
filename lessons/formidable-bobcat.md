---
title: A Folded-launched reviewer must be registered as a nexus participant before
  it can receive the prompt
scope: methodology
proposed_surface: skill
source: kickoff
slug: formidable-bobcat
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

start_worker creates the Codex session and returns its address, but prepare_message to that address is refused with unknown-destination until register_thread has given the worker a handle in the nexus. The first phase's dispatch did this implicitly; the second phase hit the refusal. Remedy: the kickoff dispatch resource's Folded route should list the steps in order: start_worker, register_thread (handle `<project>-<role>-<n>`), prepare_message, submit_message, delivery_status, then the wait span.