---
id: unified-indexing-core
title: One incremental indexing core
order: '9'
parent: null
depends_on:
- user-owned-mirrors
informs: []
review_lane: full
evidence_lane: full
---

# One incremental indexing core

The operator directed this outcome on 2026-11-11, relayed by the factotum with transcript evidence:

- (Paraphrase.) The operator set no requirement for duplicate work during the transition and expected a remove-it-at-the-roots replacement rather than a strangler-fig migration, both for the consumer integration and for the curation pipeline.
- "Everything possible about Biblios' indexing should be asynchronous, concurrent, and incremental."
- "Whole-shelf validation should be reserved for a doctor-like diagnosis and recovery function, which we should add later."
- "Again O(N) operations like ‘validate everything’ should be for deep tests and diagnostic/repair tools. Normal business should as much as possible amortize to O(1) or O(n). N = all elements in set, n = directly affected elements in set."
- (Paraphrase.) Indexing is Biblios's job to coordinate; a consumer only wraps it.

The user-owned mirror rulings recorded in [user-owned-mirrors](user-owned-mirrors.md) remain binding: users own mirrors; replacing a file is a new ingestion whose displaced mirror is retained with provenance; Biblios never re-converts an existing mirror, never unlinks bytes it has not verified, and isolates a damaged entry without blocking the shelf. Whether replacement keeps or excises the previous CAS bytes stays deferred.

## Goal

Biblios has exactly one indexing core. Shelf, CLI and bound-consumer entry points are thin calls into it. It brings a set of entries current asynchronously, concurrently and incrementally: discovery touches only changed entries, independent entries convert and embed in bounded overlap, and only admission and commit are serialized. No ordinary operation validates or hashes the whole shelf. A consumer that authenticates published state receives an exact staged publication before it becomes accepted, so a crash can never leave accepted unauthenticated state.

## Evidence that motivates the outcome

On synthetic shelves at the delivered checkpoint: N=20 derivation median 1.6 s per entry with 1,073 Castle per-entry validations and 4 whole-shelf mirror classifications per derive; N=60 did not finish in 300 s. Castle fully revalidates the namespace on every admission, so every Biblios read, fence and per-entry metadata commit costs O(N), and per-entry commits make a batch O(N²). The conversion record currently lives in Castle entry metadata, so each indexing step also advances the namespace revision and invalidates the admitted view.

## Decomposition

One coherent library outcome, delivered through two children under the root ruling relayed by the factotum on 2026-11-11. The plan grew in both review rounds and its second round found most high findings in two separable areas, a demonstrated coherence limit; persistent observation can only mark entries and call the core, so the core is an independently accepted prerequisite.

- [indexing-core-publication](indexing-core-publication.md): the one core and thin entry points, the derived and volatile stores, bring-current, the per-entry stage seam, serving, the import, creation grants, revision guarantees and the renderer.
- [persistent-edit-observation](persistent-edit-observation.md), depending on the first: file-level observers, coverage, gaps and offline recovery, the library-owned loop and its thin hosts, and service-required continuous mode.

This phase keeps the outcome and its own final implementation run: whole-graph integration across both children, documentation and the delivery gates, with no new product mechanism. Review budget (root ruling): each child takes one advisory plan review and one advisory code review, if preflight permits, carrying this phase's two spent plan passes and their dispositions as prior advice; this phase's plan budget stays exhausted, so its integration run takes no further plan review and one advisory code review. Every earlier acceptance case and review finding is mapped to a child or to a named dependency below; none is closed by the split. The bounded 200-source import, adoption of the published Palace query seam and the release (one release) are separate later outcomes.

## Consequential decisions

Rulings relayed by the factotum on 2026-11-11: decisions 1, 3 and 4 proceed through independent review with exact integrity and security proofs; decision 2's first proposal was rejected as conflicting with the whole-shelf rule and is replaced below; the consumer owner's review of decision 4 is active input, and its unresolved authentication, publication and provenance cases are reconciled before implementation. No product implementation begins until review dispositions and the necessary upstream contracts are resolved.

1. **Derived conversion state leaves the namespace.** `mirror_sha256`, `converter`, `converter_version`, `source_cas_id` and `conversion` move from Castle entry metadata to a Biblios-owned per-entry record in the derived store, keyed by entry id and bound to the content address it ran over. The namespace stays the user's authority; user metadata and namespace history are untouched, and indexing never advances the namespace revision. **Existing records (root ruling relayed by the factotum, 2026-11-11):** "use bounded verified preservation import as an implementation decision within the requested migration. Require current mirror digest AND exact Palace rows to match, preserve original user metadata/history/bytes, leave unmatched entries pending; no automatic reembedding, reconversion or data loss." This is the one scoped exception to the greenfield policy in this phase: shelves written by earlier releases keep their conversion keys in entry metadata untouched; Biblios reads them only to import a derived record when the current mirror's digest equals the recorded digest and Palace's committed rows for the entry match it exactly; every other entry stays pending. The import runs once, on the first writer admission of a shelf without a derived store, and is replay-safe.
2. **Per-entry admission needs a Castle capability.** Castle revalidates the whole namespace on every admission, so no Biblios read or fence can be per-entry without an upstream change, and Biblios will not reach into Castle's internals. The requirement sent upstream (C-1 to C-4): admission keeps its constant-cost integrity checks and validates only the records it returns or writes, on every ordinary path including open; full-namespace validation only as an explicit diagnosis read; a cheap committed-revision probe; a targeted entry read at a stated revision; and paginated changed-since discovery over the existing changed-revision column. Biblios fences on revision equality at each serialized commit and re-admits exactly the changed entries. **Sequencing (factotum ruling, 2026-11-11):** the Castle capability and its qualification are an unresolved upstream dependency; review and independent design proceed now; no unsupported Castle API is used and no blocked Castle count is claimed as passed. **Operator direction (2026-11-11, relayed by the factotum with transcript evidence; paraphrase):** asked whether Castle's normal operations should validate only the records they touch, with damage elsewhere detected when accessed or by a full diagnostic rather than blocking unrelated work immediately, the operator agreed without reservation. The requirement therefore states that direction: touched or detected corruption stays fail-closed, with no unproved caching, no history loss and no weakening of security. This is a direction for Castle's own reviewed implementation, not an implemented or released capability; Biblios's adoption transition and the ONE-02 Castle count obligation stay open unchanged.
3. **Conservative change detection over directly affected entries, with library-owned persistent observation.** Discovery visits only directly affected entries: those named by the caller, those an update seam wrote, namespace changes since the last observed revision, read-side observations returned by callers, and file-level filesystem events. While a long-lived process hosts it, a library-owned observer and indexing loop watch the mirror root at file level, independent of bindings, mark exactly the changed entry, and bring marked entries current asynchronously; ordinary open, close and per-call work never sweep. Mirrors are stored flat, so directory-level information is never used for ordinary discovery. Only a genuine gap (no observer for an interval, a restart, an overflow, a dropped-events or must-scan signal, a changed event journal) is recovered explicitly with one recorded catch-up sweep, except that on macOS the persistent FSEvents journal can first prove an offline interval change-free. Direct external edits are detected, not deferred. A candidate's stat identity (device, inode, size, modification and change times) decides whether its bytes are re-read; any difference, a missing record or a racy timestamp forces a rehash; serving always hashes and validates exact bytes. Observation uses only the standard library and the platforms' own notification interfaces; no dependency is added, and no system permission or service installation is authorized or required.
4. **Per-entry stages: authorize, stage, then promote.** Reconciled with the consumer owner's evaluations of 2026-11-11 and restated after the second plan review. Each stage covers one entry, so a single Palace per-file commit is the whole served change and no multi-file transaction is assumed. The caller binds its intent per entry into the request, including each entry's expected accepted predecessor; the core refuses that entry before any work when it differs. The core computes the exact stage in memory (or from a checkpoint the caller sealed) and asks the caller to authorize those exact rows and vectors before persisting anything; authorized rows are persisted as stage rows in the derived store (no separate Palace staging store), the served store and old accepted view stay untouched, the core recomputes the content digest from the persisted stage rows and asks the caller to promote, and only then commits the same vectors to the served store. Rows persisted before authorization never become trusted by recomputation; the bounded verified import is the one pre-authorization exception. A void counts only with the caller's evidence and also resolves a persisted stage; the accepted predecessor never changes on a void; one unresolved stage per entry, so no sibling waits. Reads answer from the accepted stage only and report its digest. Excision, cleanup and epoch change cancel and purge an unresolved stage before their own publication, with no new excision authority. The delivered durable vectors-ready boundary is kept; with a stage authority a durable checkpoint is reused only when the caller sealed it, and an authorization the library never persisted is reported absent so the caller voids it and the library re-prepares.
5. **Renderer and input view.** The library owns one rendering and input-view adapter equivalent byte for byte to the representation a deployed consumer currently produces, parameterized by the caller's format label, with read transforms moving with it. The consumer supplies the fixtures; no silent switch to Drawbridge's representation. The converter stays an injected provider-policy adapter; Drawbridge is the default converter adapter for Shelf and CLI.
6. **Copies need positive creation authority.** Castle records no creation provenance per entry, so a copy's permission to receive its first mirror comes from the committed copy result returned to Biblios's own copy call (or that same recorded result on replay of the same operation), recorded per new entry after the copy commits and consumed by the copy's single first materialization. **Grant binding (root ruling relayed by the factotum, 2026-11-11T20:20Z):** a creation grant may survive a rename, move or tag change before materialization only while its exact entry identity and content-address binding remain valid, with the security authority and the entry's current state revalidated at use; a content change since the grant refuses it. It is crash- and replay-safe and never replaces a file. Absence of a mirror or of a record never authorizes conversion, and a copy made outside Biblios is never granted. Duplicate-entry semantics are unchanged: a copy never inherits an edited mirror, which keeps open question A unchanged.

## Settled boundaries

- **Remove, do not wrap.** `reconcile.py` and `convert.py` orchestration and the duplicate default lane are removed; `Shelf.reconcile`, the CLI and `BoundShelf.derive`/`update_mirror` call the core. Public entry points may keep their names; their implementation is the core.
- **Bound batch bring-current.** One call over a named entry set or the discovered change set, with a revision precondition (`stale-view` otherwise), replay by operation id, per-entry progress observations, cancellation and cooperative yield.
- **Concurrency.** Bounded overlap of conversion, planning and embedding for independent entries within provider limits; one serialized admission/commit authority; retries, replay, provider identity, ZDR and strict fences preserved.
- **Per-entry publication.** An entry becomes searchable when its own stage is accepted; deletion filtering and cleanup of a removed entry never wait for a pending sibling; no zero-pending whole-shelf gate in ordinary paths. Finalization never unlinks user mirrors.
- **No whole-shelf validation in ordinary paths.** Read, search, status, derive and view admission touch only the entries they serve plus O(1) shelf state. Search filters candidate hits by their own per-entry state. Status reads the derived records.
- **Preserved.** User-owned mirrors, retention and provenance, per-entry isolation, writer admission, security and view fences, checkpoint and seal semantics, authorization and audit surfaces (owned by consumers).

## Deliverables

- One indexing core with thin Shelf, CLI and bound-consumer entry points; the duplicate orchestration removed.
- A per-entry derived record replacing namespace-held conversion fields, with stat-identity incremental discovery.
- Bound batch bring-current with revision precondition, replay, progress, cancellation and bounded concurrency.
- Per-entry staged publication with the authenticated stage seam and forward recovery without re-embedding.
- The library-owned renderer and input view with consumer-supplied fixtures.
- Count- and overlap-based proofs, updated documentation, independent code review and both delivery gates.

## Acceptance

| Case | Falsifying observation |
|---|---|
| ONE-01 | Any orchestration other than the core is reachable from Shelf, CLI or bound entry points, or `reconcile.py`/`convert.py` orchestration remains. |
| ONE-02 | At N=20, 60 and 200 synthetic entries, Castle per-entry validations, namespace admissions, mirror hashes or derived-record reads per brought-current entry grow with N (counts, not wall time); the Castle validation count is measured against the adopted Castle capability. |
| ONE-03 | Independent entries' provider calls do not overlap within the configured bound, or two commits overlap. |
| ONE-04 | A deleted entry is not purged from search while a sibling is pending; search is unavailable while a sibling is pending. |
| ONE-05 | User mirror bytes change across bring-current, crash at each stage boundary, or replay. |
| ONE-06 | Any Palace row for an entry exists before its stage's evidence is recorded; a crash at any stage boundary serves unauthenticated state, re-embeds, re-authenticates an authenticated stage, or presents a different stage on the same predecessor; an authenticator error or uncertain outcome changes the served accepted view; a tampered committed row is served. |
| ONE-09 | Discovery misses a change of any stat field, a racy timestamp or a missing record; a read or accepted hit is served from bytes whose digest differs from the record. |
| ONE-07 | Rendering, embedding inputs or read transforms differ from any consumer fixture byte. |
| ONE-08 | Focused checks, proof governance, final changed-path selection and one post-bookkeeping bare full gate fail on their exact candidates. |
| ONE-10 | The verified import embeds, converts, changes Castle metadata bytes, the revision, mirror bytes or inodes; makes an unmatched entry searchable; or is not replay-safe across a crash or cancellation. |
| ONE-11 | With the library's loop hosted, a direct mirror edit (in-place write, atomic-rename save or delete) is not marked and indexed asynchronously as exactly that entry; an ordinary call sweeps while observation is continuous; a genuine gap is not recovered by exactly one recorded sweep (or, on macOS, a journal proof with a positive control); a deployment with no long-lived host is not reported as service-required continuous mode with its exact cost. |
| ONE-12 | A first mirror is materialized from anything other than positive creation authority, more than once, or over an existing file. |
| ONE-13 | Indexing, edit adoption, update-seam writes, copy materialization or the import advance the Castle revision; a curation call advances it other than once on first success. |

Case owners: [indexing-core-publication](indexing-core-publication.md) owns ONE-01 to ONE-10, ONE-12 and ONE-13 in its share; [persistent-edit-observation](persistent-edit-observation.md) owns ONE-11 and the observer's share of ONE-01, ONE-02, ONE-03, ONE-05 and ONE-09; this phase's integration run re-verifies ONE-01 and ONE-08 across both. Named dependency obligations; each stayed open until its upstream capability was adopted and its check passed, and neither was waived:

- **ONE-02, Castle half.** The Castle per-entry validation count is measured only against Castle's adopted per-entry admission capability; until then it stays open, and no release claims O(N) admission is solved. Discharged 2026-11-12 in [indexing-core-publication](indexing-core-publication.md): measured flat at N=20, 60 and 200 against the adopted Castle v0.4.0 touched-record admission.
- **ONE-06, vector-at-rest clause.** Serving a tampered committed vector is detected per hit only through Palace's per-hit evidence seam (corrected design: a status per hit aligned one-to-one, with the hit list, order and scores unchanged; exact body and vector bytes only for a verified hit). This is a final-release requirement carried as a named obligation and never waived; the children state their interim check exactly and claim no per-hit vector check before the seam is published and verified. Discharged 2026-11-12 in [indexing-core-publication](indexing-core-publication.md): search compares each hit's body, input digest and vector bytes from the adopted Palace v0.5.0 per-hit evidence with the entry's accepted row and withholds only that entry on a mismatch.

All fixtures are synthetic temporary shelves. No live shelf, user data or wall-clock SLA.

## Brief refs

- [Library requirements, shelf layout and lagging index](../../briefs/BRIEF.md)
- [User-owned mirrors](user-owned-mirrors.md)

## User Demo

User Demo: N/A for this phase's deterministic proofs. Usability through a consumer, including a direct mirror edit reindexed by a hosted loop, remains open for the user after delivery.

## Exclusions

No upstream edit, and no dependency/pin change beyond the operator-directed adoption of the published Castle v0.4.0 and Palace v0.5.0, no consumer-repository edit, no doctor, no retain-versus-excise decision for replaced CAS bytes, no live data, cloud action, credentials, permissions, history rewrite, tag or release.
