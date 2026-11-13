# Biblios 0.4.0 release preparation

Version 0.4.0 is the next experimental release at the existing [GitHub repository](https://github.com/wolfmcnally/biblios). It is a minor release after the published experimental 0.3.2 that makes Castle the only authority for an entry's name and attributes, as decided by the operator on 2026-11-13; it changes the consumer library interface, while dependencies, CLI commands and storage formats are unchanged. Publication follows artifact installation checks and the final exact-tree gate. This preparation record does not itself claim publication.

## Intended artifacts

The version tag is `v0.4.0`. The attached package artifacts are `biblios-0.4.0-py3-none-any.whl`, `biblios-0.4.0.tar.gz` and their SHA-256 checksums. No PyPI upload, hosted documentation, model weights, OCR executable, private configuration or user corpus is included. The wheel and source distribution carry the same declared dependencies. Published substrate releases are selected by tag; the lockfile retains their exact resolved commits. A tag alone does not establish that an upstream release was published.

Python support remains 3.12 and 3.13; the managed checkout runtime is Python 3.12. Git and uv fetch public prerequisites. macOS and Linux have development qualification; Windows is unqualified. See [installation](project/README.md) and [update/uninstall guidance](project/RELEASE.md).

## Changes since 0.3.2

Castle is the only authority for an entry's name, path and attributes; a mirror is content. Mirrors Biblios writes carry no filename or path: the default converter stages the source under a neutral name and title, and a supplied renderer receives no name. The index adds the current Castle filename after the mirror's bytes as its own final row, so a rename re-embeds only that row and never touches the mirror, closing the case where a rename left the old filename readable on disk. A mirror's front-matter header is never an authority: values under Castle-controlled keys are ignored, and other scalar fields that are not declared converter provenance are searchable as an attributed `Mirror metadata` row. Headers are no longer validated, so a malformed header never isolates an entry. No change was needed in Palace, Castle or Drawbridge. See [metadata authority](briefs/metadata-authority.md).

Dependencies: Palace v0.5.1 (from v0.5.0), which treats an unloadable front-matter header as malformed; PyYAML is declared directly.

Interface changes: `renderer(result) -> MirrorDocument(markdown)`; `ConversionRequest` without a display name; `DerivationPolicy.provenance_header_fields` in place of `required_header_fields`; the isolation classes `mirror-header-invalid` and `mirror-name-section-invalid` are gone. Biblios's own `display_name` entry field still exists, unread by indexing; its removal waits on an operator decision about existing shelves.

Mirrors written before 0.4.0: when an entry whose mirror is still exactly the output an earlier release recorded is renamed, Biblios converts it again from the original under the name-free policy and keeps the old mirror as a retained version outside the index; an edited mirror is never converted over and keeps its old name as content. A header Palace cannot load, such as one with an impossible date, now contributes nothing while the body still indexes; Biblios pins the published Palace v0.5.1 for this. A CRLF header is indexed by Palace as body text.

One independent code review (Fable) ran. Its findings were addressed before release with failing-first witnesses: Drawbridge's per-format header fields would have been indexed as attributed metadata; Castle-controlled keys matched only in lower case; Biblios recognised front matter differently from Palace; a filename beginning with `#` could never be indexed; date values and non-ASCII keys were dropped; and PyYAML is now a declared dependency. Stale documentation it found was corrected. The post-review changes were not re-reviewed. Two proofs under the owner's named witness exception changed content to the new behaviour without governance records, because the exception forbids modifying or retiring them; one still carries a name from the earlier limitation, explained in its docstring.

## Included from 0.3.2

A lost derived store can be rebuilt while the user's mirrors survive. Before, after the palace store was removed, every surviving mirror read as user-owned and no call could index it again without deleting or rewriting the user's file. `grant_first_ingestion` now takes an optional `expected_mirror_sha256`: the existing file is adopted as the entry's mirror only if its bytes hash to exactly that caller-vouched digest, and the next `bring_current` indexes those bytes without conversion or any write. A mismatched digest refuses `mirror-user-owned`, an absent file `mirror-missing`, and a mirror edited between the grant and indexing is isolated, never adopted. The grant stays bound to the entry's current content, the operation id and write-grade creation admission.

Adoption still runs the caller's write-grade `authorize_creation`, which sees the grant kind `adopt-existing` and may refuse it with the mirror untouched. Granting again with a new digest, after the user edits the file, replaces the vouched digest. Each adoption is consumed once its entry is accepted; a second grant for the same entry refuses `converted-before`. The rebuild proof checks that no mirror is renamed, retained or rewritten.

One independent code review ran on this correction. Its findings on re-vouching, the missing proofs of creation admission and grant consumption, and the strength of the no-write proof were addressed afterward with failing-first witnesses; two smaller findings, an unintended change to the ordinary grant fingerprint and an uncontained grant-time file read, were fixed without a dedicated witness. Those changes were not reviewed again. Two findings are documented rather than changed. An adopted mirror is treated wholly as user content, so the rebuilt index adds its own filename section and the renderer's original name section stays as body text; the rebuilt index is therefore not identical to the lost one. And excising an adopted entry's source keeps its mirror in place and reports it as kept, as for any user-edited mirror.

## Included from 0.3.1

Excision now leaves no selected entry id or content id (prefixed or bare sha256) in Biblios's own records. Recorded call results drop the excised entries; replaying such an operation returns `redacted=True`, and its operation id and request digest stay, so a changed request under that id is still refused. Curation and cancellation records that name the selection are removed, an unselected entry's record of a selected earlier source forgets that source, and kept retention records forget it too; the completed disposition marker keeps only the operation id, request digest, counts and the residue it reported. A final bounded byte scan of the palace and mirror roots is reported as `biblios.identifier-files` in the residue; it is expected to be zero, and a caller relying on the removal should check it. The release's proofs also scan the successor's Castle roots and find none.

A search is no longer refused `stale-view` because another entry was curated or indexed while the query embedded. Each returned hit is proven against its entry's current accepted record, so an entry renamed, replaced, deleted or newly indexed mid-query is judged exactly as a search issued after the change would judge it; only a change of the shelf's epoch, lineage, database generation or embedder and vector selectors refuses the answer. The adoption guide also drops two stale references (`pipeline.finish`, `pipeline.status`) and states that progress rows for conversion and per-entry outcomes carry no counts.

The persistent edit observer planned for this line is not part of 0.3.1; it remains in development.

## Included from 0.3.0


One indexing core now serves the library, CLI and bound entry points; the separate conversion and reconciliation modules are gone. Each entry is authorized, staged and promoted on its own, so a slow or failed document no longer holds back the others, a crash at any stage boundary never serves unauthenticated state or re-embeds, and replayed calls return their recorded result. Castle v0.4.0's touched-record admission keeps per-entry work flat as a shelf grows (counted at 20, 60 and 200 entries).

Mirrors belong to the user once created. Reconcile re-indexes an edited mirror without converting over it; replacing a file is a new ingestion that converts the new bytes and keeps the previous mirror under a retained name; a damaged mirror isolates only its entry. A copy receives one first mirror converted from its source, and the new `grant` command gives a never-converted entry its first mirror explicitly. `reconcile --entry` and `--sweep` bring edited mirrors current.

Search checks every hit through Palace v0.5.0's per-hit evidence: a hit whose stored body, input digest or vector differs from the entry's accepted record is withheld, and only that entry is affected. Delete stays logical: the entry leaves search at once, its index rows leave at the next reconcile and its bytes stay in Castle; delete is not reversible, and restoring from backup is the way back. Excise, meant for cleaning up information spills, now merges the full-text index's segments before vacuuming, so no excised word survives in any Biblios-managed index, journal or free page; it makes no claim about storage-layer copies, backups, sync, provider-side copies or operating-system temporary files.

Status, search and diagnosis read a shelf whose first reconcile is still creating its index store as a shelf with no store yet, instead of failing with a raw SQLite error. The packaged examples use the new result types.

A shelf written by 0.2.x is refused until its owner migrates its Castle namespace once, explicitly and with a backup; see [update guidance](project/RELEASE.md). Biblios never migrates it. Drawbridge v0.3.1 and Conric v0.1.0 are unchanged.

## Included from 0.2.2

Biblios required the published Palace v0.4.1, whose Bedrock embedding provider authenticates with a Bedrock API key from AWS_BEARER_TOKEN_BEDROCK when no SigV4 credentials resolve, keeps its mandatory account data-retention check before any embedding, and declares boto3 1.43.26 as its minimum. Real-key acceptance by the Bedrock service was not proved.

## Included from 0.2.1

Public reads and strict search now finish while a document embedding callback is held. Writer admission precedes namespace locking and the reader mutex is released during embedding; stale provider results still refuse publication, competing writers keep one admission order, and active work still protects the shelf lifetime. Conversion, rendering and input preparation remain synchronous. Waiting for writer admission observes cancellation and deadlines only after its existing bounded wait returns. A hard crash after mirror replacement but before index commit may require restoring the previous admitted mirror; no multi-store crash atomicity is claimed. Those 0.2.1 dependencies were the same published releases as 0.2.0.

## Included from 0.2.0

The library adds explicit consumer-controlled shelf binding and read-only use, revision-bound retry, injected conversion and rendering, same-shelf body-vector reuse with independent destination-name embeddings, strict search and publication observations, and coordinated finite retained-content/history/excision access. Destination rows remain independent after donor removal. Excision is an explicit library operation; the CLI still has 22 commands and logical removal retains source bytes. MCP remains held.

The optional Conric `biblios.max_file_bytes` setting is a positive integer and defaults to exactly 104857600 bytes (100 MiB). Consumer create, replace, import and copy use the configured limit when passed the composed settings. Aggregate import/copy admission is at least that per-file limit. Conversion body/mirror, chunk-count, checkpoint and vector-snapshot controls remain separate finite limits; source admission does not promise that every permitted upload fits every processing budget.

Explicit larger finite conversion envelopes and snapshot budgets are supported without changing their defaults, model dimensions, rendering or input identity. Durable checkpoint admission measures encoded UTF-8 JSON bytes against the existing 512 MiB ceiling. Retry preserves uncertain dispatch and committed-but-unaccepted distinctions; increasing a read-only resource budget does not silently change the original semantic request or redispatch conversion.

## Public baseline and provenance

The initial release established one canonical public history. This ordinary release continues that same history without replacement, parallel private development lineage or cleanup of required operational records. Completed development records remain historical evidence, not a claim that a later artifact was installed or that human usability was accepted.

The package source allowlist includes the library, tests, examples, metadata, lockfile, license and user instructions. The wheel contains the package, console entry point, metadata and license notices. Repository governance, private execution evidence and local configuration are excluded from both package artifacts. The generated GitHub repository source archives are separate from these curated package artifacts.

## Qualification and limits

Release qualification inspects the actual wheel/source archive, installs each into an isolated environment outside the checkout, verifies dependency sources and installed package origin, and exercises synthetic library/CLI behavior. The repository's changed-path selection and final bare full gate bind the final release source. Their exact results and artifact digests belong to the release receipt and GitHub release notes; historical development runs are preserved separately.

The configured per-file limit is tested with small synthetic fixtures. No 100 MiB stress test, full-RAG boundary run, memory benchmark or performance gate is part of this release. The known pinned mdBook/admonish version warning remains separate from successful generated-link, asset, anchor, search and print checks. The 0.2.1 and 0.2.2 guide edits were version, responsiveness and dependency wording only; the 0.3.0 guide edits describe user-owned mirrors, the `grant` command and the new reconcile options, and no new manual browser inspection is claimed for them. Human CLI/library usability judgments remain open.

Two assurance limits are disclosed for 0.3.0 and still apply. The 0.3.1 corrections each have a failing-first witness. Two independent code reviews ran. The first found that the redaction missed three record types; the second found that the generic fix hid part of the residue, missed grant and creation-intent records, read whole files into memory and could act on an unrelated row. The final keyed redesign, a bounded streaming detector and a schema-coverage proof address those findings and have their own witnesses, but they have not been independently reviewed again. One case remains untested: an interruption between the identifier scrub and the final marker write. Recovery repeats the scrub. The project suite passes on macOS; on the Linux lane five read-observation tests that search without a supplied reranker fail because Palace's default reranker model is not provisioned there and network egress is blocked. That limit of the lane predates this release; the tests and the search code they exercise are unchanged. The membership-proof representation and the changes made after the single independent code review this release cycle allowed (the Castle v0.4.0 and Palace v0.5.0 adoption, per-hit verification, spill-grade excision and two concurrency fixes) have had no independent code review; they are covered by the automated suite. And 39 membership proofs in the test suite were admitted without an observed failing run, under the owner's named exception; the repository's test-governance check lists them as unwitnessed until each is witnessed. Upgrading a 0.2.x shelf is proved on synthetic fixtures only; no real 0.2.x shelf was migrated during qualification.

Keep original documents. The API and on-disk formats may change before a stable release. Existing shelf data and configuration are not modified by installing or uninstalling the application. Biblios supplies no encryption, authentication or authorization; surrounding applications own those boundaries. No automatic legacy-shelf conversion or external provider call is implied by installation.

## Licensing and notices

Original work is Copyright © 2026 Wolf McNally under [MIT](LICENSE), also included in the standalone package. [Package notices](project/NOTICE) distinguish separately installed dependencies and optional tools. Vellric's separate AGPL terms are not replaced by Biblios's MIT declaration.

Repository-only third-party assets retain their notices: ECharts under Apache-2.0, included Font Awesome SVG icons under CC BY 4.0, mdBook reference/theme material under its included MPL-2.0 license, and admonish material under its included MIT license. The [reference catalog](docs/README.md) records provenance and limited quotation bases for other excerpts. These repository governance/documentation assets are excluded from the package artifacts.
