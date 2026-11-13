---
title: An advisory critic dispatched as a native subagent has no shell, so a brief that
  says "run git diff" leaves it reading only the working tree
scope: methodology
proposed_surface: skill
source: kickoff
slug: wooden-caiman
status: candidate
filed: '2026-11-13'
occurrences:
- date: '2026-11-13'
  ref: 'Biblios 0.4.0 metadata-authority review, 2026-11-13: the code-critic role had
    read-only file tools and could not produce the requested diff, so it could not check
    for weakened assertions against the base'
---

The 0.4.0 advisory code review was dispatched with an instruction to read the change through `git diff HEAD`, but the code-critic role has only read-only file tools. It reviewed the working tree instead and said plainly that it could not compare any test against the base, so the "weakened rather than updated" question was answered only from the current text. Containment: the primary checked its own test edits for weakened assertions and restored one strict check. Candidate rule: when dispatching a critic without a shell, attach the diff (or the changed-file list with base contents) as a file the critic can read, rather than asking it to run git. This is an observed mechanism and candidate lesson, not a policy change.
