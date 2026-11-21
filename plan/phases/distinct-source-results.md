---
id: distinct-source-results
title: One result per source at the consumer seam
order: '11'
parent: null
depends_on:
- palace-cache-exposure
informs: []
review_lane: full
evidence_lane: full
---

# One result per source at the consumer seam

On 2026-11-21 the operator authorized, through the factotum, an ordered feature: a Palace enhancement and minor release, then a Biblios integration and minor release, then a consuming service's pin. The factotum then designated this phase's owner as its sole canonical writer through release v0.7.0 and handback, without transfer of any other phase. Palace v0.8.0 is published (tag 0f25c697 peeling to 2f96c7d390fd1bf24098e884f70d68e4a57d78e8) with an opt-in execution-time `DistinctResults(key, limit)` selection over its bounded, already reranked pool. Preparation in an isolated copy produced a candidate with independent plan and code advice; those reports are prior observations, and this phase runs its own review on the canonical candidate. This phase exposes Palace's selection at Biblios's consumer seam.

## Goal

A consumer can ask bound single-shelf or stack search for at most `limit` results with one result per source, where the consumer supplies the source key and its own accepted-version rule, Biblios's per-hit authority decides eligibility before grouping, and a stale or rejected top candidate never hides a valid sibling of the same source. Without the option, search is unchanged.

## Settled boundaries

1. **Default unchanged.** Without `distinct`, search results, provider calls and inputs are those of 0.6.1.
2. **Same inputs, one rerank.** The option never widens Palace's candidate input, adds a retrieval, embedding or rerank call, or refills from a provider. A pool short of distinct sources returns fewer results and says so.
3. **Authority before grouping.** A candidate is eligible only after Biblios's existing per-entry and per-hit checks serve it and the consumer's key accepts it. A rejected or unverifiable head never fills its group. Every selected hit is re-read at assembly; one deleted, replaced, re-indexed or whose mirror changed since selection is withheld without refill and reported, and a renamed one is served under its new path.
4. **Generic contract.** The key receives a Biblios candidate (shelf, entry, accepted stage) whose entry Biblios has already served, never a consumer type; the default key is one source per entry. A consumer key may span shelves in a stack.
5. **Consumer failures are typed.** A key that raises or returns a non-string fails the search with `callback-failed`; it is never skipped.
6. **No upstream change.** Only the published Palace v0.8.0 API is used, pinned by tag and verified.

## Deliverables

- Palace pinned to the published v0.8.0 tag; Biblios version 0.7.0.
- `DistinctSources(key=None)`, `SourceCandidate` and `DistinctReport`; `distinct=` on `BoundShelf.search` and `BoundStack.search`; `SearchResult.distinct` and `StackResult.distinct` (a `DistinctReport` whenever requested).
- Offline synthetic proofs for the acceptance cases, admitted through the proof estate.
- Consumer documentation and release records; independent plan and code review; installed-artifact checks and both gates; release under explicit authority.

## Acceptance

| Case | Falsifying observation |
|---|---|
| DIS-01 | Without `distinct`, results equal 0.6.1's and `distinct` is `None`. |
| DIS-02 | `limit` 10 with three early duplicates of a kept source returns 10 sources from later groups with one rerank call. |
| DIS-03 | A pool with fewer than 10 distinct eligible sources returns fewer, reports underfill, and makes no further retrieval or provider call. |
| DIS-04 | A stale top chunk (per-hit authority failure) or a version the consumer key rejects does not hide its valid sibling. |
| DIS-05 | In a stack, the default key is per shelf and entry; a consumer key spanning shelves keeps the best-ranked eligible member, including when the head is stale on another shelf. |
| DIS-06 | A selected hit whose entry is re-indexed, deleted, replaced or whose mirror is edited between selection and assembly is withheld, reported and not refilled; a renamed one is served under its new path. |
| DIS-07 | Accelerated and exact distinct searches return equal hits and outcomes. |
| DIS-08 | A failing consumer key is `callback-failed`. |
| DIS-09 | Focused checks, proof governance, artifact build and clean install, changed-path selection and the bare full gate pass on their exact candidates. |

## Brief refs

- [Library requirements, bounded consumer operations and lagging search](../../briefs/BRIEF.md)
- [Completed vector acceleration](palace-cache-exposure.md)

## User Demo

User Demo: N/A — deterministic synthetic oracles.

## Exclusions

No Palace or other dependency change, no default change, no consumer-specific type, no refill or wider provider input, no change to the in-progress phases owned by another operator, no consuming-service action.
