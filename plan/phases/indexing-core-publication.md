---
id: indexing-core-publication
title: The indexing core and per-entry publication
order: 37/4
parent: unified-indexing-core
depends_on:
- user-owned-mirrors
informs: []
review_lane: full
evidence_lane: full
---

# The indexing core and per-entry publication

First child of [unified-indexing-core](unified-indexing-core.md). The parent's quoted operator direction, its consequential decisions and its settled boundaries bind here unchanged; this file names only the share this child owns. The split follows the root ruling relayed by the factotum on 2026-11-11: two genuine, independently scoped children; one advisory plan review and one advisory code review each, if preflight permits; the parent's two spent plan reviews and every disposition carried in as prior advice, never reset; one release.

## Goal

Biblios has exactly one indexing core, and every Shelf, CLI and bound-consumer entry point is a thin call into it. The core brings a named or discovered set of entries current incrementally and with bounded concurrency, publishes each entry through its own authenticated stage, serves only accepted per-entry state, imports earlier records without re-embedding, and grants a first mirror only from positive creation authority. It exposes the core, stage, request, replay and change-marking interfaces that [persistent-edit-observation](persistent-edit-observation.md) consumes; it does not observe the filesystem itself.

## Decomposition

This child is the independently accepted prerequisite of the parent's outcome: observation can only mark entries and call this child's `bring_current`, so it cannot be accepted before the core, the derived store and its volatile store exist. Everything here changes one publication path and stays one child.

## Owned share of the parent's decisions

- **Decision 1** in full: the derived store, the separate small volatile store for dirty marks, read-side observations and unconsumed grants (digest-bound accepted state stays in the derived store), and the bounded verified zero-embedding import.
- **Decision 2's Biblios half**: reads, fences and re-admission use only published Castle interfaces; revision-equality fences at each serialized commit. The Castle per-entry admission capability is a named dependency, not delivered here.
- **Decision 3's non-observer half**: discovery from caller-named entries, update-seam writes, namespace changes since the last observed revision and read-side observations returned by callers; the stat-identity rule; serving hashes exact bytes. The marking interface the observer calls is defined here.
- **Decision 4** as restated in the parent: per-entry stage slots, stage rows in the derived store, authorize-stage-promote, voids and restart re-presentation.
- **Decision 5** in full: the library renderer and input view against consumer-supplied private fixtures.
- **Decision 6** in full: positive creation grants.
- Serving with per-entry withholding, status, view, checkpoint, seal, readability and excision across both stores, and the revision guarantees.

## Constraints carried into this child's plan

The plan must settle, not assume, each of these:

- **Creation grants (review finding F028).** A pre-commit intent row in the volatile store plus Castle's public committed operation result is a proposal, not accepted. Settle: binding the intent to the committed request by its request fingerprint; trusted provenance of the pre-commit intent, so read-grade tampering cannot forge a creation; supported pre-allocated entry ids; which affected ids of a batch are creations; handling of retired operations; the no-replace crash window. Keep explicit the residual between a legacy never-converted entry and a user-created-then-deleted file the library never observed. No reconstruction from emptiness or absence alone. The shelf's current read of Castle's private operations table must not become a dependency: remove it, or route it to the Castle requirement as a public stored-request read.
- **Vector evidence at rest (review finding F020).** Palace 0.4.1 offers no supported route to a hit's exact body and stored vector inside the search snapshot. The corrected upstream proposal (design only, not accepted or implemented) is a per-hit status aligned one-to-one with the hits, with the hit list, order and scores unchanged; only a verified status carries the exact body and raw vector bytes; other statuses are attributed by store, path and chunk with no bytes; whole-call store or security failures stay whole-call refusals. This child implements per-entry withholding and states its interim per-hit check exactly; it claims no per-hit vector check until that seam is published and independently verified. Stated residuals: chunk omission and full-text-index tampering are not detected per query, and reranking runs before verification.
- **Rulings after this child's plan review (root, relayed by the factotum, 2026-11-11).**
  - *Explicit first ingestion.* A supported, explicit, write-grade route may grant a first mirror for a named entry that was never accepted. It never recreates a mirror for an entry that was converted before and whose mirror is missing, on the basis of absence; that repair stays the explicit update seam, and user bytes are never overwritten. It adds no approval gate and is never invoked automatically.
  - *Grant binding.* A creation grant may survive a rename, move or tag change before materialization only while its exact entry identity and content-address binding remain valid, with the security authority and the entry's current state revalidated at use; replay and races are proved.
  - *Explicit re-embedding.* The design includes an explicit, caller-selected, write-grade ordinary re-embedding of named entries, with cost and side effects explicit. It is never invoked automatically and never signs unverifiable rows. A caller that declines an import keeps the default: the entry stays pending. It reuses the core's once-recorded stage and replay semantics, with no duplicate pipeline and no blind retry. This authorizes the capability, not a live provider call or a migration.
  - *Curation replay.* Every Shelf and CLI curation call stays replayable after its paths change, including operations recorded before this release, through public interfaces only. If that truly needs a public stored-request read from Castle, the plan states the exact minimal Castle requirement instead of regressing replay.
- **Consumer-owner reconciliation (advisory, 2026-11-11).** Derived-store and volatile-store contents authenticate nothing: a served hit's identity is usable only with the exact canonical stage and manifest the caller can verify against its own signed record, and an in-flight marker may only withhold. With a stage authority, the creation identity carried in the stage, checked by the caller, is the only creation authority. One unresolved stage never blocks the shelf's readable checkpoint or seal. Acceptance is reported to the caller only after the served commit is accepted, including after a restart.
- **Cutover (operator direction, 2026-11-11; paraphrase).** A consuming deployment replaces its integration outright when the release lands, with no shadow or parallel transition and no temporary duplicate lifecycle. The core contract and the final published release are coordinated with the consumer owner; no contract is weakened and no unreleased API is claimed.
- **Durability** covers process crash only; power loss is a stated limitation.
- **Castle count.** The ONE-02 Castle validation count stays honestly open until Castle's capability is adopted; no claim that O(N) admission is solved. The operator's direction that Castle's normal operations validate only the records they touch (touched or detected corruption stays fail-closed) is recorded in the parent's decision 2 as a requirement on Castle's own reviewed implementation, not as available capability; this child uses no unreleased Castle API.

## Deliverables

- `core.py` with the one orchestration; thin Shelf, CLI and bound entry points; `reconcile.py`/`convert.py` orchestration and the duplicate default lane removed.
- The derived store and the volatile store with per-entry records, stat-identity discovery and the marking interface.
- Bound batch `bring_current` with revision precondition, replay by operation id, per-entry outcomes, cancellation and bounded concurrency.
- The per-entry stage seam with its signatures, boundaries B0–B5 and restart re-presentation.
- Serving with per-entry withholding; status and views read the derived records.
- The bounded verified import; positive creation grants with the explicit first-ingestion route; the explicit re-embedding capability; replayable curation through public interfaces; the library renderer and input view.
- Count- and overlap-based proofs, documentation, one advisory code review and both delivery gates.

## Acceptance

The parent's cases, in this child's share:

| Case | Falsifying observation |
|---|---|
| ONE-01 | Any orchestration other than the core is reachable from Shelf, CLI or bound entry points, or `reconcile.py`/`convert.py` orchestration remains. |
| ONE-02 | At N=20, 60 and 200 synthetic entries, adapter calls, derived-record rows, mirror hashes, mirror reads, served hits or view admissions per brought-current entry grow with N (counts, not wall time). The Castle validation count is recorded as an open dependency obligation, measured only after the Castle capability is adopted. |
| ONE-03 | Independent entries' provider calls do not overlap within the configured bound, or two commits overlap. |
| ONE-04 | A deleted entry is not purged from search while a sibling is pending; search is unavailable while a sibling is pending. |
| ONE-05 | User mirror bytes change across bring-current, a crash at each stage boundary, or replay. |
| ONE-06 | Any Palace row for an entry exists before its stage's evidence is recorded; a crash at any stage boundary serves unauthenticated state, re-embeds, re-authenticates an authenticated stage, or presents a different stage on the same predecessor; an authenticator error or uncertain outcome changes the served accepted view; a tampered committed row is served where this child's interim check is stated to detect it. The vector-at-rest clause is a named final-release obligation on the Palace evidence seam, never waived. |
| ONE-07 | Rendering, embedding inputs or read transforms differ from any consumer fixture byte. |
| ONE-08 | Focused checks, proof governance, final changed-path selection and one post-bookkeeping bare full gate fail on their exact candidates. |
| ONE-09 | Discovery misses a change of any stat field, a racy timestamp or a missing record; a read or accepted hit is served from bytes whose digest differs from the record. |
| ONE-10 | The import embeds, converts, changes Castle metadata bytes, the revision, mirror bytes or inodes; makes an unmatched entry searchable; or is not replay-safe across a crash or cancellation. |
| ONE-12 | A first mirror is materialized more than once, over an existing file, or from anything other than positive creation authority: absence, emptiness, a creation made outside Biblios, a consumed grant, a content change since the grant, or a grant whose entry-identity and content-address binding or security authority fails revalidation at use; an explicit first ingestion recreates a mirror for an entry converted before; any of this differs under replay or a race. |
| ONE-14 | The readable checkpoint or seal changes under volatile-store writes or is blocked by one unresolved stage; excision leaves a selected entry's data in any table, free page or write-ahead log of either store. |
| ONE-15 | A replayed bring-current or curation call (including after its paths changed) does other than return the recorded result; a different request under the same operation id is not refused; an explicit re-embedding runs without being named, runs twice under replay, or signs unverifiable rows. |
| ONE-16 | Status counters or the view digest differ from the explicit diagnosis's recomputation after mixed operations and a crash at each boundary; acceptance is reported to the caller before the served commit is accepted. |
| ONE-13 | Any indexing lane, update-seam write, copy materialization or the import advances the Castle revision; a curation call advances it other than once on first success. |

All fixtures are synthetic temporary shelves. No live shelf, user data or wall-clock SLA.

## Brief refs

- [Library requirements, shelf layout and lagging index](../../briefs/BRIEF.md)
- [User-owned mirrors](user-owned-mirrors.md)

## User Demo

User Demo: a short CLI protocol on a scratch shelf (add a file, reconcile, search, edit its mirror, reconcile naming it, search again, copy it, reconcile, search) that the user may run after delivery; the deterministic proofs do not depend on it. ONE-07's consumer fixtures are checked by a manual evidence step when the fixtures are in hand. Usability through a consumer remains open after the parent's delivery.

## Exclusions

The parent's exclusions, plus: no filesystem observer, indexing loop or service host (the second child owns them).
