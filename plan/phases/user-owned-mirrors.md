---
id: user-owned-mirrors
title: User-owned mirrors with per-entry isolation
order: '8'
parent: null
depends_on:
- adapter-consumer-adoption
- responsive-provider-reads
informs: []
review_lane: full
evidence_lane: full
---

# User-owned mirrors with per-entry isolation

The operator directed this outcome on 2026-11-11, relayed by the factotum with transcript evidence. Their rulings, recorded here because they refine the brief's description of mirrors as derived artifacts:

- A mirror is never immutable. It may change and be re-indexed for many reasons, minor or major, for example corrected speakers or transcript words in an audio description.
- A mirror becoming corrupt must never block the whole shelf, or a node built on it, and must not make the entry's metadata unreadable.
- The library never re-converts a mirror automatically. Users may edit a mirror directly to make it a better representation of the original bytes; the library must then re-index it. Users own the mirror.
- The only corruption is: the mirror is missing, its YAML metadata is unparseable or missing a required field, or it contains invalid Unicode.
- Biblios manages ingestion through Drawbridge and must also allow existing mirrors to be updated and re-indexed as they are updated. Malformed updates are caught at the seam where users perform the update.
- Index lag, mirror change detection, admission and re-index status are Biblios's responsibility. A consumer marshals this library functionality and owns its own security; it does not keep parallel indexing rules.

The brief's "Mirrors are derived" bullet and its `reconcile` wording ("convert … what is … at another digest") are superseded by these rulings for this phase. Amending the brief is follow-up documentation work after this phase, not part of its write set.

## Goal

A converted entry's mirror is user-owned content that Biblios validates, indexes and reports per entry. A valid change is re-indexed from its current bytes without writing the mirror. A malformed or missing mirror isolates only its own entry. An explicit update operation validates before writing. No Biblios operation loses a user's mirror bytes or re-converts an existing mirror implicitly.

## Decomposition

One coherent library outcome: per-entry mirror state, read-only re-index of changed mirrors, one explicit validated update operation, a non-destructive mirror write discipline, and the consequent status/search/read/checkpoint/seal and CLI reconcile behavior. A consumer's later adoption, and its retirement of duplicated lifecycle code, is that consumer's own work after a Biblios release. Three product questions remain open and are carried as bounded exclusions (below); they do not block the outcome.

## Settled boundaries and review inputs

1. **Mirror states.** Each converted, current entry is in exactly one state: `searchable` (file bytes equal the recorded digest, which Palace holds); `pending-reindex` (file present, valid, bytes differ from the recorded digest); or isolated with a class. Corruption classes are exactly the operator's: `mirror-missing`, `mirror-unicode-invalid` (strict UTF-8 decoding fails), `mirror-header-invalid` (the validation contract in item 3 refuses). Distinct, non-corruption classes keep honest names: `mirror-too-large` (bytes or chunk bounds of the governing policy), `mirror-name-section-invalid` (Biblios's own name-section invariant), `mirror-path-unsafe` (the entry's mirror file is a symlink or resolves outside the mirror root; never followed). A symlinked or out-of-domain mirror root remains a whole-binding refusal.
2. **Isolation never blocks the shelf.** The admitted view, strict search fences, derivation, donor capture and status no longer raise for a per-entry mirror state; the state set is bound into the derived view digest so its appearance or clearing is still a view change. Unrelated entries keep read, search and derivation. An isolated or pending entry's metadata stays listable and resolvable and its source download is unchanged. Its stale Palace rows are never returned as hits. It is never a reuse donor until searchable again.
3. **Validation contract.** Biblios validates only what it depends on, once, in one function used both at the update seam and at detection: strict UTF-8; the governing size and chunk bounds; the name-section invariant; and the header. For mirrors read through Drawbridge's provenance envelope (the default lane), the header check is Drawbridge's own `parse_mirror_header`, unchanged. For a consumer-supplied representation, Biblios does not impose Drawbridge's schema: a leading YAML front-matter block, if present, must close and parse as a mapping under a safe loader, and must carry every field the consumer declares in a new optional representation-owned required-header set (default empty). No other field is invented.
4. **Re-index never writes the mirror.** Derivation and CLI reconcile of a converted entry whose conversion is recorded for the live content address read the current file once, validate those exact bytes, index them and record their digest. They never call the converter and never write, rename or delete the mirror. A detected direct edit is recorded as such, honestly unattributed unless the caller supplies attribution; no approval gate.
5. **Explicit update seam.** One new library operation updates an entry's mirror from caller-supplied text with the caller's expected current digest (or expected absence). Malformed text is refused with a typed per-class error before any write. Valid text is staged, embedded and published by one writer-admitted, fenced, checkpointed and replayable operation; the prior admitted state is served until publication. It also repairs a missing mirror. It grants no permission beyond the caller's existing write binding.
6. **Non-destructive writes.** Biblios writes a mirror only for initial conversion of new data, the explicit update, and an already-existing derivation path the plan identifies; reconvert of an existing mirror is not added. Reading, hashing and then renaming is not a compare-and-swap and does not preserve a concurrent edit. The plan must define a supported scheme in which Biblios never unlinks or overwrites bytes it has not itself verified as the expected version (for example an atomic exchange or exclusive create, with displaced bytes retained rather than deleted, and conditional rollback under the same rule), state its platform basis, and name any residual conflict exactly. An explicit update never silently discards a different intervening edit.
7. **Edited-mirror provenance without a schema change.** The `biblios.entry/1` field set is unchanged. Reindex of a direct edit and the explicit update record the converter as a fixed Biblios edit label so later operations can tell an edited mirror from converter output. Source replacement does not grant overwrite permission: an edited mirror over a replaced source is retained and reported, not re-converted (pending the operator's question B below).
8. **Checkpoint and seal.** The dynamic read checkpoint publishes membership excluding pending and isolated entries and lists them. Seal refuses while any entry is pending or isolated; the unsealed shelf remains fully usable.
9. **Reads of pending text.** The plan must decide, with reasoning, whether reading a valid `pending-reindex` entry returns its current validated bytes (with its state reported) or refuses until indexed. Byte/index consistency is a reporting obligation, not a permission gate.
10. Preserve existing writer admission, single coordinator, strict fences, responsiveness, retry/replay, cancellation, provider identity, ZDR/provider fences, authorization and audit surfaces. No Palace, Drawbridge or Castle change.

These boundaries refine the operator's direction; independent plan advice reviews this elaboration and the plan. Ordinary engineering choices remain with the primary.

## Deliverables

- Per-entry mirror state classification, one shared validator, and isolation through view, status, search, read, donor, checkpoint, seal and verify.
- Read-only re-index of valid changed mirrors in derivation and CLI reconcile, with no converter call and no mirror write.
- One explicit validated mirror-update operation with expected-digest preconditions, repair of missing mirrors, and replay.
- A non-destructive mirror write and rollback discipline with stated platform basis and exact residual.
- Finite public-boundary proofs, rewritten tests that encoded "edit = damage", generic API documentation, independent code review, and both source-delivery gates.

## Acceptance

| Case | Falsifying observation |
|---|---|
| MIR-01 | A shelf with one entry in each isolated class and one pending edit: unrelated entries read, search, derive and serve as donors; strict and stack search never return an isolated or pending entry's old rows; each isolated entry reports its exact class; every entry's metadata lists and resolves; source download works. |
| MIR-02 | A valid direct edit, minor and major, is re-indexed by derivation and by CLI reconcile with zero converter calls and zero writes to the mirror file (bytes, inode and mtime unchanged; no partial file); new text becomes searchable, old text no longer is. |
| MIR-03 | The update operation refuses each malformed class with nothing written; a valid update becomes searchable with the prior state served until publication; a wrong expected digest refuses and leaves current bytes; interruption replays without a second provider dispatch. |
| MIR-04 | A concurrent direct edit injected between Biblios's read and publication, on update, initial conversion and rollback, is never lost: its bytes remain at the mirror path or in a retained file the result names. Any residual the plan states is demonstrated or explicitly unproved. |
| MIR-05 | No path re-converts an existing mirror: reconcile, derivation, recovery and source replacement under an edited mirror leave its bytes in place. |
| MIR-06 | Checkpoint excludes pending/isolated entries and lists them; seal refuses while any exist; a restart preserves states. |
| MIR-07 | Focused checks, proof governance, artifact inspection, final changed-path selection and one post-bookkeeping bare full gate pass on their exact candidates. |

Reuse unaffected existing proofs; rewrite in this phase the tests whose premise is that a changed mirror is damage. All fixtures are synthetic temporary shelves. No live shelf, user data, benchmark or load.

## Open operator questions (bounded exclusions)

- **A. Duplicates.** Whether a byte-identical new entry inherits another entry's edited mirror. Existing duplicate and donor semantics are preserved unchanged pending a ruling.
- **B. Source replacement under an edited mirror.** Default in this phase: retain the edit and report it; never overwrite implicitly. Whether a replacement may re-convert awaits a ruling.
- **C. Explicit reconvert as a repair action.** Not added; the explicit update repairs missing or malformed mirrors.

## Brief refs

- [Library requirements, shelf layout and lagging index](../../briefs/BRIEF.md)
- [Completed adoption contract](adapter-consumer-adoption.md)
- [Completed responsiveness correction](responsive-provider-reads.md)

## User Demo

User Demo: N/A for this phase's deterministic proofs. Human judgment of editing usability through a consumer remains open after delivery; source delivery claims no acceptance of it.

## Exclusions

No consumer-repository edit, MCP/CLI feature beyond the reconcile behavior change, dependency or pin change, upstream edit, schema change, reconvert operation, automatic overwrite, live data, cloud action, credentials, permissions, history rewrite, tag or release. Ordinary explicit-path commit and nonforced fast-forward source delivery follow required gates.
