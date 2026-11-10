# Biblios 0.2.0 release preparation

Version 0.2.0 is the next experimental release at the existing [GitHub repository](https://github.com/wolfmcnally/biblios). It follows the published initial 0.1.0 release and includes the intervening library capabilities; it is not a backport labeled 0.1.1. Publication follows artifact installation checks and the final exact-tree gate. This preparation record does not itself claim publication.

## Intended artifacts

The version tag is `v0.2.0`. The attached package artifacts are `biblios-0.2.0-py3-none-any.whl`, `biblios-0.2.0.tar.gz` and their SHA-256 checksums. No PyPI upload, hosted documentation, model weights, OCR executable, private configuration or user corpus is included. The wheel and source distribution carry the same declared dependencies. Published substrate releases are selected by tag; the lockfile retains their exact resolved commits. A tag alone does not establish that an upstream release was published.

Python support remains 3.12 and 3.13; the managed checkout runtime is Python 3.12. Git and uv fetch public prerequisites. macOS and Linux have development qualification; Windows is unqualified. See [installation](project/README.md) and [update/uninstall guidance](project/RELEASE.md).

## Changes since 0.1.0

The library adds explicit consumer-controlled shelf binding and read-only use, revision-bound retry, injected conversion and rendering, same-shelf body-vector reuse with independent destination-name embeddings, strict search and publication observations, and coordinated finite retained-content/history/excision access. Destination rows remain independent after donor removal. Excision is an explicit library operation; the CLI still has 22 commands and logical removal retains source bytes. MCP remains held.

The optional Conric `biblios.max_file_bytes` setting is a positive integer and defaults to exactly 104857600 bytes (100 MiB). Consumer create, replace, import and copy use the configured limit when passed the composed settings. Aggregate import/copy admission is at least that per-file limit. Conversion body/mirror, chunk-count, checkpoint and vector-snapshot controls remain separate finite limits; source admission does not promise that every permitted upload fits every processing budget.

Explicit larger finite conversion envelopes and snapshot budgets are supported without changing their defaults, model dimensions, rendering or input identity. Durable checkpoint admission measures encoded UTF-8 JSON bytes against the existing 512 MiB ceiling. Retry preserves uncertain dispatch and committed-but-unaccepted distinctions; increasing a read-only resource budget does not silently change the original semantic request or redispatch conversion.

## Public baseline and provenance

The initial release established one canonical public history. This ordinary release continues that same history without replacement, parallel private development lineage or cleanup of required operational records. Completed development records remain historical evidence, not a claim that a later artifact was installed or that human usability was accepted.

The package source allowlist includes the library, tests, examples, metadata, lockfile, license and user instructions. The wheel contains the package, console entry point, metadata and license notices. Repository governance, private execution evidence and local configuration are excluded from both package artifacts. The generated GitHub repository source archives are separate from these curated package artifacts.

## Qualification and limits

Release qualification inspects the actual wheel/source archive, installs each into an isolated environment outside the checkout, verifies dependency sources and installed package origin, and exercises synthetic library/CLI behavior. The repository's changed-path selection and final bare full gate bind the final release source. Their exact results and artifact digests belong to the release receipt and GitHub release notes; historical development runs are preserved separately.

The configured per-file limit is tested with small synthetic fixtures. No 100 MiB stress test, full-RAG boundary run, memory benchmark or performance gate is part of this release. The known pinned mdBook/admonish version warning remains separate from successful generated-link, asset, anchor, search and print checks. Bounded desktop/narrow manual inspection passed on the unchanged affected guide sources. Human CLI/library usability judgments remain open.

Keep original documents. The API and on-disk formats may change before a stable release. Existing shelf data and configuration are not modified by installing or uninstalling the application. Biblios supplies no encryption, authentication or authorization; surrounding applications own those boundaries. No automatic legacy-shelf conversion or external provider call is implied by installation.

## Licensing and notices

Original work is Copyright © 2026 Wolf McNally under [MIT](LICENSE), also included in the standalone package. [Package notices](project/NOTICE) distinguish separately installed dependencies and optional tools. Vellric's separate AGPL terms are not replaced by Biblios's MIT declaration.

Repository-only third-party assets retain their notices: ECharts under Apache-2.0, included Font Awesome SVG icons under CC BY 4.0, mdBook reference/theme material under its included MPL-2.0 license, and admonish material under its included MIT license. The [reference catalog](docs/README.md) records provenance and limited quotation bases for other excerpts. These repository governance/documentation assets are excluded from the package artifacts.
