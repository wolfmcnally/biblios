# Consumer Adoption

The experimental 0.2.0 development library can operate a shelf inside storage and provider lifetimes owned by another application. The published initial release remains 0.1.0. This lane adds no CLI or MCP command and no security boundary. Applications retain authorization, encryption, audit and publication decisions.

## Choose the binding and lifetime

Declare a `ShelfBinding` with the shelf's absolute domain, separate namespace, active Castle, mirror, Palace and scratch roots, plus exact shelf/namespace/storage identities. Bind one admitted existing Castle handle with `bind_shelf`, or open one existing handle through your supplied session with `open_bound_shelf`. Choose borrowed or owned handle lifetime explicitly. Biblios neither initializes a missing shelf nor silently recovers one during opening.

Keep mounts and ancestor directories stable until close. Read-only access is available for a dynamic shelf after its writer finishes derivation, publishes a readable checkpoint and closes. It does not require sealing. Missing, corrupt, retired or unfinished storage is an error, not an empty shelf. Only the local SQLite vector backend is admitted in this interface.

## Keep the original request

Use typed `CurationRequest` actions with a caller UUIDv4 and expected revision. The coordinator serializes mutations and refuses new stale work before content staging. A completed exact retry returns its original result; changing the request under the same ID refuses. Imports reserve a complete creation-only tree, including empty directories. They do not create a general mixed-operation transaction system.

Source payload bounds and Castle's canonical metadata-request bound are separate. The supported envelope is 100 files, 100 folders, 64 MiB total payload, 4 MiB per file and 255 UTF-8 bytes per name, subject to namespace capacity and validity. Inspect actual durable status after uncertainty; do not replace an unknown operation with a new ID.

## Supply and fence providers

Converters receive exact bytes, declared media type, display name and explicit scratch. Renderers and model providers are supplied explicitly; the library does not start services or choose a replacement provider. Completed durable callbacks replay without repeated effects. A dispatched callback with no durable result is uncertain and needs consumer-controlled reconciliation before retry.

Strict search returns no degraded answer after provider/storage failure. A stack includes only its supplied members and uses one Palace union rerank. Namespace, content and mirror identities are checked through completion. Cancellation and monotonic deadlines reject late results cooperatively; they do not kill arbitrary synchronous Python. Close refuses while provider work remains active, and caller-owned providers stay caller-owned.

## Reuse compatible bodies independently

The caller supplies `ConsumerPipeline` with conversion, rendering, an exact embedding-input view, an explicit policy and a document embedder. Declare the matching Palace representation for an empty index before starting; old breadcrumb vectors are never relabeled. `literal_inputs` is an optional explicit view of each exact chunk body, including its heading. Converted-body reuse across filenames requires the caller's explicit `reuse_converted_body=True` policy.

Capture one completed donor view and nominate it for a destination. Same-shelf identity, live source, policy, ordered actual input digests and the committed donor view must agree. Missing or incompatible donors use ordinary work; stale or corrupt admission evidence refuses. Every destination filename is freshly embedded, even when names match. Destination rows remain independent after donor deletion.

Completed provider results and actual index commits survive restart. An uncertain dispatch requires explicit reconciliation and retry; a completed replay validates the real destination instead of silently repairing changed state. `pipeline.finish` removes obsolete index members and validates the watermark after all current entries finish. The writer's separate `checkpoint` prepares dynamic read-only access. Source distributions include a runnable `examples/consumer_binding.py` example with deterministic local providers.

## Coordinate history and cutover

Finite retained-content references and snapshots preserve exact replaced/deleted source bytes without adding history to current search. Explicit excision requires complete content, entry, operation and snapshot closure, including shared owners. Metadata-only redaction leaves surviving files downloadable without inventing new metadata.

A cutover invalidates the old facade, disposes of selected library-derived disclosures and returns an explicit successor binding. It does not resume application service. Interrupted cleanup blocks admission until recovery with the original roots and operation completes. Library observations describe local commits; they are not authenticated external publication receipts.

See the source distribution's `ADOPTION.md` for the full API, effect, retry, checkpoint and filesystem contract. The [Command-Line Tool](command_line.md) continues to expose the ordinary shelf operations; `remove` remains logical deletion. The surrounding application must qualify its own security, storage and publication behavior.
