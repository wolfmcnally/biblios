---
slug: secret-hippogriff
title: Decide the 0.6.0 signed commit, push, tag and GitHub release
status: pending
category: decision
urgency: high
blocks:
  - Delivery of phase palace-cache-exposure (gate-proved tree, not committed)
  - Biblios v0.6.0 tag and GitHub release
filed: 2026-11-19
needed_at: "Before any consumer pins Biblios 0.6.0"
source: kickoff
refs:
  - plan/phases/palace-cache-exposure.md
  - AGENTS.md
  - policies/human-in-the-loop.md
---
The palace-cache-exposure work is accepted on its gates but held at an uncommitted, gate-proved tree by your factotum's instruction of 2026-11-19: the general goal of a new release is not a waiver of the rule that tags belong to you, and nothing may be signed as you, pushed as a release or published until you decide. Every commit in this repository is signed with your key 943652EE38441760C3DC35364B6C2FCF894780AE (Wolf McNally), requested per command, so the delivery commit is held with the tag. Decide one of: (a) the agent makes the signed explicit-path commit, fast-forward pushes it, creates the signed `v0.6.0` tag on that commit and publishes the GitHub release "Biblios 0.6.0" with the wheel, the sdist and `SHA256SUMS`, as was done for 0.5.0; (b) the agent makes and pushes the signed commit only, and you tag and publish; (c) you do all of it. The artifacts built from the gate-proved tree are qualified by a clean install; they must be rebuilt from the commit that is tagged.
