Embed the returned `plan.embed_inputs` with the declared implementation and pass vectors in that exact order to `commit_plan(..., identity=expected_identity)`. The write validates the complete plan and convention, vector count, dimension and finite float32 values. It publishes UTF-8 SHA-256 input digests in the same transaction as chunks, vectors, keyword rows and file state. Palace records the supplied-input claim; it cannot establish what an external model actually received. Changing descriptor, provider, model or dimension requires an explicit rebuild of incompatible derived state. Never stamp a new identity over old vectors. The ordinary default full-build bypass is unavailable for supplied-input commits; initialize a genuinely empty index with the intended selector before composing its rows.

`read_file_vectors(conn=..., watch_root=..., path=..., expected_identity=..., max_chunks=16384, max_bytes=67108864)` reads exactly one normalized watch-root-relative POSIX path. It returns `None` for an absent file without orphan rows, otherwise a frozen `FileVectorSnapshot(file, identity, chunks, view_sha256)`. Each `CommittedChunkVector` contains its `ChunkRecord`, a tuple of float32 values and `input_sha256`. Default input digests include the full path/heading breadcrumb. Explicit input digests are the evidence committed with each vector. Missing, inconsistent or corrupt evidence refuses. The snapshot validates the file manifest, kind-aware chunk identities, bodies and vectors; the view digest binds the actual observed file, ordered rows, inputs, vectors and complete identity. It is not a whole-store checkpoint or a reservation.

The reader requires an idle connection with no active transaction. Hold one same-store `writer_lock` **before opening that connection and until after donor read, planning, embedding, publication and connection close**, on one writer thread. Neither the reader nor `commit_plan` takes the lock. Do not invoke a second lock-taking writer in that interval. If the lock is released, reacquire it and reread/revalidate before reuse. The reader does not commit or roll back caller state. Limits must be positive integers (booleans are refused). The row ceiling remains 16,384; callers may lower it. The byte default remains 67,108,864 (64 MiB), but callers may explicitly choose a larger finite byte budget. No unlimited sentinel is supported. They bound row count and encoded database payload bytes before payload retrieval, not Python object overhead, RSS or on-disk database size; excess refuses rather than truncates.

For prepublication capacity admission, pass the frozen public `FileVectorLimits(max_bytes=67108864, max_chunks=16384)` as `commit_plan(..., snapshot_limits=limits)`. This opt-in requires a full, non-noop Markdown/code/text plan and an explicit matching identity; incremental, deletion, JSONL and identity-bypass plans refuse before mutation. It works for both default breadcrumb and explicitly supplied inputs. The existing transaction measures the staged file with the reader's exact SQL accounting before COMMIT; excess rolls back the file, chunks, vectors, keyword rows, mapping and input evidence together. Budget refusal uses the public `IndexError`; callers must not parse its message as a stable outcome protocol. Rollback or I/O failure can leave the outcome uncertain and requires reconciliation before retrying; the budget declaration cannot guarantee successful rollback. Omitting `snapshot_limits` preserves ordinary commit behavior. This is admission after embedding/staging, not a pre-embedding memory estimate, and the resource declaration is not part of semantic identity.

Use the same limits for every mandatory donor, destination and retry read. After commit, call `read_file_vectors(..., max_bytes=limits.max_bytes, max_chunks=limits.max_chunks)` under the uninterrupted lock before reporting acceptance. Admission proves encoded capacity, not future I/O or integrity success: a read failure after COMMIT leaves a committed-but-unaccepted result, never a fictitious rollback. Large snapshots may occupy much more memory than their encoded size; callers separately bound conversion, rendering, checkpoint and process resources.

The caller selects an eligible donor, compares complete identity and exact ordered body-input digests, freshly embeds every destination filename input even when names match, and supplies the combined vectors in destination plan order. Palace does not identify body/name rows or authorize donors. Equal body bytes alone are insufficient when model inputs contain a destination-dependent prefix. Reuse copies vector values into independent destination rows; donor deletion leaves those rows searchable. `copy_paths` instead preserves unchanged IDs between distinct stores, and is not the same-store composition API.

```python
from contextlib import closing
from palace.index import (
    FileVectorLimits, commit_plan, plan_one, read_file_vectors,
    with_embedding_inputs, writer_lock,
)

# `open_initialized_connection`, the mapping and eligibility checks belong to
# the caller. The connection loads sqlite-vec and uses the configured identity.
limits = FileVectorLimits()  # Choose a larger finite byte budget explicitly if required.
with writer_lock(store, operation="compose file"):
    with closing(open_initialized_connection(store)) as conn:
        donor = read_file_vectors(
            conn=conn, watch_root=root, path=donor_path, expected_identity=identity,
            max_bytes=limits.max_bytes, max_chunks=limits.max_chunks,
        )
        plan = plan_one(
            conn=conn, file_kind="markdown", change_kind="created",
            path=destination, watch_root=root, full=True, log=log,
        )
        plan = with_embedding_inputs(plan, representation=descriptor, inputs=map_inputs(plan))
        vectors = compose_verified_body_and_fresh_name_vectors(donor, plan, identity)
        commit_plan(
            conn=conn, plan=plan, embeddings=vectors, identity=identity, log=log,
            snapshot_limits=limits,
        )
        accepted = read_file_vectors(
            conn=conn, watch_root=root, path=plan.stored, expected_identity=identity,
            max_bytes=limits.max_bytes, max_chunks=limits.max_chunks,
        )
        if accepted is None:
            raise RuntimeError("Committed file is absent; acceptance failed")
