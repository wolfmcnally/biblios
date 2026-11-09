---
title: "Requirement sent upstream: a Bedrock embedding provider in Palace"
status: implemented
as_of: 2026-11-07
scope: "What Biblios needs from Palace so that a shelf can declare Cohere Embed v4 through AWS Bedrock as its embedding identity, selected from the top-level Conric configuration, with the account's zero-data-retention setting checked before any request."
---

# Requirement sent upstream: a Bedrock embedding provider in Palace

As of 2026-11-07. This is the second requirement Biblios sends to Palace (the first was filtered multi-store search). It asks for a capability in Palace; it implements nothing and edits nothing in Palace. The operator ruled on 2026-11-07, through Fenric, that embedding through Bedrock comes as a Palace provider sent upstream first, not as an embedder Biblios owns and injects, in these words: "All dependency stack's we own should be configurable with Conric, so the top level (in this case Biblios) should have a section of Palace configs that now can specify Bedrock+Cohere ZDR. That's the whole point of Conric: configure all the components once at the highest level." Biblios's AWS qualification phase (`../RELEASE.md#public-baseline-and-provenance`) depends on this capability for its Bedrock lane; its other lanes do not.

## Where Palace stands today

Read from the installed Palace 0.2.0 (`project/.venv/lib/python3.12/site-packages/palace`), which Biblios pins by git tag:

- The registered embedding providers are `ollama`, `openrouter` and `endpoint` (`palace/provider_config.py`, `PROVIDERS`); `require_provider` refuses any other name, and Palace's own external-consumer brief states that AWS Bedrock is not an implemented provider.
- The `endpoint` provider is operator-controlled private HTTPS inference with a bearer credential read from an environment variable and an explicit boundary assertion (`EndpointConfig`); Bedrock authenticates by SigV4 from the AWS credential chain, so it cannot be reached through `endpoint`.
- A store's identity is the stamp Palace derives from the selector (`identity_for`): convention, model, provider string and dimension. Readers and builders validate every axis before a write or a query; equal dimensions never make vectors interchangeable.
- An embedder is anything with `max_concurrency`, `embed(texts)`, `probe()` and `close()` (`palace/index/embedder.py`, `Embedder`); the query path resolves the store's embedder again at search time (`palace/search.py`, `resolve_embedder`) and refuses if the provider changed meanwhile.
- A multi-shelf consumer that already runs on AWS reaches Bedrock today through an embedder of its own that it injects into Palace; Palace stamps a local identity for it. That shape is what this requirement replaces for Biblios: no injection, the identity declared and stamped from the selector.

## What Biblios needs

1. **A Conric configuration surface owned by Palace.** Palace reads its own `palace` section from the Conric composition the top-level application hands it (the composed files and their root, as Drawbridge already takes them), validates that section itself, and derives the store's selector from it at initialization. Biblios passes the section through unchanged and stops validating Palace's settings on Palace's behalf; today Biblios validates a `biblios.embedder` section and serializes it into the store (`project/biblios/config.py`, `_embedder`; `project/biblios/shelf.py`, `init_shelf`), which the operator's ruling replaces, not extends. Palace does not depend on Conric today; this requirement adds that dependency, in the same way Drawbridge carries it.
2. **A registered provider named `bedrock`** in `PROVIDERS["embedding"]`, selectable from that `palace` section and recorded in the store's `meta/embedder.toml` with at least `model` (the Bedrock model id of Cohere Embed v4, `cohere.embed-v4:0`), `region` and `dim`. Palace writes the selector once at initialization and never again; Palace's store stays the only record of the identity, as the brief's ruling already says.
3. **Authentication by the standard AWS credential chain only**: an instance role on EC2, or a named profile on a workstation, never a secret in the selector or in Palace's files. The provider reads no credential itself; the SDK does. A missing or expired credential is a refusal at `probe()`, before any build or query starts.
4. **A zero-data-retention check before the first request, fail closed.** Before any embedding call the provider reads the account's Bedrock data-retention mode (`bedrock:GetAccountDataRetention`) and refuses unless it reports `none`. The limit of that check must be stated in Palace's documentation as it is here: it reads an account setting at that moment; it does not prove what the service retains, it cannot see what a cross-region inference profile does in its destination regions, and it is not a substitute for the account administrator's configuration. Biblios will not claim zero data retention as effective on the strength of this check alone.
5. **Direct model id on the regional runtime endpoint**, not an inference profile, so that the retention check and the request land in the one region the selector names. Request shape as the Cohere v4 API on Bedrock takes it: `texts` in batches of at most 96, `embedding_types` `["float"]`, `truncate` `"NONE"`, an explicit `output_dimension`, and `input_type` `search_document` for indexing and `search_query` for a query.
6. **The query/document distinction surfaced in the embedder contract.** Cohere embeddings are asymmetric: a document and a query are embedded with different `input_type`. Palace's `Embedder.embed(texts)` carries no such distinction, and the search path resolves the same embedder for the query. Palace must decide how the provider learns which it is embedding (two embedders resolved by role, or an argument on the protocol); Biblios's request is only that documents and queries use the right input type without the caller knowing about it.
7. **Dimension handling settled by Palace.** Cohere Embed v4 returns at most 1536 float dimensions and accepts a requested `output_dimension`; Palace's selector carries `dim` and its store binds one dimension. Whether a `bedrock` store runs at the model's dimension or Palace pads to its configured width is Palace's design decision; Biblios validates `dim` against whatever Palace's selector accepts (`project/biblios/config.py`, `_embedder`).
8. **Identity stamping.** The stamp's provider string should identify the venue and model without the deployment's accidents: `bedrock` with the model id and dimension, the region recorded in the selector but not in the identity unless Palace judges that embeddings differ by region. Two stores built with the same selector in two regions should be judged by Palace, not by Biblios.
9. **Concurrency and cost bounded and explicit**, in Palace's existing manner for remote providers: a `max_concurrency` the operator can lower, the batch limit above, bounded retries on throttling, and no request made by configuration alone. A probe is a cheap authenticated call, not an embedding.
10. **No fallback.** A Bedrock failure is a refusal, never a silent switch to local Ollama or another provider, consistent with the identity contract and with Biblios's own rule that a build never silently falls back to a different embedder.

## What Biblios did when it shipped (as of 2026-11-07)

Palace v0.3.0 (commit de950db7…, release published 2026-11-07T16:46Z, wheel SHA-256 78054425…, verified independently against the tag and SHA256SUMS) is pinned in `project/pyproject.toml` and `project/uv.lock`. `load_settings` validates only an optional, empty `biblios` section and hands the composed files and root to `palace.settings.load_palace_settings`; `init_shelf` writes the store's selector through `palace.settings.initialize_store`; reconcile resolves the embedder with `purpose="document"` and Palace's search resolves the query side itself. The `biblios.embedder` shape is gone from the library, the proofs, the demos, the lanes and the manual. The items below were the plan; the Bedrock lane's real run remains an operator-approved action.


- Replace Biblios's own validation of the embedder (`project/biblios/config.py`, `_PROVIDERS` and `_embedder`, and the selector write in `init_shelf`) with handing the composed files and root to Palace at initialization, so that the operator's `palace` section, Bedrock and Cohere included, reaches Palace exactly as written; Biblios keeps validating only its `biblios` section. Greenfield: the `biblios.embedder` shape is removed, not kept beside the new one. The manual names the `palace` section's shape as Palace documents it.
- Pin the Palace release that carries the provider in `project/pyproject.toml` and `project/uv.lock` in one atomic toolchain change, never while a run is live.
- Run the AWS qualification phase's Bedrock lane: one shelf built and searched through the provider from an EC2 instance role, with the retention check observed to pass and its limit recorded in the results brief.

## Acceptance coverage Biblios asks Palace to provide

Offline, hermetic, in Palace's own suite: the `palace` Conric section parsed from a composed file set and refused on unknown fields; selector validation (unknown fields, missing region, wrong dimension refused); the identity stamp for a `bedrock` selector; refusal at `probe()` without credentials; refusal when the retention mode read is anything but `none`, against a stubbed client; the exact request body for a document batch and for a query; batching at 96; no request from configuration alone. Live, as an operator-approved action with its cost stated: one build and one search against the real model in one region, from a workstation profile and from an instance role, with the response dimension equal to the requested one.

## Palace's answer (as of 2026-11-07)

The Palace owner session answered on the coordination bus the same day: the capability is implemented, independently reviewed and gate-proved in Palace as its Phase 22, delivered as a local commit on Palace's `master` and not yet pushed or released; the release follows Palace's own real AWS qualification. The settled shape, with the differences from the wording above flagged so Biblios composes exactly what Palace validates:

- Palace owns and validates the top-level `palace` section (`palace.settings.load_palace_settings(paths, root=root)` takes the composed files and root as loaded; `palace.settings.initialize_store(store, settings)` writes the store's selector once and refuses a different existing one). Refusals name the field path and the file, line and column, with no values echoed. Any key other than those below, under `palace` or under `embedding`, is refused.
- `bedrock` is registered beside `ollama`, `openrouter` and `endpoint`. Credentials come from the SDK chain only; the selector may name an optional `profile`; a missing credential refuses at probe. Ambient endpoint overrides are ignored so the request lands in the selector's region.
- Before the first request each provider instance reads the account's data retention in the selector's region and refuses unless the mode is exactly `none`; Palace documents that the check speaks for one region at one moment and is blind to profiles and per-model overrides.
- Direct model id only; the allowlist is exactly `cohere.embed-v4:0`; inference-profile ids and ARNs are refused; `region` is required. Builds use `search_document`, queries `search_query`; callers never pass the input type.
- `dim` is required and must be 256, 512, 1024 or 1536; the vector table is declared at that width and a mismatch with an existing table refuses before any request.
- Identity as Palace records it: provider `bedrock:<region>`, model `cohere.embed-v4:0`, dim, plus Palace's text convention. Difference from the wording above: the region sits inside the provider string, and input types are not identity axes because documents and queries share one space.
- At most 96 inputs per request under a 120,000-unit aggregate budget; build concurrency ceiling 4; retries only on throttling, unavailability, internal, model-timeout, model-not-ready and connection errors, four attempts with backoff, with one audit record per attempt. No fallback anywhere.
- A requirement Biblios had not listed: Bedrock is third-party inference, so the store must carry Palace's published-corpus assertion, and the personal store or vault overlap is refused unconditionally.

The exact section Biblios composes:

```yaml
palace:
  embedding:
    provider: bedrock
    model: cohere.embed-v4:0
    region: us-east-1
    dim: 1536
    published_corpus: true
    published_corpus_note: "<what is published and where>"
    published_corpus_asserted_at: "2026-11-07T00:00:00Z"
    # optional:
    profile: <aws profile name>
```

Also settled: Palace's `resolve_embedder` takes a required `purpose` keyword; Conric is a git-pinned Palace dependency, so a hash-locked install of Palace excludes and separately installs it (Palace's release notes show how); direct in-region Embed v4 is documented today only in `us-east-1`, `eu-west-1` and `ap-northeast-1`. Biblios's qualification lanes already read the `palace.embedding` selector (provider, model, region) when deciding whether the Bedrock lane runs.

## Release dependency and coordination

The Bedrock lane of `aws-ebs-qualification` waits for a tagged Palace release containing this provider; the hermetic and ingestion lanes do not and run first. The Palace owner session is on the coordination bus and has acknowledged this requirement; it will reply with the tag and commit when the qualified release exists, and Biblios moves its pin only then, never while a run is live. Nothing here edits Palace. Marketplace agreements for the Cohere models, the account's retention configuration and the instance role's `bedrock:InvokeModel` and `bedrock:GetAccountDataRetention` grants are administrator and operator actions outside both repositories.
