---
slug: abstract-foxhound
title: Try the CLI shelf workflow and judge terminal usability
status: pending
category: acceptance
urgency: low
blocks:
  - Human acceptance of the CLI; does not block gate-proved source delivery
filed: 2026-11-08
needed_at: After CLI delivery, when convenient
source: kickoff
refs:
  - ../RELEASE.md#public-baseline-and-provenance
  - manual/src/command_line.md
---

Pre-release CLI automated qualification was recorded; public-candidate qualification is separately reported in RELEASE.md; the hands-on demo and judgment of help, progress and errors remain yours. No MCP work or public release is part of this action. Use a fresh disposable shelf and a file you choose. Reconciliation uses the explicit configuration's provider; select one you intend to call and bear its ordinary costs.

### CLI test protocol

Surfaces introduced: the installed `biblios` command and `./bin/python -m biblios`; the Command-Line Tool manual chapter.

Hot-state checks (no shelf or provider needed):

```sh
./bin/python -m biblios --help
```

Expected: successful exit and the complete command list.

```sh
./bin/python -m biblios set-tags --help
```

Expected: the mutually exclusive date replacement and clear options.

```sh
./bin/python -m biblios search --help
```

Expected: shelf selection, query, limit, rerank and JSON options.

End-to-end: follow the Command-Line Tool chapter with a fresh disposable shelf, an explicit configuration and one chosen file. Initialization creates the shelf; adding stores its bytes; listing and status work before reconciliation. Repeating the add reports the duplicate unless explicitly allowed. Reconciliation calls the provider declared by that configuration and may incur its normal costs; perform it only with a provider you intend to use. Search, read and download should recover the file and its source. Seal only after the index is current, observe a refused add, then unseal. Compare downloaded bytes with the chosen original. Repeat search over two shelves to inspect shelf identity and lag reporting.

Manual acceptance: judge terminal help, errors and progress for clarity, and whether JSON redirection remains convenient. The automated checks prove behavior; they do not supply that judgment. No daemon or service is introduced. Conditional excision has no settled library contract and remains deferred; MCP work is on hold. The User Demo below remains unrun.

## Approved User Demo

User Demo: curate and inspect a shelf from the shell

Entry point:
  ./bin/python -m biblios --help

Suggested inputs:
- Follow the manual's Command-Line Tool chapter to initialize a fresh shelf with an explicit Conric configuration, add one of your files, list it and inspect status before reconciling.
- Add the same bytes again at another path: inspect the duplicate report, then choose whether to retry with the explicit duplicate opt-in.
- Reconcile with your configured provider, search for a phrase from the file, follow its entry id to a page and download its bytes. Seal it and attempt another add, then unseal.

What to look for:
- Help, status and results are understandable from the terminal; the listing and bytes are available before indexing, progress is readable, and lag remains visible in search results.
- JSON can be redirected without progress messages mixed in; download preserves exact bytes; the duplicate and sealed-write errors explain the library refusal.

Variations to explore:
- Search two shelves with repeated shelf arguments while one still lags. Is it clear which shelf contributed each hit and which is catching up?
- Try a missing file or an invalid page. Does the error make the next action clear?
