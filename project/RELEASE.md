# Biblios 0.4.0

Biblios 0.4.0 is an experimental Python library and command-line tool. It keeps the 0.2.x consumer library capability families, configurable source-file admission and responsive reads, and replaces the indexing pipeline with one core that publishes each entry on its own, treats mirrors as user-owned, checks every search hit against its accepted record and makes excision spill-grade. It requires Castle v0.4.0 and Palace v0.5.0. 0.4.0 lets a lost derived store be rebuilt from surviving mirrors; 0.3.1 removes excised identifiers from Biblios's own records and stops searches being refused when unrelated entries are indexed during a query. It supports Python 3.12 and 3.13; the checked-in managed runtime is Python 3.12. macOS and Linux have been exercised during development. Windows and other Python versions are not qualified. The API and on-disk formats may change before a stable release; keep your original documents and inspect changes before updating an existing shelf.

## Install and use

See README.md for a source-checkout installation, command help and a hermetic example. Git and uv are prerequisites because the four substrate libraries are pinned to published public Git release tags. The lockfile records the exact resolved commits. A generic package with the same name on an index is not a substitute. The wheel records the same release-tag Git requirements, so installers need Git and access to GitHub as well as the Python package index.

The CLI has 23 operations over the existing library. It includes curation, reconciliation, first-mirror grants, status, verification, sealing, federated search, reading and source-byte download. An MCP server is not included. The finite consumer library contract, including explicit retained-content/excision coordination, is documented in [ADOPTION.md](ADOPTION.md). It adds no CLI command: remove is still logical deletion and retains source bytes in Castle.

## Update and uninstall

For a source checkout, fetch the release you intend to use, inspect its changes and run uv sync --locked --managed-python from this directory. Never merge an older private development history into the initial public history; use a fresh clone for that transition. Do not overwrite shelves or configuration as part of updating the application. A shelf written by 0.2.x keeps its namespace in Castle's v1 format, which 0.3.0 refuses to open (`NamespaceFormatError`) until its owner migrates it once, with a backup, through Castle's `castle.namespace_migrate_v1.migrate_namespace_v1`; Biblios never migrates it. The first reconcile after that records every existing entry, carries over without re-embedding each one whose mirror and index rows match exactly, and leaves the rest pending until reconciled.

Updating from 0.3.x to 0.4.0 changes the consumer library interface: a supplied renderer is now `renderer(result)` and returns `MirrorDocument(markdown)` with no name; `ConversionRequest` carries no display name; `DerivationPolicy.required_header_fields` is replaced by `provenance_header_fields`. Existing mirrors are never rewritten: a mirror that already contains a filename keeps it as content, and the next indexing adds the current Castle filename as its own row. Mirror headers are no longer validated; an entry isolated as `mirror-header-invalid` or `mirror-name-section-invalid` under 0.3.x is indexed again at its next reconcile.

For an installed wheel, install the new reviewed wheel into a new virtual environment and exercise it on a disposable copy of a shelf before switching callers. Remove the old virtual environment only when its owner no longer needs it. Uninstalling the package or deleting its environment does not remove shelves, source files, configuration or separately installed conversion tools and model services.

## Build and contribute

Build from this directory with uv build. The source distribution explicitly includes the package, tests, examples, metadata, lockfile and license/user instructions. The wheel includes the package, entry point, metadata and license notices; it excludes development history and configuration.

Report reproducible issues or propose changes at https://github.com/wolfmcnally/biblios/issues. Include the version, platform and a minimal synthetic example; omit private documents, configuration values and credentials. Development checks from a standalone source directory are the ruff and pytest commands in README.md. The complete repository additionally supplies its documented governance checks.

See LICENSE and NOTICE for ownership and third-party terms. No model weights or optional external converter are part of these artifacts.
