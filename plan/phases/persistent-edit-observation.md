---
id: persistent-edit-observation
title: Persistent edit observation and the indexing loop
order: 19/2
parent: unified-indexing-core
depends_on:
- indexing-core-publication
informs: []
review_lane: full
evidence_lane: full
---

# Persistent edit observation and the indexing loop

Second child of [unified-indexing-core](unified-indexing-core.md). The parent's quoted operator direction, its decisions and its settled boundaries bind here unchanged. Root rulings relayed by the factotum on 2026-11-11: automatic detection of direct mirror edits is not deferred; no ordinary full-shelf scan; recovery sweeps only for genuine outages; routine whole-shelf work in a deployment with no long-lived host is not an outage; the service entry point is a thin host of the library's one loop, not an installed daemon or a duplicated lifecycle; scheduling stays in Biblios. One advisory plan review and one advisory code review, with the parent's spent passes and dispositions carried as prior advice.

## Goal

While any long-lived process hosts it, a library-owned file-level observer and asynchronous indexing loop notice each direct mirror edit, mark exactly that entry and bring it current through [indexing-core-publication](indexing-core-publication.md)'s core alone. Ordinary open, close and per-call work never sweep. A genuine gap is recovered explicitly with one recorded sweep, except where the platform's own event journal proves an offline interval change-free.

## Decomposition

This child consumes only the first child's accepted core, change-marking, request and replay interfaces. It adds no publication path.

## Owned share of the parent's decisions

- **Decision 3's observer half.** Mirrors are flat (`mirror_root/<entry_id>.md`), so directory-level information is never used for ordinary discovery. Live FSEvents file events on macOS and recursive inotify on Linux, through the standard library only; no dependency, no system permission, no service installation. Each file event marks exactly that entry in the volatile store.
- **Coverage.** Coverage is a liveness fact the observer renews after each durable batch of marks, with an epoch and a monotonic sequence; another process's `bring_current` consumes marks up to the sequence it read, and continuous coverage means no sweep (review findings F022, F030).
- **Gaps and offline recovery.** No observer for an interval, a restart, an inotify queue overflow, a dropped-events or must-scan signal, a changed event-database identity, a moved root or exhausted watches are gaps closed by one recorded recovery sweep reported with its cause. On macOS the FSEvents journal is replayed first from the persisted position, and a change-free proof is accepted only with a positive control showing the journal would have reported a change (F023). Linux keeps no history, so an offline interval always costs one recorded sweep. Unjournaled volumes are reported `observation-unsupported` on every call, never silently.
- **The loop.** The library-owned loop debounces marks, calls `bring_current` asynchronously within the core's concurrency bound, and records each outcome in its outcome table using the first child's outcome vocabulary (F027).
- **Thin hosts.** `BoundShelf.run_indexing` and `biblios index --follow` only host the one loop in the caller's process. The deployer chooses whether to run one.
- **Service-required continuous mode (F024).** A deployment with no long-lived host does not have continuous detection. Every result reports this mode and the exact hostless cost per platform; it is a stated limitation, never called an outage and never reported as compliance with the no-full-scan rule.

## Deliverables

- `observe.py` with both observers, coverage, gap detection and offline recovery, and the library-owned loop.
- The thin hosts, status reporting of observation mode, position and gaps, and documentation of service-required continuous mode.
- Count-based proofs on both platforms (Linux through the standing Linux lane), one advisory code review and both delivery gates.

## Acceptance

| Case | Falsifying observation |
|---|---|
| ONE-01 | The loop or a host reaches any orchestration other than the first child's `bring_current`. |
| ONE-02 | With the loop hosted, an edit stats, hashes or reads any other mirror, or open, close or a per-call `bring_current` does any per-entry work, at N=20, 60 and 200 (counts, not wall time). |
| ONE-03 | The loop exceeds the core's concurrency bound or overlaps commits. |
| ONE-05 | The observer or loop writes a mirror. |
| ONE-08 | Focused checks, proof governance, final changed-path selection and one post-bookkeeping bare full gate fail on their exact candidates. |
| ONE-09 | An observed in-place write, atomic-rename save or delete does not trigger exactly its entry's stat-identity check. |
| ONE-11 | With `run_indexing` hosted, an in-place write, an atomic-rename save or a delete fails to mark exactly that entry or is not indexed asynchronously, on either platform; an ordinary call sweeps or stats while coverage holds; a macOS offline interval with no edits is not proven change-free with zero stats, or one with edits costs other than exactly one recorded sweep; a forged must-scan or dropped flag, a changed journal identity, a moved root, an overflow or missing positive control is not a recorded gap; hostless mode is not reported with its exact cost. |

All fixtures are synthetic temporary shelves. No live shelf, user data or wall-clock SLA.

## Brief refs

- [Library requirements, shelf layout and lagging index](../../briefs/BRIEF.md)
- [User-owned mirrors](user-owned-mirrors.md)

## User Demo

User Demo: N/A for this child's deterministic proofs. Watching a live edit reindex through a hosted loop remains open for the user after the parent's delivery.

## Exclusions

The parent's exclusions, plus: no change to the first child's publication path, no installed daemon or launch agent, no permission request, no scheduling moved to another system.
