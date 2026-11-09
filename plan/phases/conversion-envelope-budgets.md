---
id: conversion-envelope-budgets
title: Explicit bounded conversion and snapshot envelopes
order: 15/2
parent: null
depends_on:
- adapter-consumer-adoption
informs: []
review_lane: full
evidence_lane: full
---

# Explicit bounded conversion and snapshot envelopes

The operator authorized this bounded compatibility correction on 2026-11-09 after a synthetic accepted source expanded beyond the default conversion-body budget and its committed vector snapshot exceeded the default snapshot budget. The completed parent and its consumed advice remain closed; this dependency-based correction receives its own full plan/code cycle for the changed public admission and persisted-replay boundary.

## Goal

Allow explicitly declared larger finite conversion body, mirror and committed-snapshot budgets without changing defaults, source limits, row ceiling, representation or original-operation semantics. Preserve complete accepted content and coherent read/write persistence limits; no truncation or unconverted workaround.

## Decomposition

One bounded correction outcome across conversion admission, durable checkpoints and committed-vector publication/readback. The separately owned Palace snapshot-admission capability is a prerequisite only for dependent integration, qualification and final code-review freeze. Independent body/mirror/checkpoint work proceeds through the normal plan gate. No completed phase is reopened and no successor is selected.

## Settled correction decisions

1. Keep body 4 MiB, mirror 8 MiB and snapshot 64 MiB defaults, the 16,384-row ceiling and source-upload limits unchanged. Accept strict positive finite body/mirror declarations up to the existing 512 MiB checkpoint boundary; combined encoded payload must independently fit.
2. Apply the existing 512 MiB checkpoint boundary to actual serialized UTF-8 JSON bytes, including escaping and vector arrays, both before parsing and before durable replacement. Failed replacement preserves the previous checkpoint and retains truthful effect/recovery classification. Limit this correction to pipeline checkpoints; other checkpoint callers retain their behavior.
3. Add an explicit ConsumerPipeline max_snapshot_bytes resource budget, default 64 MiB, separately from semantic DerivationPolicy. Use the same admitted byte/count budget for every donor, destination, replay and post-commit snapshot and Palace publication admission. Do not change unchanged policy fingerprints or donor eligibility.
4. Semantic body/mirror/count-policy changes remain changed original requests. Preserve an existing uncertain dispatch and refuse blind converter retry. More read resources do not authorize repeating completed external work.
5. Compose Palace's delivered public reader and transactional snapshot admission under the existing uninterrupted same-store writer lock and connection lifetime. Publication exceeding its declared budget rolls back before commit; a later post-commit failure remains committed-but-unaccepted. No pre-embedding estimate or rollback of external model calls is promised.
6. Synthetic vectors only in qualification. Keep dimensions, padding, chunker, rendering, exact input mapping, destination ownership and independently embedded names unchanged. No cross-shelf reuse or consumer-specific policy enters the library.
7. Bind the actual immutable delivered Palace API and coherent package metadata/lock before dependent qualification, independent code advice and final gates. Proposed upstream definitions are design evidence, not shipped support.

These decisions implement the authorized correction; there is no new product, security, deployment or publication decision. Independent plan advice reviews this elaborated phase as well as the implementation plan.

## Deliverables

- Explicit finite body/mirror and snapshot resource admission with unchanged defaults and semantic request identities.
- Coherent bounded pipeline checkpoint read/write enforcement using actual encoded bytes and preserved prior records on refusal.
- Delivered Palace snapshot-publication admission integrated with every required committed readback under existing custody and lifecycle rules.
- Focused admitted regression proofs, one retained expanded 4,096-dimensional synthetic qualification, clean installed artifacts and affected Linux/read-only qualification.
- Generic API/manual documentation, independent plan/code advice, primary dispositions, accepted close and gate-proved source delivery without a new tag or public release.

## Acceptance

| Case | Required falsifying observation |
|---|---|
| ENV-01 | Defaults still refuse oversized results; explicit adequate finite body/mirror budgets preserve the complete expanded result. Invalid declarations refuse before callbacks; no source/row/default increase. |
| ENV-02 | Checkpoint writer and reader agree at the exact encoded boundary, including Unicode/control escaping and vectors; one-over refuses before durable replacement/parsing and retains the previous record. |
| ENV-03 | Unchanged completed operations replay without callback/model effects; changed semantic policy conflicts; retained uncertain dispatch refuses blind retry. Snapshot resource budgets do not enter semantic policy identity. |
| ENV-04 | The preserved expanded renderer at 4,096 stored dimensions produces complete committed readback with an adequate finite snapshot budget; insufficient publication budget rolls back, and every donor/destination/replay read uses the declared budget. |
| ENV-05 | Restart, donor body reuse, independently embedded equal/different destination names and donor deletion preserve destination ownership; post-commit acceptance failure remains truthful. Reuse existing qualified proofs unless the changed boundary needs a distinct falsifier. |
| ENV-06 | Exact wheel/sdist contain coherent metadata, dependency and corrected bytes; clean installation and affected Linux/read-only proofs pass. Required final changed-path acceptance and bare full handoff gate pass on their exact candidates. |

The large expanded case is one bounded explicit qualification with complete retained diagnostics; representative smaller contract proofs run in the normal suite. All new/modified proofs follow ordinary admission and attributed observed-red policy, with no supplemental survey. Preserve original failed and successful evidence.

## Brief refs

- [Library requirements and consumer boundary](../../briefs/BRIEF.md)
- [Completed adoption capability contract](adapter-consumer-adoption.md)
- [Delivered Palace vector API reference](../../docs/palace-vector-api-0.3.0/)

## User Demo

User Demo: N/A — this correction changes deterministic library admission and persistence bounds; executable proofs establish its outcome. The existing generic adoption usability action remains open. Changed rendered documentation still receives its applicable visual check.

## Exclusions and delivery

No MCP/CLI feature, model or renderer change, global cap relaxation, new security system, cross-shelf reuse, credentials, live user data, cloud action, destructive history operation or new public tag/release. Ordinary commit and fast-forward source delivery follows both required gates. Parent acceptance and review history remain intact.
