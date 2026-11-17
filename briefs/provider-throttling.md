---
title: "Provider throttling: typed failures and pacing in Palace, truthful outcomes in Biblios"
status: implemented
as_of: 2026-11-17
scope: "What Biblios needed from Palace so that a service integrating Biblios can tell a provider throttle from a genuine failure without reading error text, pace every physical embedding request with one limiter it owns, and requeue a document that was only throttled; and how Biblios reports it."
---

# Provider throttling: typed failures and pacing in Palace, truthful outcomes in Biblios

As of 2026-11-17. It is a requirement Biblios sent upstream to Palace, released together with its adoption. It describes the capability class, a deployed service that embeds through a rate-limited remote provider and integrates Biblios as a library, never a particular consumer. The operator directed on 2026-11-17, relayed by the factotum, that the owned dependencies be changed and released so that integration can continue, in these words (the consumer's name redacted here): "I said you are to release everything you need to touch that we own so my colleague can continue to integrate Biblios into [the consuming service]!"

## Why

Before this change a service could not distinguish a throttle from any other provider failure:

- Palace's remote embedders raised a plain index error after their own retries, distinguishable only by its message; the delay ignored or under-honoured the provider's retry hint and had no jitter, and nothing could pace request starts across embedder instances.
- Biblios turned every provider exception into `callback-failed` with effect `uncertain`, kept only an entry's first failure, and went on admitting entries and sending slices after the provider had started refusing. A throttled document therefore spent the service's retry budget for genuine failures, and a throttle could hide an earlier timeout or the reverse, depending on timing.

## What Palace provides (Palace 0.6.0)

Palace owns physical requests, so it owns pacing, retry and the typed failure. Only one layer retries.

1. **A typed failure.** A remote embedder's terminal failure is a `ProviderCallError`, still an index error, carrying the provider, operation, the latest failure's `kind`, the number of attempts, why no further attempt was made, every attempt's classification, the provider's retry hint (uncapped, seconds from receipt), the leading inputs already answered, and `delivery`: what is known about whether the provider performed the work (`not-sent`, `rejected`, `unknown`, `processed`), merged over every attempt so a later definite answer never erases an earlier ambiguous one. `ProviderThrottledError` marks a throttle as the latest failure; a caller reads `delivery`, never the class, to decide whether anything may have been performed. Message text is not the contract.
2. **One retry rule.** Jittered exponential backoff with a cap; a provider hint is a floor that jitter never reduces; a hint beyond the permitted wait, the caller's deadline or the policy's budget ends the request at once with the hint, never an early retry.
3. **A pacer at the physical request.** Every attempt, retries included, asks an injected request pacer before it is sent and settles it afterwards. The reference pacer limits requests and conservative UTF-8 request bytes per interval, refuses a request that could never fit, serves queries first with bounded progress for documents, and holds after a throttle with a hint. It is process-local and counts bytes, not tokens.
4. **Deadline and cancellation scope.** A context scope carries an absolute deadline and a cancellation predicate into pacer waits, backoff and the start of every attempt. Within a request Palace lowers its per-operation transport timeouts to the time remaining, which is not a total-request bound.
5. **Accounting.** The usage collector counts every physical attempt, including throttled and indeterminate ones, and can be nested so each enclosing total counts each attempt once.

## How Biblios reports it (Biblios 0.5.0)

- **Classification once per unit of work, after it drains.** An entry's provider failures are all kept, in slice order, and classified only after every slice it submitted has returned, so the result never depends on completion order. The entry is `pending` with effect `none` only when every failure left nothing performed (`delivery` `not-sent` or `rejected`) and was a throttle, a deadline, a cancellation or a pacer refusal; otherwise it is `uncertain`. The code says why: `provider-throttled` whenever any attempt was throttled, whatever the certainty; otherwise the definite stop's own code (`deadline-exceeded`, `cancelled`, `cancellation-failed`, `pacer-refused`); otherwise the existing code, `callback-failed` for a provider failure. Other definite refusals, such as a denied credential, stay uncertain. Only Palace's typed failure is classified: any other exception from the embedder, including one shaped like a Biblios error, is `callback-failed` and uncertain.
- **Stopping fresh work.** Before admitting an entry and before sending any slice, the call looks at every submitted call that has already finished. Once one was throttled, no entry is admitted and no further slice is sent for any entry; what was submitted drains, an entry whose slices were all answered is still committed, an admitted entry left incomplete is reported `pending`/`provider-throttled`, and unreached entries stay marked for the next call. A serial call visits no further entry.
- **Stored results are unchanged.** The recorded result keeps exactly its earlier keys, so no stored shape or schema changes. `BringCurrentResult.throttled` is computed from the stored entry codes and so holds on a replay. Call-local facts live in `BringCurrentResult.local` and are never recorded: the longest provider hint still to wait, embed invocations that raised, answers returned that no persisted stage keeps, and Palace's usage summary for the call's physical attempts (none, not zero, when the embedder records nothing). A replay therefore never renews a retry hint. A redacted replay may have lost its throttled entries and is not a scheduling input.
- **`provider_calls`** counts embed invocations that returned, when each returns, before its answer is checked or used.
- **Context.** Each pooled provider call runs in its own copy of the caller's context, so Palace's deadline scope, the call's usage collector and the caller's own context variables reach it; only the collector is shared.
- **Search.** A query embedding whose Palace failure left nothing performed reports its own code at stage `embedding` with effect `none`: `provider-throttled` for a throttle, with a numeric `retry_after`, or `deadline-exceeded`, `cancelled`, `cancellation-failed` or `pacer-refused` for a definite stop. An ambiguous throttle and every other failure stay `callback-failed`. The query embedder runs under the call's deadline and cancellation scope.
- `ConsumerError.retry_after` is a number only; no provider text reaches a Biblios diagnostic. Biblios adds no limiter and no retry of its own.

## What the integrating service does

Build one pacer and pass it to every embedder it creates, with queries first; choose the retry policy and deadlines; on `pending` with `provider-throttled`, requeue its own record after `local.retry_after` under a new operation id without charging its failure retry; on `uncertain`, charge it as before and use the code only for scheduling. The same operation id replays the recorded result.

## Limits

- The 120,000 figure in Palace is a per-request size cap in conservative UTF-8 bytes, not a per-minute token or request quota. No numeric rate ships, and nothing here establishes any model's, Region's or account's quota.
- The reference pacer enforces no token quota and coordinates one process only.
- There is no guaranteed total duration. A deadline bounds waiting and the start of work; within a request Palace lowers each transport timeout to the time remaining, but a response that keeps arriving in pieces can outlast it and SDK credential resolution is not bounded. Biblios never abandons an outstanding call: it drains every submitted call before returning, so a call can end after its deadline.
- Every proof is offline and hermetic. No live throttle run qualified either release.
- Not done: keeping answered slices across calls, aligning Biblios's slices to the provider's request shape, coordination across processes, and throttling in conversion stages.
