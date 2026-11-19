---
id: palace-cache-exposure
title: Opt-in exact vector acceleration at the consumer seam
order: '10'
parent: null
depends_on:
- responsive-provider-reads
- user-owned-mirrors
informs: []
review_lane: full
evidence_lane: full
---

# Opt-in exact vector acceleration at the consumer seam

On 2026-11-19 the factotum relayed the operator's direction for a new Biblios release that pins the published Palace v0.7.0 and exposes its acceleration and memory observability, with the operator's words: "So after the Palace release we'll want a new Biblios release that pins and exposes the capabilities of the new Palace." and "Well want to surface that, and then bubble it up through Biblios and Tower so we can monitor and deal with memory management and pressure effectively at every level." The relayed assignment authorizes a new phase on the released v0.5.0 baseline, the normal review, gate, signing and publication workflow, and a new release. It makes no default change: whether a consuming service turns acceleration on is a later operator decision taken in that service. This phase is distinct from the in-progress `unified-indexing-core` and `persistent-edit-observation` phases and takes, closes or rebinds neither.

## Goal

A long-lived consumer can opt a shelf into Palace 0.7.0's exact in-memory vector cache through Biblios's supported binding, search alone or in a stack with the same answer as the released exhaustive search, and monitor at every scope what is stored, what is resident, how much memory the cache owns and how much headroom remains, and whether each search ran from memory, from the exact released SQL, or on a configured fallback. A consumer that passes nothing runs exactly the released search.

## Decomposition

One coherent outcome at the consumer seam: the dependency pin, one configuration object, the per-shelf feed opt-in through the existing writer, the search routing, the outcome and statistics reports, the error vocabulary, documentation and the release. No independently acceptable prerequisite or human seam separates them; the upstream mechanism is already released.

## Settled boundaries

1. **Default unchanged.** Without an explicit acceleration object every search is byte-for-byte the released path: no cache, no feed module import and no new result content beyond an absent (`None`) report. Feed enablement is explicit per shelf and is never performed implicitly by search, indexing, sealing or binding.
2. **One writer.** Enabling or disabling a shelf's change feed is a store write performed only through the binding's existing writer admission (the Palace writer lock that every Biblios and Palace writer takes), on a read-write binding. A feed that already exists is left as it is; starting a new lineage, the remedy for a divergence, requires an explicit request. Biblios never resets a lineage on its own.
3. **Fail closed on integrity and access.** A diverged or malformed feed, a vector of the wrong width, an unreconcilable load, an authorization refusal or an error in the authorization hook fails the search under every setting, with its own code, and never falls back. Fallback is the caller's explicit choice and applies only to Palace's operational unavailability (`feed-disabled`, `budget`, `queue-full`, `snapshot`); every fallback is reported per shelf.
4. **Access binds to admitted shelves.** The cache serves only stores that belong to the shelves of the call in progress; the consumer's hook decides with the shelf's admitted view, never with a bare path. A store the call did not admit is refused.
5. **Scope and source are explicit.** Stored figures are store-scope, read in constant work from the shelf's feed state and unknown (`None`), never zero, when the feed is disabled or the index is absent. Resident figures are cache-scope from Palace's ledger, an estimate of the cache's own accounting. Peak resident memory is process-scope from the operating system. No figure carries text, chunk ids or queries.
6. **Existing guarantees hold.** Per-hit evidence, the serving check, strict-search withholding, stale-view fencing, provider fences and the error envelope keep their behaviour with or without acceleration. Biblios's stack search moves from Palace's one-shot multi-store search to its prepare-then-execute pair, which Palace documents as the same operation.
7. **No upstream change.** Only the published v0.7.0 API is used, pinned by tag and verified against the remote, the lock, the release's checksums and an installed import.

## Deliverables

- Palace pinned to the published v0.7.0 tag; Biblios version 0.6.0.
- A consumer configuration object owning one process-wide Palace vector cache with an explicit unavailability policy, budget, load limits and an admitted-shelf authorization hook; warm, evict and statistics operations.
- Read-only per-shelf stored-vector status, and explicit feed enable/disable through the binding's writer.
- Single-shelf and stack search that accept the configuration object and report each shelf's route; distinct error codes for unavailability, authorization and feed divergence.
- Offline synthetic proofs for the acceptance cases below, admitted through the proof estate.
- Consumer documentation of the monitoring model and its limits; release records; independently reviewed implementation; installed artifact checks and both required gates; a signed `v0.6.0` tag and published release with verified assets.

## Acceptance

| Case | Falsifying observation |
|---|---|
| ACC-01 | Without acceleration, single and stack search return the released results and no route report, and the search imports neither Palace's cache nor feed module. |
| ACC-02 | On a feed-enabled shelf, accelerated single and stack searches return exactly the same hits, order, scores and evidence as the exact search, report cached legs, and stay equal after the shelf's entries are added, changed, deleted and excised through Biblios's writers, with the feed never diverged and resident rows equal to stored vectors. |
| ACC-03 | A budget too small for the shelf refuses with its own code under `refuse` and answers exactly with a reported `budget` fallback under `fallback`; the statistics show the refusal or fallback counter and the budget, owned and headroom figures. |
| ACC-04 | A shelf without a feed is reported as unavailable (`feed-disabled`) in the same two ways; its stored status says disabled with unknown counts. |
| ACC-05 | A false or failing authorization hook, or a store the call did not admit, fails the search under `fallback` with the authorization code and evicts the store. |
| ACC-06 | A chunk written outside the feed makes accelerated search fail with the divergence code under `fallback`, the stored status reports the divergence, and only an explicit new lineage clears it. |
| ACC-07 | Feed enable/disable refuses on a read-only binding and while another writer holds the lock, and a repeated enable keeps the existing lineage. |
| ACC-08 | Focused checks, proof governance, wheel/sdist build and a clean install from the published tags, changed-path selection and the one bare full gate pass on their exact candidates. |

## Brief refs

- [Library requirements, bounded consumer operations and lagging search](../../briefs/BRIEF.md)
- [Provider throttling: typed failures, pacing and outcomes](../../briefs/provider-throttling.md)
- [Completed responsive reads](responsive-provider-reads.md)
- [Completed user-owned mirrors](user-owned-mirrors.md)

## User Demo

User Demo: N/A — every case has a deterministic synthetic oracle. Performance, memory sizing on real corpora and the decision to make acceleration a consuming service's default stay with the operator and are not claimed.

## Exclusions

No change to Palace or any other dependency's source, no default change, no activation in any consumer or live store, no cross-process cache, no performance or latency claim, no Linux or ARM64 Linux qualification (Biblios has no hosted CI; only local macOS ARM64 runs), no MCP/CLI feature, credentials, permissions, history rewrite or unrelated release.
