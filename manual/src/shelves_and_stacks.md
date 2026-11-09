# Shelves and Stacks

## The shelf

A shelf is one directory with three strictly ordered layers. Each layer is derived from the one before it and can be rebuilt from it.

```text
<shelf>/
  shelf.json     manifest: schema, shelf id, name, description, generation, mode
  castle/        exact bytes of every file, content addressed; the journal is the truth
  namespace/     the shelf's file tree; each entry has a stable id, a path, bytes and metadata
  namespace.lock Castle's domain lock for the namespace
  mirror/        one Markdown mirror per file entry, derived
  palace/        the search index, derived
```

The authority is the bytes plus the namespace. Every file has two names: a stable entry id that survives replacement, rename and move, and a path that is resolved at the moment a hit or listing is answered. Derived artifacts are keyed by entry id, so renaming or moving a file never re-embeds anything.

Files a converter cannot handle are marked unconverted and never searched. Only mirror text reaches the index; original bytes never do.

## Curation

The user curates the shelf. Biblios makes no admission decisions. Files are created, replaced, renamed, moved, copied and removed through the namespace, and each operation is atomic; a retry carrying the same operation id and the same request returns the original result. Adding bytes the shelf already holds is reported as a duplicate, and the user decides whether a second entry is wanted. Removing a file retires it from the tree at once; its mirror and its chunks are derived state and leave at the next reconcile, with no embedding call, and until then a search refuses its hits; the bytes stay. A separate, explicit excision retires bytes for good.

## The index lags

A shelf created from scratch has an empty index. Every curation call returns as soon as the namespace has committed it, and the entry's bytes and the listing are readable at once; the mirror and the index follow. `reconcile` is the step that brings them up: it converts and indexes one entry at a time, smallest first, so short files become searchable before long ones, and each entry's index update is its own transaction. The step takes two optional callbacks. `progress` receives an `EntryProgress` row at every stage change (`stored`, `converting`, `indexing`, `searchable`, `unconverted`) and after every slice of sixteen chunks embedded, with the chunks done and the total for that one entry and an estimate of the time left; `should_yield` is read between entries and between slices, and when it answers true the step lets the current entry finish and returns with `yielded` set, so a newer change can be taken up without re-indexing anything already done. A step interrupted by a failure leaves the same state: finished entries stay indexed, and the next step picks up only what is missing.

`index_status` says where a shelf stands without running anything: the namespace revision (the authority), the revision the index last matched in full (the watermark, kept in `palace/watermark.json`), whether the shelf is lagging (the watermark behind the revision, or any entry pending), whether its store is built, the counts of files, searchable, pending and unconverted entries, and one row per entry that is not yet searchable. Every row carries the shelf id, so an application holding many shelves can show one table and let the user pick a shelf to watch. The watermark advances only after a step that completes with the index matching every live converted entry and no other writer having changed the tree meanwhile; a rename advances the revision without index work, so the shelf reads as lagging until the next step, which costs nothing, rewrites it.

`search` never waits for the index. It answers from what the index holds, and the result says `indexing` when the shelf is behind; a shelf whose store is not built yet answers no hits rather than an error. A hit is answered only for an entry that is live, whose conversion is current, and whose mirror the index holds at that very digest, so a replacement whose new chunks are not yet committed never answers with the old ones. The library ships no thread or scheduler: the application drives `reconcile` from its own worker, as the stack demo does.

## Identity

Palace is the only record of a shelf's embedding identity: provider, model, dimension, convention and reranker. Biblios sets Palace's selector before the first build and runs Palace's own check of its built stamp against that selector after every build and before every seal. The manifest carries no copy. A build never silently falls back to a different embedder.

## Stacks

A stack is a set of shelves searched together, declared with `open_stack` over shelves the application has opened; it is held, never stored. Construction refuses an empty declaration, a shelf named twice, a shelf with no declared embedder selector, and shelves of different embedding identities, naming every member's identity and the odd ones out; it admits a shelf whose index lags or is not yet built. The search hands Palace exactly the built member stores and one query embedder for the stack's identity; Palace retrieves each store in its own space, fuses the ranks and reranks the union once. Every hit says which shelf it came from, which entry, its current path and its page or heading, and the result names every member whose index is still catching up, so coverage is never silently overstated; a chunk naming an entry that is not live and current in its shelf is refused and reported by shelf and entry. `resolve`, `read` and `download` take a shelf id and an entry id and reach the member shelf.

## Static and dynamic

A shelf's index is either a sealed static artifact opened read-only, or a live object that accepts updates. The same shelf can move between the two. A sealed shelf is a directory and travels as one.

`seal` makes a shelf static. It refuses, before writing anything, a shelf whose index is not current: a read-only artifact can never catch up, so a seal over a lagging index would ship a search that is permanently incomplete; run `reconcile` first. It also refuses a shelf whose store has never been built or whose embedder stamp disagrees with the declared selector. A seal takes a Castle snapshot of the namespace, puts the Palace store on SQLite's rollback journal so no `-wal` or `-shm` file exists or will be created by a reader, marks the manifest sealed with the next generation, and publishes `seal.json`: the snapshot record, the sealed revision and the SHA-256 digests of the manifest, `chunks.sqlite`, the embedder selector and the index watermark. The whole transition is one critical section under the shelf's handle lock and its step lock, so no curation call can slip into it, a seal attempted while a reconcile step is running is refused, and a step admitted just before the seal reads the sealed mode under that lock and refuses before it writes anything. Once the manifest is published the open shelf is sealed too, even if publishing the record then fails.

A sealed shelf opens read-only through Castle's read-only namespace handle, and Biblios refuses every curation call, every retry and `reconcile` before Castle does; `open_shelf` reads the mode again once it holds Castle's lock, so an opener racing a seal or an unseal never returns a handle of the wrong kind. A corrupted index or a malformed record is reported in the verification report, never raised through it. `search`, `read`, `download`, `index_status`, `verify` and stacks work unchanged. Copy the directory anywhere: the copy opens the same way, `verify_seal` proves its recorded digests, its snapshot and its sealed revision, and it answers searches from `chunks.sqlite` with paths resolved from the namespace and bytes served from Castle. `unseal` returns a shelf to dynamic and restores the write-ahead log; a copy unsealed at a new location is rebuilt by its first `reconcile`, because Palace keys files by the absolute mirror root they were indexed under. A seal that fails before the manifest is published leaves a dynamic shelf; one that fails after it leaves a sealed manifest without a record, which `open_shelf` refuses and `unseal` repairs.

```admonish info title="No security of its own"
A shelf has no encryption, authentication, authorization, tenancy or audit. Anything that must be protected is protected by the application around the shelf.
```
