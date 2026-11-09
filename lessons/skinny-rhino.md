---
title: An operator's Fenric window must outlive every tool timeout that could end
  it, and its lifetime must be verified on both the dialog and the link
scope: methodology
proposed_surface: skill
source: kickoff
slug: skinny-rhino
status: candidate
filed: '2026-11-06'
occurrences:
- date: '2026-11-06'
  ref: Pre-release development observation; original operational records excluded
    from the public baseline
---

A follow-up Fenric question was launched as a harness background command with the harness's 600-second ceiling while the dialog itself carried a 3600-second timeout; the operator reported the question had timed out while the invocation was still nominally alive with no answer, and the dialog had to be reopened. The operator then directed that every Fenric window for them stays usable for at least 60 minutes. Remedy, now a machine-wide rule: pass the maximum dialog timeout, verify the dialog timeout and the remote link's shared clock before sending the link, launch the invocation so that no tool, worker or shell timeout shorter than that can end it (detached, with the answer file written by the Fenric process itself), confirm the process is alive, and report the configured lifetimes and expiry with the link. The ask route in the kickoff skill should say this where it describes operator questions.