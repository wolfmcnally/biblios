---
title: "Biblios"
date: 2026-11-06
status: draft
scope: "Entry point: thesis, operator requirements, the shelf design, consumers, proposals and unresolved decisions."
---

# Biblios

*Standalone, portable knowledge bases ("shelves") that can be searched alone or federated in a stack, built from Drawbridge, Castle and Palace.*

## Thesis

Three projects already cover the hard parts of a knowledge base. Drawbridge turns any file into a page-anchored Markdown mirror with a provenance header. Castle keeps the exact source bytes content-addressed, with an append-only journal, and since 0.2.0 gives them a coordinated namespace: a durable tree of named files and folders with atomic create, replace, rename, move, duplicate and logical delete, frozen folder import, snapshots and read-only opening. Palace chunks the mirrors, embeds them under a pinned identity, and searches any number of stores at once with hybrid retrieval and a single rerank over the union. What is missing is the unit that ties them together: a self-describing directory that one user can add documents to, update and remove them from, carry to another machine, ship to a deployment as a static artifact, and search either by itself or together with others.

Biblios defines that unit and calls it a **shelf**. A set of shelves searched together is a **stack**. Shelves are horizontal: every shelf is a peer, and a search names its stack and federates the results with one rerank. A shelf has no encryption and no security of any kind. Security is a consumer's business, and the first such consumer is the secured adapter-reached consumer, whose label nodes each need a number of shelves and whose searches select and federate the shelves of the nodes unlocked under the current authorization. Biblios manages shelves; the surrounding application and its security substrate manage security.

The same unit is useful far from the secured adapter-reached consumer, in the way Open WebUI's knowledge bases are useful: a document collection a chat application can retrieve from. The short-term target outside the secured adapter-reached consumer is the connector-consuming chat application, an application that attaches external services through MCP connectors and has no retrieval of its own beyond substring search. The intent there is a large improvement over what Open WebUI offers.

Two prior implementations prove the pipeline and are the main sources for this design. The secured adapter-reached consumer's node volume already holds a Castle store, a sealed catalog, a mirror directory and a Palace store, and its index module already reconciles mirrors into Palace and runs a union search over a closure of stores. The static publishing pipeline's source-of-truth workflow for source documents runs received bytes through Castle intake and corpus stores, Drawbridge mirrors, and a Palace index, then ships that index read-only to a serverless MCP server. Biblios is the generalization of those two, extracted into a package neither of them owns.

## Operator requirements

These are stated requirements, not proposals. Changing one is the operator's decision.

1. **The shelf is the unit.** A knowledge base is a shelf. It stands alone, is complete in one directory, and can be used by itself.
2. **The user curates the shelf.** The user adds documents to a shelf, updates them and removes them. Biblios makes no admission decisions.
3. **Drawbridge, Castle and Palace do the work.** Drawbridge converts, Castle stores, Palace indexes and searches. Biblios composes them and invents as little as possible. Vellric is reached only through Drawbridge, which keeps its AGPL terms out of Biblios.
4. **A shelf is a file system.** The files in a shelf live in Castle's namespace and have a path from the shelf root, every component named. The pseudo-file system supports every typical file-system manipulation except live opening with partial writes, and each operation is atomic. (Operator, 2026-11-06.)
5. **No security.** A shelf has no encryption, authentication, authorization, tenancy or audit of its own. Anything that must be protected is protected by the consumer around the shelf.
6. **Federated search in a stack.** A search names a stack of shelves. Results are fused across them and reranked together, once, and every hit says which shelf it came from. All shelves in a stack share one embedding identity; a mismatch is refused, because shelves are meant to be integrated into single applications where the embedder is the same across the application. (Operator, 2026-11-06.)
7. **Static or dynamic.** A shelf's index is either a sealed static artifact that ships to a deployment and is opened read-only, or a live object that accepts updates. The same shelf can move between the two.
8. **Four deployment shapes.** Packaging must serve, without contortion: a very simple single-shelf dynamic deployment; a single-shelf static deployment of the kind the static publishing pipeline ships; a multi-shelf dynamic deployment of the kind the connector-consuming chat application needs; and a multi-shelf dynamic deployment where the shelves in the query stack are reached through adapters, which is the secured adapter-reached consumer. (Operator, 2026-11-06.)
9. **Adapter-reached applications are consumers.** Each node in a secured adapter-reached application holds a number of shelves; a search in that application selects and federates the shelves of the nodes unlocked under the current authorization. Biblios must serve that without learning anything about labels, clearances or keys. The secured adapter-reached consumer protects a shelf by wrapping it; the shelf's own records stay in the clear. (Operator, 2026-11-06.)
10. **Usable elsewhere.** Biblios serves other applications the way Open WebUI's knowledge bases serve its chat. The short-term target is integration with the connector-consuming chat application.
11. **License.** MIT. (Operator, 2026-11-06.)
12. **Configuration through Conric.** Drawbridge is a dependency and its model stages are configured through Conric, so Biblios is configured through Conric too: one composed configuration in which Biblios validates its own section and the sections of Drawbridge, Vellric and any other Conric-based member of the dependency closure travel to them unchanged, so they inherit the settings. Conric is a direct dependency. (Operator, 2026-11-06.)
13. **Federation stays in Palace.** Cross-store fusion and the single rerank over the union are Palace's job, through its multi-store search. Biblios never reranks or fuses; a capability the stack needs and Palace lacks is requested of Palace as a generic multi-store requirement. (Operator, 2026-11-06.)
14. **The index lags.** A shelf created from scratch has an empty index. Every file operation commits in the namespace at once, and the entry's bytes and the listing are readable at once; mirrors and index catch up asynchronously, one entry at a time, resumable and preemptible, and the library reports per shelf and per entry what is stored, what is indexing and how far along it is, with the shelf id on every row so a user watching several shelves can select one. A search answers from the index as it stands and names every shelf still catching up. (Operator, 2026-11-06, relayed by the factotum: "when a shelf is created from scratch it has an empty index. If it's suddenly loaded with a lot of files, or experiences large changes the index has to catch up and must do so asynchronously. The file contents must be available right after the change. The index may lag.")

## The shelf

### Layout

A shelf is one directory. Its three layers are strictly ordered: each is derived from the one before it and can be rebuilt from it.

```
<shelf>/
  shelf.json           manifest: schema, shelf id, name, description, generation, mode (no embedder identity: Palace owns it)
  castle/              Castle store: every file's exact bytes, content addressed; journal.jsonl is the truth
  namespace/           Castle namespace: the shelf's file tree; each entry has a stable id, a path, bytes and metadata
  mirror/<entry>.md    the Drawbridge mirror Palace indexes, one per file entry (derived)
  palace/              the Palace store: index/chunks.sqlite, meta/ (derived)
```

- **Authority** is `castle/` plus `namespace/`. Castle holds bytes; the namespace says which bytes are members of the shelf, where they sit in the tree, and what Biblios knows about each. A namespace entry carries a stable entry id, a kind, a parent, a name, a content address, a size and a metadata map. Biblios keeps its catalog facts in that metadata: media type, display name, added time, the converter and its version, the mirror's digest, conversion status, and user tags and dates. There is no separate catalog file.
- **Two names for every file.** The entry id is the stable reference: it survives replacement, rename and move, and it is what mirrors, chunks and citations are keyed by. The path is the human name and is resolved from the namespace at the moment a hit or listing is answered. Keying derived artifacts by entry id means a rename or move never re-embeds anything.
- **Mirrors** are derived. A file of a type Drawbridge cannot convert is marked unconverted in its metadata and is never searchable, exactly as in the secured adapter-reached consumer. Drawbridge's optional structure, image-description and audio stages are out of the default path; a shelf runs the fidelity conversion only unless a profile is supplied.
- **The index** is derived. Only mirror bodies reach Palace. Original bytes never do.

This is the secured adapter-reached consumer's node layout with the sealing removed and the catalog replaced by Castle's namespace, and it is the static publishing pipeline's custody store reduced to one Castle member. A consumer that needs sealed records, an inventory root or an encrypted volume wraps the shelf; Biblios does not offer a per-record sealing hook.

### Operations

The library is the API; the CLI and the MCP server are thin over it. File operations are Castle namespace operations and inherit its atomicity: each request has one effect, an identical retry returns the original result, and there is no live opening with partial writes.

- `init`: create a shelf, initialize its namespace, and set Palace's embedder selector for its store. The manifest records nothing about the embedder.
- `add PATH FILE…`, `mkdir PATH`: create entries. Bytes enter Castle once; a file whose content is already in the shelf is reported, and the user decides whether a second entry pointing at the same bytes is wanted.
- `update PATH FILE`: replace an entry's content under the same entry id, name and metadata.
- `rename`, `move`, `copy`, `remove PATH`: the namespace's own operations. `remove` is logical deletion: the entry leaves the tree and its chunks leave the index in place with no embedding call; the bytes stay in Castle.
- `excise`: a separate, explicit operation that moves Castle and its namespace to a successor epoch, retiring selected bytes and metadata. It is never implied by `remove`. (Operator, 2026-11-06.)
- **Purpose of the two removals (operator, 2026-11-12, relayed by the factotum).** In the operator's words: "The main purpose of delete is normal reorganization while still maintaining grow-only a[n] paper trail guarantees. The main use case for excise is cleaning up classified information spills." Both make the removed document unsearchable; their implementations may differ. `remove` therefore keeps the grow-only history and the original bytes in Castle while its hits are refused at once and its index rows leave at the next reconcile. `excise` must leave none of the selected document's text in any store Biblios manages, including the keyword index's internal segments, free pages and write-ahead logs. It does not reach copies outside those stores: backups, synchronized or external copies, text already sent to an embedding or rerank provider, operating-system temporary files, and blocks a file system or storage device keeps after a file is rewritten or unlinked. Biblios does not claim physical erasure of the medium. Delete is not reversible and is not a goal to make so: in the operator's words, "I haven’t made it a goal to make delete reversible at this time. It’s highly complex and would probably require us to implement full journaling." There is no built-in undo or journaling; the grow-only paper trail and content retention are the whole guarantee. "The normal course of action for un-delete will be restore from backup. Of course that could also restore excised records, but that’s not our problem at this time." (Operator, 2026-11-12, relayed by the factotum.) Backups are therefore not sanitized by excise, and propagating an excision into backups is not a current requirement.
- `ls`, `stat`, `tags`: the tree and an entry's metadata.
- `reconcile`: bring mirrors and index into agreement with the namespace: drop what no live entry names, convert and index what is missing or at another digest, one entry at a time and smallest first, through Palace's per-file update; recover a missing store or interrupted full build through Palace build. The step reports progress per entry, yields to a newer change at an embedding-slice boundary, and advances a shelf-wide watermark only after a complete match, so `index_status` can say how far the index is behind the namespace. This is the secured adapter-reached consumer's `NodeIndex.reconcile`, extracted, with its progress and preemption.
- `verify`: Castle journal verification, namespace-to-mirror digests, Palace's own per-file hashes against the live mirrors, and Palace's own check of its built stamp against its declared selector.
- `seal`: make the shelf static. Take a namespace snapshot, checkpoint and truncate the index WAL so there are no sidecars, record the digest of `chunks.sqlite`, the snapshot manifest and the shelf manifest, and mark the generation. A sealed shelf opens read-only (Castle's read-only namespace handle and SQLite `mode=ro`) and refuses every write.
- `unseal`: return to dynamic.
- `search QUERY --shelf A --shelf B …`: see Stacks.
- `resolve SHELF ENTRY`, `read SHELF ENTRY [--page N]`, `download SHELF ENTRY`: a hit's citation back to its current path, its mirror page and its exact bytes.

### Identity

Palace is the single source of truth for a shelf's embedding identity: provider, model, dimension, convention, and the reranker. Palace holds it twice by design, as the declared selector in the store's `meta/` set before the first build and as the stamp it writes into `index_meta` at build time, and it refuses to build or query across a mismatch between them. Biblios records the identity nowhere else. The manifest carries no copy, because a second record can only agree with Palace's or be wrong, and a reader that trusted it would have two sources of truth. (Operator, 2026-11-06.)

What Biblios adds is discipline around Palace's record: `init` sets the selector before anything is built, because a store with no selector silently falls back to a local default; `verify` and `seal` run Palace's own stamp check, because Palace's read path accepts a foreign provider without error and a wrong embedder only ranks worse; a keyword-only fallback never overwrites a working hybrid store; and a stack reads every member's identity from its Palace store at construction. The first three are the static publishing pipeline lessons. The secured adapter-reached consumer's sealed watermark keeps its own copy of the identity, but that is the secured adapter-reached consumer's integrity witness over an encrypted volume, not a second record Biblios consults.

### Stacks

A search names its stack. Biblios hands Palace exactly those stores and the identity's providers through Palace's multi-store search, which already retrieves each store in its own space, fuses the ranks, and reranks the candidate union once; it never reranks per store, and Biblios does no fusion or reranking of its own. What Palace's multi-store search lacks today is the metadata filtering its single-store search accepts, so a filtered stack search is an enhancement Biblios needs from Palace, not something Biblios builds around it. (Operator, 2026-11-06.) A hit carries the shelf id, the entry id and its current path, the mirror heading (a page for PDFs, a sheet, a message, a turn), the chunk, the scores and the rerank status (`applied`, `disabled`, `failed`). A hit whose chunk names no live entry is refused, never returned.

Every shelf in a stack shares the stack's identity, read from each shelf's Palace store. A stack naming a shelf of another identity is refused at construction, before any query runs. A member whose index lags or is not yet built is admitted: the stack searches what is built and names every member still catching up, so coverage is never silently overstated. (Operator, 2026-11-06.) A stack is a declaration a consumer holds, not a stored object: the secured adapter-reached consumer's is the closure of the session, the connector-consuming chat application's is the user's shelves.

### Static shipping

A sealed shelf is a directory and can travel as one: copied, tarred, placed in a container image, exported through Castle's snapshot export, or published block-by-block to object storage with a pointer moved last, which is how a static publishing pipeline ships. The receiving process opens `chunks.sqlite` read-only with sqlite-vec loaded and never needs Castle to answer a search; it needs Castle and the namespace only to resolve a path and serve exact bytes. The manifest's digests let the receiver prove it opened the artifact that was sealed.

## Access planes

The Python library is the contract and the only interface for development and testing. It is the deliverable of the first phases, and the CLI and MCP server arrive later as thin layers over it that add no logic. (Operator, 2026-11-06.)

- **Library.** The public API is designed as a contract from the start, with typed requests and results in Castle's manner, not internal functions a later plane exposes by accident. Every operation in this brief is a library call; every proof is an in-process assertion over a temporary shelf. The substrates are library-first already, and the secured adapter-reached consumer's index and sources modules call them as libraries, so the secured adapter-reached consumer's adoption becomes a swap of imports rather than a subprocess seam. The static publishing pipeline drives Palace as a subprocess and had to reimplement the embedder contract and parse the schema by hand; Biblios does not repeat that.
- **CLI.** `biblios`, one command per library operation, `--json` on every read. Added once the library's contract has held through the stack and seal phases.
- **MCP server.** Streamable HTTP, stateless, with read-only tools (`search`, `resolve`, `read`, `download`, `ls`, `shelves`) annotated read-only and non-destructive, and curation tools (`add`, `update`, `remove`, `rename`, `move`, `mkdir`) that a deployment may leave disabled. This is the plane every target consumes: the secured adapter-reached consumer's dashboard is already an MCP client, the connector-consuming chat application attaches custom connectors over streamable HTTP only and treats every unannotated tool as a write, and Open WebUI accepts MCP tool servers beside OpenAPI ones. Its tool list is the forcing function for the library's verbs, so it is designed with the library and built after it.

A library commits to nothing about processes or transports, so building it first does not prejudge how the planes are packaged across the four deployment shapes; see Unresolved.

## Consumers

**Secured adapter-reached application.** A node's volume becomes one or more shelves plus the secured adapter-reached consumer's own layers: the encrypted volume, a sealed inventory root over the shelf's namespace and journal, the release authority and the worker lease. The shelf's own records stay in the clear inside the volume. The secured adapter-reached consumer's `index.py`, `sources.py` and the conversion container are the code Biblios absorbs; the secured adapter-reached consumer keeps everything that touches a key. A search in that application is a Biblios stack over the shelves of the closure's nodes, reached through the secured adapter-reached consumer's adapters and run by the secured adapter-reached consumer's retrieval worker with the secured adapter-reached consumer's providers. Biblios must not need to know that a shelf is inside a node.

**Connector-consuming chat application.** The chat application stores extracted text per document version and searches it with case-insensitive substring matching (pg_trgm); it has no embeddings, chunks or reranker. Its connectors are streamable-HTTP MCP servers, per workspace, with bearer or OAuth auth, and tools are treated as writes unless annotated. The integration is therefore the Biblios MCP server added as a custom connector, with read tools annotated read-only, so the chat application's agent can call `search` across the user's stack and `read` a page. Feeding the chat application's documents into a shelf (the `document.extract_text` job and the `extracted-text/` cache are the hooks) is a second step and is not needed for the first.

**Open WebUI.** Its knowledge bases chunk and embed under one global setting, keep no provenance to source bytes, and cannot be pointed at an external retriever except through an experimental external vector store that still embeds the query with Open WebUI's own model. A Biblios shelf is the opposite shape: identity pinned per shelf, exact bytes and page anchors behind every hit. The fit is the same as for the chat application: Open WebUI calls the Biblios MCP server as a tool server. Pushing mirror text into an Open WebUI knowledge base through its files API is possible and lossy, and is not in scope.

**Static publishing pipeline.** Not a target. Its pipeline is evidence for this design and the source of its lessons; whether a book later adopts shelves is that project's decision.

## What Biblios is not

- Not a security boundary: no encryption, auth, tenancy, labels or audit.
- Not a chat application, a UI, or a model host.
- Not a converter or an OCR engine; Drawbridge and Vellric are.
- Not a retrieval research project: no legal-specialist embeddings, citation graphs or evaluation sets. Those stay with the consumers that need them.
- Not a curator: no screening, splitting, dedup across shelves, or admission policy. Exact-duplicate reporting within a shelf is the only judgment it makes.
- Not a cloud service: static shipping is a directory contract; publishing to object storage or S3 Vectors is an adapter a later phase may add.
- Not a consumer registry: this repository describes consumer capability classes to state the requirements, without naming private consuming projects. Code, tests, policies and plans describe capability classes, never projects.

## Proposals

Working suggestions, none a decision.

- **Lessons carried in as rules.** Pin the embedder before any build; verify the stamp before any seal; never let a fallback overwrite a hybrid store; checkpoint the WAL and refuse sidecars before a snapshot; refuse to start a local embedding service a shelf did not declare; parse Palace's schema as it is written (`FLOAT[4096]`, uppercase, across lines); a hit must resolve to a live entry; never bump an upstream pin while a run is live.
- **Reuse the secured adapter-reached consumer's code.** The consumer's reconciliation module (reconcile, watermark, union search) and the consumer's source module (catalog fields, unconverted handling, duplicate donor content) are the starting point. The secured adapter-reached consumer's phase that adopts Biblios replaces them with calls.
- **Castle's namespace as the only catalog.** Biblios defines a metadata schema for entries (`biblios.entry/1`) and validates it on read; it stores nothing about a file anywhere else. Snapshots are the seal; `import-folder` is the bulk-add path for a prepared tree.
- **The shape of the Conric composition.** A `biblios` section (embedder and reranker selection handed to Palace at `init`, conversion defaults, limits) that Biblios validates, beside `drawbridge` and `vellric` sections that travel unchanged, as the secured adapter-reached consumer's conversion module composes them today. The embedder selection in configuration is input to `init`; after `init`, Palace's store is the record.
- **Metadata filtering.** User tags and dates in entry metadata are mirrored into Palace's document-metadata tables, so a filtered search works on one shelf now and on a stack once Palace's multi-store search accepts the filters its single-store search already takes. That is the first requirement Biblios sends upstream.
- **A packaging investigation brief** before phase 1 settles, covering the four deployment shapes: what each installs, what each opens, where the MCP server runs, and whether one package with subcommands or a library plus a server package serves all four.
- **A phase order.** Phases 1 through 3 deliver the library only. (1) Shelf layout, manifest, namespace-backed add/update/remove/rename/move, reconcile, verify, single-shelf search. (2) Stacks and the identity rule. (3) Seal/unseal, snapshots and the read-only open. (4) CLI over the settled contract. (5) MCP server with read-only annotations. (6) the connector-consuming chat application connector walkthrough. (7) adapter-reached adoption, planned in the consumer's own repository. Audio through Quillric and the Open WebUI push exporter are candidates after that.

## Rulings

Recorded from the operator's answers of 2026-11-06, collected through a Fenric wizard over the then-open questions.

- A set of shelves searched together is a **stack**.
- Mixed embedding identities in a stack are **refused**; shelves are meant to be integrated into single applications with one embedder.
- A secured consumer **wraps the shelf**; Biblios keeps its records in the clear and offers no per-record sealing hook.
- Files in a shelf are **Castle namespace entries with paths**; the namespace is the shelf's file system, atomic per operation, with no live partial writes.
- `remove` retires; a separate explicit **`excise`** purges. Delete serves reorganization under a grow-only paper trail; excise serves cleanup of classified information spills (operator, 2026-11-12; see Operations).
- Packaging is **open**, to be investigated against the four deployment shapes in requirement 8.
- License is **MIT**.
- The **Python library is the basic interface** for development and testing; CLI and MCP server are thin and later.
- **Palace is the only record of a shelf's embedding identity.** The manifest carries no copy; two records would be two sources of truth.
- **Fusion and reranking across a stack stay in Palace**, through its multi-store search; filtered multi-store search is requested of Palace rather than built in Biblios.
- **Biblios is configured through Conric**, so Drawbridge and the rest of the Conric-based closure inherit its settings; Conric is a direct dependency.
- **The index lags and says so.** A shelf starts with an empty index; file bytes and the listing are available right after a change; the index catches up asynchronously with per-shelf, per-entry progress that supports shelf selection; a stack admits lagging members, searches what is built and names the members still catching up. (Operator, 2026-11-06, relayed by the factotum; the design follows a read-only inspection of a deployed multi-node consumer's indexing and progress surfaces.)
- **AWS qualification before the thin layers (operator, 2026-11-07, through two Fenric dialogs).** An inserted phase, `aws-ebs-qualification`, qualifies Biblios on its own on EC2 with the shelves on EBS under the account's established CDK practice, ephemeral per run, in the adapter-reached consumer's private network shape with a separately demonstrated low-security example; the EBS volume stays at the account default and Biblios encrypts nothing (the operator's earlier words "no encryption of courses" are recorded verbatim; asked, he could not place them, so nothing is derived from them); portability is proved by sealing to another directory on the same volume. Embedding through Bedrock comes as a **Palace `bedrock` provider sent upstream first**, selected from Biblios's Conric-composed `palace` section, in the operator's words: "All dependency stack's we own should be configurable with Conric, so the top level (in this case Biblios) should have a section of Palace configs that now can specify Bedrock+Cohere ZDR. That's the whole point of Conric: configure all the components once at the highest level." That provider is the second requirement Biblios sends upstream.

## Unresolved

- **Packaging and the four deployment shapes.** Whether one package with subcommands or a library plus a server package; what a static single-shelf deployment installs and what it leaves out; how an adapter-reached shelf (the secured adapter-reached consumer) presents itself to a stack. An investigation brief is proposed above.
- **Duplicate content within a shelf.** Whether `add` of bytes already present creates a second entry (the namespace allows it) or refuses unless asked. The brief proposes report, then let the user choose.
- **Mirror keying on export.** Derived artifacts are keyed by entry id; a snapshot export writes files under their paths. Whether the exported tree should also carry mirrors under paths, for readers without Biblios, is open.
- **Excision scope exposed to users.** Castle's excision selects content, entries, operations and metadata and requires explicit coverage of every dependency. How much of that surface `excise` exposes, versus a simpler "purge this entry and everything only it referenced", is open.

## Catalog

This is the entry-point brief. Research and design briefs that follow are indexed here as they are written.

- [Metadata authority](metadata-authority.md): Castle is the authority for current attributes and filenames, mirrors are content, and header fields Castle does not control are searchable as attributed metadata (operator, 2026-11-13).
