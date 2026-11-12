# Consumer Adoption

The experimental 0.3.0 library can operate a shelf inside storage and provider lifetimes owned by another application. This lane adds no CLI or MCP command and no security boundary. Applications retain authorization, encryption, audit and publication decisions.

## Choose the binding and lifetime

Declare a `ShelfBinding` with the shelf's absolute domain, separate namespace, active Castle, mirror, Palace and scratch roots, plus exact shelf/namespace/storage identities. Bind one admitted existing Castle handle with `bind_shelf`, or open one existing handle through your supplied session with `open_bound_shelf`. Choose borrowed or owned handle lifetime explicitly. Biblios neither initializes a missing shelf nor silently recovers one during opening.

Keep mounts and ancestor directories stable until close. Read-only access is available for a dynamic shelf after its writer finishes derivation, publishes a readable checkpoint and closes. It does not require sealing. Missing, corrupt, retired or unfinished storage is an error, not an empty shelf. Only the local SQLite vector backend is admitted in this interface.

## Keep the original request

Use typed `CurationRequest` actions with a caller UUIDv4 and expected revision. The coordinator serializes mutations and refuses new stale work before content staging. A completed exact retry returns its original result; changing the request under the same ID refuses. Imports reserve a complete creation-only tree, including empty directories. They do not create a general mixed-operation transaction system.

Source payload bounds and Castle's canonical metadata-request bound are separate. Use `bound.curate(request, settings=settings)` with [composed configuration](getting_started.md) to apply `biblios.max_file_bytes` to create, replace, import and copy. The per-source-file default is 100 MiB (104857600 bytes), including calls that omit settings; an override must be a positive integer byte count. Supply the intended settings on each call. Import/copy aggregate payload is limited to the larger of 64 MiB and the configured per-file allowance. The remaining envelope is 100 files, 100 folders and 255 UTF-8 bytes per name, subject to namespace capacity and validity. Inspect actual durable status after uncertainty; do not replace an unknown operation with a new ID.

## Supply and fence providers

Converters receive exact bytes, declared media type, display name and explicit scratch. Renderers and model providers are supplied explicitly; the library does not start services or choose a replacement provider. Completed durable callbacks replay without repeated effects. A dispatched callback with no durable result is uncertain and needs consumer-controlled reconciliation before retry.

Strict search returns no degraded answer after provider/storage failure. A stack includes only its supplied members and uses one Palace union rerank. Namespace, content and mirror identities are checked through completion. Cancellation and monotonic deadlines reject late results cooperatively; they do not kill arbitrary synchronous Python. Close refuses while provider work remains active, and caller-owned providers stay caller-owned.

## Reuse compatible bodies independently

The caller supplies `ConsumerPipeline` with conversion, rendering, an exact embedding-input view, an explicit policy and a document embedder. Declare the matching Palace representation for an empty index before starting; old breadcrumb vectors are never relabeled. `literal_inputs` is an optional explicit view of each exact chunk body, including its heading. Converted-body reuse across filenames requires the caller's explicit `reuse_converted_body=True` policy.

Capture one completed donor view and nominate it for a destination. Same-shelf identity, live source, policy, ordered actual input digests and the committed donor view must agree. Missing or incompatible donors use ordinary work; stale or corrupt admission evidence refuses. Every destination filename is freshly embedded, even when names match. Destination rows remain independent after donor deletion.

Completed provider results and actual index commits survive restart. An uncertain dispatch requires explicit reconciliation and retry; a completed replay validates the real destination instead of silently repairing changed state. `pipeline.finish` removes obsolete index members and validates the watermark after all current entries finish. The writer's separate `checkpoint` prepares dynamic read-only access. Source distributions include a runnable `examples/consumer_binding.py` example with deterministic local providers.

## Declare finite resource envelopes

Conversion output can be much larger than its source. Body and mirror defaults remain 4 MiB and 8 MiB; a `DerivationPolicy` may explicitly declare larger positive bounds up to 512 MiB each. The complete checkpoint must independently fit within 512 MiB of encoded UTF-8 JSON, including escaped text and vectors. Reads check this boundary before parsing; an oversized write preserves the previous checkpoint and reports recovery required. The row ceiling remains 16,384.

`ConsumerPipeline(max_snapshot_bytes=128 * 1024 * 1024)` is an example of an explicit larger snapshot allowance; the default remains 64 MiB. This separate resource budget applies consistently to donor, destination, replay, publication and final readback. It does not change semantic policy identity. Changing body, mirror or chunk declarations under an original operation ID still changes that request.

Snapshot database bytes and checkpoint JSON bytes are different limits. Dense vectors can make JSON the binding ceiling, and encoding a checkpoint holds its serialized text alongside the document. Neither allowance bounds process memory. Publication checks staged database size before COMMIT, after embedding; no pre-embedding estimate or reversal of external effects is promised.

Keep the original operation after refusal. A successfully rolled-back publication can leave a rendered mirror and a `vectors-ready` checkpoint with no committed destination rows; an adequate snapshot allowance can finish that same operation without repeating completed provider work. Generic recovery diagnostics also cover integrity and I/O failures, so do not infer a safe retry from exception prose. Failed post-commit readback is committed but unaccepted, and unresolved external dispatch still requires reconciliation.

## Coordinate history and cutover

Finite retained-content references and snapshots preserve exact replaced/deleted source bytes without adding history to current search. Explicit excision requires complete content, entry, operation and snapshot closure, including shared owners. Metadata-only redaction leaves surviving files downloadable without inventing new metadata.

A cutover invalidates the old facade, disposes of selected library-derived disclosures and returns an explicit successor binding. It does not resume application service. Interrupted cleanup blocks admission until recovery with the original roots and operation completes. Library observations describe local commits; they are not authenticated external publication receipts.

See the source distribution's `ADOPTION.md` for the full API, effect, retry, checkpoint and filesystem contract. The [Command-Line Tool](command_line.md) continues to expose the ordinary shelf operations; `remove` remains logical deletion. The surrounding application must qualify its own security, storage and publication behavior.
