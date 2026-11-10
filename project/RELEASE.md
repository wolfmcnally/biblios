# Biblios 0.2.0

Biblios 0.2.0 is an experimental Python library and command-line tool. It adds the six consumer library capability families and configurable source-file admission to the initial 0.1.0 library and CLI. It supports Python 3.12 and 3.13; the checked-in managed runtime is Python 3.12. macOS and Linux have been exercised during development. Windows and other Python versions are not qualified. The API and on-disk formats may change before a stable release; keep your original documents and inspect changes before updating an existing shelf.

## Install and use

See README.md for a source-checkout installation, command help and a hermetic example. Git and uv are prerequisites because the four substrate libraries are pinned to published public Git release tags. The lockfile records the exact resolved commits. A generic package with the same name on an index is not a substitute. The wheel records the same release-tag Git requirements, so installers need Git and access to GitHub as well as the Python package index.

The CLI has 22 operations over the existing library. It includes curation, reconciliation, status, verification, sealing, federated search, reading and source-byte download. An MCP server is not included. The finite consumer library contract, including explicit retained-content/excision coordination, is documented in [ADOPTION.md](ADOPTION.md). It adds no CLI command: remove is still logical deletion and retains source bytes in Castle.

## Update and uninstall

For a source checkout, fetch the release you intend to use, inspect its changes and run uv sync --locked --managed-python from this directory. Never merge an older private development history into the initial public history; use a fresh clone for that transition. Do not overwrite shelves or configuration as part of updating the application.

For an installed wheel, install the new reviewed wheel into a new virtual environment and exercise it on a disposable copy of a shelf before switching callers. Remove the old virtual environment only when its owner no longer needs it. Uninstalling the package or deleting its environment does not remove shelves, source files, configuration or separately installed conversion tools and model services.

## Build and contribute

Build from this directory with uv build. The source distribution explicitly includes the package, tests, examples, metadata, lockfile and license/user instructions. The wheel includes the package, entry point, metadata and license notices; it excludes development history and configuration.

Report reproducible issues or propose changes at https://github.com/wolfmcnally/biblios/issues. Include the version, platform and a minimal synthetic example; omit private documents, configuration values and credentials. Development checks from a standalone source directory are the ruff and pytest commands in README.md. The complete repository additionally supplies its documented governance checks.

See LICENSE and NOTICE for ownership and third-party terms. No model weights or optional external converter are part of these artifacts.
