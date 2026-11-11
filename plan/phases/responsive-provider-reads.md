---
id: responsive-provider-reads
title: Responsive admitted reads during provider work
order: 31/4
parent: null
depends_on:
- adapter-consumer-adoption
- conversion-envelope-budgets
informs: []
review_lane: full
evidence_lane: full
---

# Responsive admitted reads during provider work

The operator authorized this bounded correction on 2026-11-10 after a deterministic held-provider reproduction showed public reads waiting for document embedding. The completed adoption and envelope phases and their spent advice remain closed. This dependency-based correction receives its own ordinary full plan/code review cycle.

## Goal

Public reads and strict search can promptly observe the admitted lagging shelf while document embedding is in progress, without accepting stale provider results or weakening lifetime, single-coordinator, writer, retry or publication safety.

## Decomposition

One concurrency correction within the existing library outcome. The implementation separates provider work from short namespace-critical sections and preserves the existing Palace writer contract. No new upstream API or namespace coordinator is needed. A nullable parent preserves the completed dependency graph without reopening completed work.

## Settled boundaries and review inputs

1. Preserve the published experimental v0.2.0, source history, provider/model/input/representation and resource-budget semantics. Gate-proved source delivery is separate from a later versioned release and consumer adoption.
2. Keep one existing namespace handle/coordinator. Never open a hidden second writer or use private lock-release or database patching APIs. Protect the admitted binding and provider lifetime until all owned work returns.
3. Do not hold the namespace mutex over document embedding. Preserve uninterrupted same-store Palace writer admission and connection lifetime across donor read, planning, embedding, commit, mandatory readback and close. Other supported writers must not wait for that admission while holding the namespace mutex needed by the active derivation.
4. Preserve the last admitted mirror/index view while provider work is outside the namespace mutex. Revalidate exact binding, namespace/CAS view, source/name and derived state after unlocked work and before commit/publication. A concurrent change refuses stale acceptance; it does not silently retarget an operation.
5. Keep existing cooperative cancellation, deadline, external-dispatch uncertainty and durable replay behavior. Provider failure or a late result never supplies fabricated rollback, acceptance or automatic redispatch. Close must refuse while provider work is active.
6. Preserve strict search live/current suppression, complete before/after view fences and explicit indexing lag. Do not relax the existing two-second responsiveness requirement or replace it with a nonblocking-lock proxy.
7. Bound the correction to the actual contention and its necessary lock-order/publication consequences. No generic concurrency survey, resource-cap redesign, Palace edit, new security system or unrelated API is included.

These boundaries refine the authorized repair; independent plan advice reviews the phase elaboration and the implementation plan. Ordinary engineering choices remain with the primary.

## Deliverables

- Document embedding outside namespace-critical sections with consistent supported writer ordering and protected lifetime.
- Exact stale-view fencing and last-admitted mirror/index publication across the unlocked provider boundary.
- Finite public-boundary regression proofs for responsiveness, mutation races, competing writers, lifetime and retry/cancellation outcomes, admitted through the existing proof estate.
- Generic API documentation, independently reviewed implementation, actual affected artifact checks and both required source-delivery gates.

## Acceptance

| Case | Falsifying observation |
|---|---|
| READ-01 | Hold the actual injected document embed callback with an event; public view/files/status and strict search finish before release and within two seconds, with truthful indexing lag and no stale hits. Assert the callback and reader windows were reached. |
| READ-02 | A namespace/source/name or admitted derived-view change during the unlocked callback refuses stale commit/publication. Existing unaffected indexed content and mirrors remain readable while the new work is held. |
| READ-03 | A competing supported derivation/donor/completion/history writer cannot invert the namespace/Palace lock order or duplicate external dispatch. Scope tests to the shared writer-admission boundary and concrete reachable paths. |
| READ-04 | Active provider work prevents close; cancellation, callback error and uncertain dispatch preserve truthful effects and exact retry/replay behavior. Every test-owned thread is released and terminal on success or failure. |
| READ-05 | Focused consumer checks, proof governance, coherent wheel/sdist inspection and clean installed synthetic smoke, final changed-path selection and one post-bookkeeping bare full gate pass on their exact candidates. |

Reuse unaffected existing lifecycle, replay, read-only and strict-search proofs. Add only the owning-boundary obligations needed by this changed concurrency contract; no expanded stress, performance or full-RAG qualification. Preserve the original failed diagnostic and all new failures without relabeling them.

## Brief refs

- [Library requirements, bounded consumer operations and lagging search](../../briefs/BRIEF.md)
- [Completed adoption contract](adapter-consumer-adoption.md)
- [Completed envelope correction](conversion-envelope-budgets.md)

## User Demo

User Demo: N/A — the correction has deterministic held-callback and stale-publication oracles. Existing manual adoption usability remains open; no human acceptance is claimed by source delivery.

## Exclusions

No MCP/CLI feature, dependency update, upstream edit, model/input/rendering change, additional namespace writer, relaxed responsiveness threshold, live user data, cloud action, credentials, permissions, history rewrite or new tag/release. Ordinary explicit-path commit and nonforced fast-forward source delivery follow required gates. Report later release readiness separately.
