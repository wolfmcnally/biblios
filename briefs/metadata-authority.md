---
title: "Metadata authority: Castle owns current attributes, mirrors own content"
date: 2026-11-13
status: approved
as_of: 2026-11-13
scope: "Which store is the authority for each kind of entry metadata, how mirror headers are treated after Castle admits an entry, and how renames and edits reach the search index."
---

# Metadata authority

As of 2026-11-13. The operator approved this direction in a conversation relayed by the factotum between 01:29 and 01:54 UTC on 2026-11-13. The decisive words: "Castle namespace is the single authority for the current filename"; "the only thing that isn't directly mutable in Castle should be the original bytes"; "make searchable useful mirror-header fields not controlled by Castle, as attributed metadata, not silently promoted to authority"; and "MVP all the way". This brief records the design and the minimum that implements it.

## Why

A consuming service found that a rename left the old filename readable inside the entry's mirror, so cleaning up a spilled name could never complete. The cause was general, not a single bug. The filename lived in four places: the Castle namespace, a copy in Biblios's entry metadata (`display_name`, set at ingestion and never updated), the converted text (Drawbridge's H1 and its `mirror_of`/`acquired_from` header fields, all taken from the name Biblios passed in), and the index. Every copy but the first could go stale.

## The rule

Three kinds of data, each with one home.

1. **Original content.** The source bytes, addressed by content hash, are the only immutable thing in Castle. Facts true of those bytes forever (content hash, size) may be recorded anywhere as provenance.
2. **Current attributes are Castle's.** Name, path, kind, media type, tags and dates live in Castle and change only through curation. They are never copied into a mirror, into converted text or into content embeddings, so there is no second copy to keep current. Biblios's `display_name`, set at ingestion and never updated, is such a copy and is to be removed; its removal waits on the decision under Unresolved, and nothing reads it meanwhile.
3. **Mirrors are content.** A mirror is the user's once it exists and is never rewritten by Biblios. Its body is indexed as content. Its front-matter header is not an authority for anything Castle controls: a header value under a Castle-controlled key is ignored, never applied and never indexed.

## Mirror headers: attributed metadata

Header fields that Castle does not control and that are not converter provenance are preserved untouched in the mirror and made searchable as attributed metadata: the index adds them, after the mirror's content, as a section headed `Mirror metadata`, one `key: value` line per field. They are labelled as coming from the mirror, never promoted to Castle values and never allowed to override one.

- **Which fields.** Every top-level header key (a letter, then letters, digits, `_`, `.` or `-`) whose value is a string, number, boolean, date or list of those, except the Castle-controlled keys (`name`, `filename`, `path`, `media_type`, `tags`, `dates`, `content_hash`, `content_address`, `cas_id`, `byte_size`, `size`) and the keys the pipeline's policy declares as converter provenance, both compared case-insensitively. The shelf's default Drawbridge policy declares every header key the pinned Drawbridge release writes, its required set and its per-format fields for PDFs, slides, email and audio, so a freshly converted mirror contributes no attributed fields; a field a user adds, such as `author`, does. A Drawbridge release that writes a new key must be added to that declaration.
- **Bad headers.** A header that is not valid YAML or not a mapping contributes nothing and never isolates the entry. Palace v0.5.1 treats a header it cannot load (such as an impossible date like `2026-02-30`) as malformed front matter in the same way, so the body still indexes.
- **Recognition.** Front matter is recognised exactly as Palace recognises it: a leading `---` block with LF line ends. A CRLF header is indexed by Palace as body text, Castle-controlled keys included; that is Palace's behaviour and is unchanged here.
- **Edits.** A header edit changes the mirror's bytes, so it re-indexes like any content edit, with the same lag. Only the sections whose text changed are embedded again.

## Filenames in the index

The index adds the entry's current Castle filename as the final section, headed `Filename`, after the mirror's bytes and any attributed metadata. A rename changes only that section: the mirror is not touched and no content chunk is embedded again, because Palace identifies content chunks by the mirror's path (the entry id) and their text, never by the filename.

Mirrors that Biblios generates carry no filename or path. The default converter stages the source under a neutral name and passes a neutral title (`Document`), so Drawbridge's H1, `mirror_of` and `acquired_from` carry no name. A renderer supplied by a consumer receives only the conversion result, never a name.

## What does not change

- **Palace.** No change. Palace already keeps front matter out of keyword search and embeddings, and its explicit document-metadata contract, used by a consumer that pushes metadata from its own records, is untouched.
- **Castle.** No change.
- **User-edited mirrors.** A name or a Castle-controlled field that a user typed into a mirror stays where it is. In the body it is indexed as content; in the header it is ignored.
- **Earlier-release imports.** A shelf imported from an earlier release keeps its committed rows exactly until the entry is next indexed.
- **Mirrors written before 0.4.0.** A mirror an earlier release generated carries the filename it was converted under. When such an entry is renamed and its mirror is still exactly the converted output Biblios recorded (not a user edit), and no conversion of its source exists under the current policy, Biblios converts it again from Castle's original bytes under the current name-free policy. The old mirror is exchanged out, digest-bound so a concurrent edit wins, and kept as a retained version outside the index; excision removes it. An edited earlier-release mirror is never converted over: its old name stays as content. An entry never renamed keeps its mirror.

## Consumer formats checked

As of 2026-11-13, read-only against two existing book corpora. A legal-domain corpus's search feed is body-only text files, with tags, year and document number pushed to Palace's document-metadata filters from the consumer's own records; its headers never reach the index, so this design changes nothing for it. A pattern-book corpus's mirrors carry `slug`, `type`, `summary`, creation and update dates, all of which Biblios makes searchable as attributed metadata, and a nested `related` mapping, which it does not (only scalars and lists of scalars are attributed). Replacing either consumer's indexing with Biblios without re-deriving mirrors is a separate integration: its tags would need to become Castle attributes and Palace filters, and its body projections and file naming differ.

## Interface changes for consumers

- The renderer is `renderer(result) -> MirrorDocument(markdown)`: no name argument, no `name_heading`.
- `ConversionRequest` carries no display name.
- `DerivationPolicy.required_header_fields` is gone; `DerivationPolicy.provenance_header_fields` declares the header keys that are converter provenance.
- The isolation classes `mirror-header-invalid` and `mirror-name-section-invalid` are gone.

## Unresolved


- **Existing shelves and `display_name`.** Entries written by 0.3.x carry `display_name` in Castle. Without a compatibility read they are refused once it is removed; whether to accept that, defer the removal or provide a one-time rewrite is the operator's decision.
- **Drawbridge's H1.** Drawbridge always writes an H1; Biblios passes a fixed neutral title. An option in Drawbridge to omit the H1 would remove even that line, and would be a request to Drawbridge's owner.
