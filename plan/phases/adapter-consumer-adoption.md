---
id: adapter-consumer-adoption
title: Adapter-reached multi-shelf consumer adoption
order: '7'
parent: null
depends_on: []
informs: []
review_lane: full
evidence_lane: full
---

# Adapter-reached multi-shelf consumer adoption

The operator authorized the six-family library adoption effort targeting 0.2.0 on 2026-11-09. This elaborates the existing sketch; adoption, security and application parity remain the consumer's responsibility. The package version is assigned during implementation, not asserted as already shipped here.

## Goal

A consumer can operate one explicitly bound shelf per storage domain through bounded storage, conversion and provider interfaces, preserve its existing atomic curation and retained-content workflow, reuse compatible same-shelf duplicate bodies, and search supplied shelves strictly with one Palace union rerank. All six families below form one adoption outcome; an opener alone does not complete it. Biblios composes Castle, Palace and Drawbridge and adds no security boundary.

## Decomposition

One coherent library phase. Binding and effect semantics are shared across all six families, so ordinary module and proof sequencing stays inside the phase. A demonstrated missing substrate seam is coordinated with its owner; unblocked work continues, but omitted capabilities never count as completed adoption. No arbitrary mixed-operation batch or new transaction engine is required: individual Castle mutations, creation-only folder import and subtree duplication supply the actual atomic operations.

## Settled decisions carried into elaboration

These pins implement the operator-authorized generic adoption contract. They replace the sketch's conditional opener-only scope and are included in independent plan advice. They introduce no new security, deployment, publication or consumer policy decision.

1. Library only, targeting experimental 0.2.0; no CLI/MCP feature, tag, publication or history transition. The existing greenfield policy is unchanged: replace superseded internal shapes directly, update their callers and preserve user data and valid history.
2. Explicit binding, access mode, fixed roots, active Castle identity/epoch and handle ownership. Borrowed handles stay usable after exit/refusal; owned handles close on every path. One existing coordinator, no hidden second writer. Dynamic read-only opening is distinct from sealing.
3. The consumer controls mount and ancestor custody for the binding lifetime and excludes untrusted path replacement. Biblios rejects escaping symlinks/inconsistent roots and keeps library-controlled scratch and content-bearing artifacts inside declared roots; it does not sandbox arbitrary Python callbacks or claim protection against replacement of trusted ancestors.
4. Individual atomic curation, creation-only frozen folder import including empty directories, and atomic subtree copy; caller expected revision and reserved IDs are exposed. An original operation ID binds the complete normalized request and active storage epoch; exact replay precedes stale-new-request admission, changed retries refuse before effects, and generated values/results stay stable across restart. No second transaction log or arbitrary namespace executor.
5. The admitted lane uses only supplied converters, mirror policy, document/query embedders and rerankers. Missing/failed injections never cause credential discovery, service startup or fallback. Document unconverted outcomes remain distinct from configuration, provider, cancellation and recovery failures.
6. Reuse only explicitly nominated live compatible same-shelf donors admitted by the consumer and checked against actual committed state. Every destination independently embeds its own filename section, including an identical name, owns its rows, and survives donor deletion. Missing/ineligible donors take normal ingestion; integrity/binding failures refuse. Representation compatibility includes actual model inputs, not only source digest/dimension.
7. Strict single/stack search uses the underlying strict provider/rerank operation; completion validates the admitted binding/view and cancellation/deadline. Cooperative cancellation promises no accepted late answer, not forced termination of arbitrary synchronous code. Only supplied shelves participate; Palace alone fuses/reranks.
8. Retained reads, snapshots, excision preview/execute/status/recovery and successor rebinding are finite coordinator operations over Castle's existing protocol. Consumer history/selection/authorization/publication remain external. No user-facing excision, snapshot restore, physical-erasure or external-copy guarantee.
9. Typed failures carry safe stage/effect classifications: refused before effects, authority committed with derived work pending/failed, or recovery required/uncertain. Diagnostic text excludes source/name/query/path/provider exception contents. Durable observations describe their actual transaction/checkpoint; callbacks and unsigned watermarks cannot authenticate a consumer publication or promise exactly-once external execution.

## Capability contract

### BIB-01 — Binding, lifetime and active storage

Provide explicit existing-shelf open and separate initialization through an admitted NamespaceHandle or a provider-owned session. Validate shelf/namespace/layout/active Castle identity before content access; allow a declared current successor root within the bounded domain. Refuse missing, stale, retired or incomplete storage without opening a predecessor, initializing replacement authority or repairing it. A dynamic read-only reader creates no locks, WAL/shared-memory files or other sidecars, converts nothing and refuses recovery-needed state. Requested writes refuse before effects. Close all reader/index connections before unmount and expose whether work remains active. All sniff/conversion scratch, mirrors, indexes, staging and content-bearing sidecars use explicit roots, without ambient temporary/cache configuration.

### BIB-02 — Revision-bound curation and folder admission

Expose typed individual file/directory creation, replacement, relocation, logical deletion and snapshot requests plus a typed creation-only frozen inventory import and existing atomic subtree copy. Serialize concurrent mutations through the one coordinator. Bind expected revision, operation kind, source/destination/parent/name, immutable content addresses/sizes, metadata, supplied/generated IDs and relevant option presence to the original operation ID within the binding. Refuse stale/changed requests before ingestion, staging, conversion or model calls. Exact restart replay returns the original durable result and IDs. Distinguish payload bounds from Castle's canonical metadata-request bound. Cover 100 files, 100 folders, 64 MiB total payload, 4 MiB per file and 255 UTF-8 bytes per name, subject to namespace validity and capacity; over-limit work refuses without effects. Interruption exposes complete before/after authority or explicit supported recovery, never a partial accepted folder. No arbitrary mixed create/replace/move/delete transaction is in scope.

### BIB-03 — Contained conversion and admitted providers

Supply a finite typed conversion/mirror interface receiving exact source bytes, declared media type, display name and bounded scratch; return converted/unconverted provenance and mirror digest. Support bounded escaped rendering and an independent filename section without forcing default rendering. Preserve original downloads and current source identity; no silent truncation or unbounded source-derived heading/frontmatter. Keep existing provider injection, progress and cooperative yield. Expose/admit convention, provider/model/dimension/implementation, chunking and name policy; do not silently change model input or padding. Completed durable work replays without repeating effects; interruption after an external dispatch but before durable result is explicitly uncertain and requires consumer-controlled retry. No new journal or exactly-once remote billing claim.

### BIB-04 — Compatible duplicate body reuse

Reuse an eligible nominated donor's converted body and committed body vectors without another body conversion/embedding call. Bind live content, mirror/body input digests, committed index view, consumer admission evidence and complete embedding/chunker/name policy. Plan destination-owned rows with Palace's public FilePlan/plan_one/commit_plan route and supplied vectors under the actual writer-lock/lifecycle contract. Independently embed each destination name, including equal names. Revalidate changed donors; no cross-shelf cache or private vector-table patching. A narrow supported donor acquisition/representation seam requires demonstrated substrate support before this family is qualified; no engine clone substitutes for it. Shared Castle ownership must be included in later excision closure.

### BIB-05 — Strict search and durable observations

Retain live/current/mirror-digest suppression, lag reporting, identity checks and one union rerank. Add underlying strict behavior to single and stack calls, with exclusive admitted providers, finite cancellation/deadline checks and admitted view validation through completion. Concurrent mutation/epoch retirement returns actual current results/view or typed stale/retry/recovery; no old content under a new citation. Known unbuilt/lagging members remain explicit; corrupt/failed/unexpectedly missing admitted members never disappear silently. Mutation/index observations include request/operation, active storage, revisions, affected content/mirror IDs and the actual committed index/checkpoint. Indexed progress follows its Palace commit; final completion validates exact mirror membership and readable checkpoint. Observer failure retains truthful durable effect/status, recognizable on replay; it cannot forge authenticated consumer publication.

### BIB-06 — Retained content, snapshots and excision

Expose finite bound retained-content reads, immutable snapshot access, historical/deleted inventory, dependency-complete excision preview, execute/status/recovery and successor rebind through the sole coordinator. Validate retained references against actual storage/content; no arbitrary filesystem/global content access, raw database or unrestricted executor. Preview/execute bind exact epoch/revision/operation/selection/snapshot actions/successor. Include deleted versions, name-only selections and shared owners; incomplete closure refuses before retirement and never widens silently. Reuse Castle's durable staging/prepared/cas-complete/publishing/references-published and replacement protocol; forward recovery never reactivates the predecessor. Remove selected library mirrors/rows/content-bearing staging, invalidate stale handles/caches/watermarks and validate/rebuild survivors at the successor. Keep service blocked/recovery-required until derived disposition and consumer publication are reconciled. Historical versions/snapshots never become search documents.

## Deliverables

- Typed supported library interfaces and implementation for BIB-01 through BIB-06, reusing existing substrate transactions and search.
- Experimental 0.2.0 metadata, runtime/lock consistency, exports, installation/API documentation and a generic adoption note with truthful support and filesystem assumptions.
- Focused admitted proofs for the acceptance cases below, including real Linux read-only and actual durable interruption boundaries; coherent installed-package qualification.
- Minimal cataloged canonical dependency excerpts captured before implementation, independent plan/code advice, primary dispositions and both normal close gates.

## Acceptance

Each case needs actual source-backed evidence. Proof design names the planted defect and intended assertion; tests may share fixtures when their distinct failure witnesses remain attributable. A skipped or unavailable requirement stays open.

| Case | Required falsifying observation |
|---|---|
| BIB-01-A | Directory/bound dynamic reads and search agree; real Linux read-only mount works without changed bytes/metadata/sidecars; recovery-needed reader refuses without repair. |
| BIB-01-B | Borrow/own success, refusal and callback exception obey ownership; instrument one writer, closed readers and no active work at unmount. |
| BIB-01-C | Namespace/CAS epoch mismatch, missing shelf, retired predecessor and escaping scratch refuse before bytes/providers/outside sentinel changes; valid successor reads only itself under stated ancestor custody. |
| BIB-01-D | Distinct source/name/query markers across sniff/conversion/index failure remain within the complete declared library I/O boundary and safe diagnostics. |
| BIB-02-A | One creation-only import includes new parent, empty folder and two files; concurrent individual writes serialize or refuse stale without objects/staging/provider effects. |
| BIB-02-B | Restart exact replay preserves IDs/result with zero completed effects; changed bytes/target/name/revision/relevant option presence under original ID refuse. |
| BIB-02-C | Real process interruption at content and namespace publication gives complete before/after or explicit recovery; preserve actual Castle residue. |
| BIB-02-D | Cover declared file/folder/payload/name envelope and no-content subtree copy; one-over-limit requests leave authority intact. |
| BIB-03-A | Text and supported image/PDF exercise only injected converter/rendering/providers; exact original bytes and bounded mirror/name structure survive. |
| BIB-03-B | Configuration, document refusal, provider error and cancellation are distinct/safe; durable replay repeats no effects; interrupted external call reports uncertainty with no blind retry. |
| BIB-03-C | Dimension/implementation/chunk/name-policy changes refuse reuse/admission or require explicit rebuild; injected progress/yield remain functional. |
| BIB-04-A | Same/different/long destination names reuse zero body calls and independently embed each own single name section; committed body vectors and original bytes match. |
| BIB-04-B | Donor removal leaves independently searchable destinations; shared-content excision refuses incomplete closure or preserves survivors. |
| BIB-04-C | Pending/unconverted/failed/removed/excised/changed/incompatible/other-shelf donors cannot supply reused vectors; interrupted publication has one independent destination or explicit recovery. |
| BIB-05-A | Only supplied shelves participate with exactly one union rerank; strict provider/storage failures produce no accepted/degraded hits; unbuilt lag remains explicit. |
| BIB-05-B | Provider-return race with rename/replace/delete/epoch retirement yields current admitted view or refusal; cancelled/expired late answers never succeed; Linux read-only path has no sidecars. |
| BIB-05-C | Namespace/entry-index/checkpoint and observer failure boundaries retain exact durable status/replay; simulated failed consumer publication is never asserted by unsigned library observations. |
| BIB-06-A | Old references/snapshot read exact replaced/deleted bytes through one coordinator; wrong bindings refuse and history stays out of current search. |
| BIB-06-B | Preview covers deleted versions, empty/name-only entries, snapshots and shared content; exact complete selection preserves unselected bytes/rows and removes selected derived disclosures at declared successor. |
| BIB-06-C | Interrupt actual Castle durable stages and successor/derived publication; supported recovery moves forward under original operation, rejects predecessor/stale index and blocks unfinished cleanup. |

The implementation-candidate sequence ends with `./bin/test --changed-from '@{upstream}'`; accepted close precedes all final tracked bookkeeping and one bare `./bin/check all` handoff gate. Complete diagnostics, warning assessment, candidate identities and owned cleanup are retained. Artifacts must contain the coherent actual dependency closure and working installed library surface. No public release is inferred by gate-proved source delivery.

## Brief refs

- [Entry-point brief: Operator requirements 5, 8 and 9, Consumers, What Biblios is not](../../briefs/BRIEF.md)
- [Castle public namespace/admission/recovery excerpts](../../docs/castle-adoption-api-0.3.0/)
- [Palace public planning/commit/strict-search excerpts](../../docs/palace-adoption-api-0.3.0/)
- [Drawbridge conversion excerpt](../../docs/drawbridge-adoption-api-0.3.0/)

## User Demo

User Demo: Review the generic bound-shelf example and its truthful status/error results; consumer-specific parity and security demonstration remain in the consumer. Human usability judgments are separate from executable acceptance and are not claimed by delivery.


## Public baseline

The shelf, stack, sealing and CLI prerequisites are shipped in the 0.1.0 baseline described in [release provenance](../../RELEASE.md#public-baseline-and-provenance). Their private lifecycle records are not imported as completed public phases. Lifecycle state comes only from canonical events. MCP planning and implementation remain held; this phase selects no successor.
