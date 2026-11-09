# Biblios

Biblios builds portable knowledge bases called **shelves**. Each shelf keeps exact source files, a file tree, Markdown mirrors and a searchable index in one directory. Search one shelf or federate several as a **stack**, with each result identifying its shelf and source. Curate a live shelf while its index catches up, or seal it and carry a read-only copy elsewhere.

**Published version 0.1.0 and development version 0.2.0 are experimental.** The Python library and 22-command CLI are implemented. APIs and storage formats may change before a stable release. The development library adds an [explicit consumer adoption contract](project/ADOPTION.md), including finite history and excision coordination. The CLI remains unchanged; removing an entry retains its bytes in Castle. MCP is not included. A shelf provides no encryption, authentication or authorization. Protect sensitive material in the surrounding application and storage.

## Install

You need Git and [uv](https://docs.astral.sh/uv/). The repository's complete contributor setup also requires Node v24.13.1, installed in the usual nvm layout or selected by setting `BIBLIOS_NODE_BIN` to the absolute directory containing that version's `node` and `npm`. Setup validates Node but does not install it; see the [infrastructure toolchain](infra/README.md). Library/CLI users who do not need contributor tooling can use the [standalone package installation](project/README.md), which does not require Node. Python 3.12 and 3.13 are supported; the checkout selects a managed Python 3.12 runtime. Development has exercised macOS and Linux, including Amazon Linux 2023. Windows is not qualified. No AI coding harness is required to use Biblios.

Clone the repository, then work from its root:

```sh
git clone https://github.com/wolfmcnally/biblios.git
```

```sh
cd biblios
```

```sh
./bin/setup
```

Setup installs the locked environment and probes its dependencies. A new installation needs access to GitHub and the Python package index. The four substrate libraries use exact Git revisions; an unrelated index package with the same name is not a replacement. See the [standalone package instructions](project/README.md) for a source archive or wheel.

## Try it

From the checkout root:

```sh
./bin/python -m biblios --help
```

For a complete shelf walkthrough without an embedding service, choose a new disposable directory and a Markdown or text file:

```sh
./bin/python project/examples/shelf_demo.py /tmp/biblios-first-shelf notes.md
```

The example stores, converts, indexes, searches, reads and renames the file, then verifies the shelf. It uses deterministic test embeddings and a test reranker, which have no retrieval quality and contact no model service. A successful run reports a current index and `ok=True` verification. Use a directory that does not already hold a shelf.

For real retrieval, declare the provider and model in an explicit Conric configuration. Biblios forwards the `palace`, `drawbridge` and `vellric` sections to those components; it does not start a service or silently select a fallback. A remote provider may transmit selected content and incur its ordinary charges. See [Get Started](manual/src/getting_started.md) and [Command-Line Tool](manual/src/command_line.md) for configuration, curation, JSON output, search and sealing.

## Prerequisites and limits

Drawbridge converts supported formats. External conversion tools and model services are separately installed and configured. Plain-text and Markdown workflows need no OCR engine; other formats may require `file`, Tesseract or Vellric. Unsupported sources remain stored and are reported as unconverted. Vellric has its own AGPL terms and is not bundled. No model weights or OCR executable ship in Biblios's package. See [notices](project/NOTICE).

Keep original documents. Avoid placing a live shelf in a folder whose sync service changes read-only file modes. Reconciliation must finish before sealing. Verify a sealed copy at its destination. Updating or uninstalling the application does not delete shelves or configuration; see [update and uninstall guidance](project/RELEASE.md).

## Checks and contributing

From the repository root:

```sh
./bin/test project/tests
```

```sh
./bin/check all
```

The full gate also checks governance and builds the manual. Documentation tools are pinned in `documentation.toml`; use `./bin/docs setup` if absent, with Cargo and the pinned Rust toolchain available. The [manual source](manual/src/SUMMARY.md) is local; no hosted documentation URL is assumed.

Report issues and propose changes through [GitHub issues](https://github.com/wolfmcnally/biblios/issues). Include the version, platform and a minimal synthetic reproduction, omitting private files, configuration values and credentials. [Release preparation](RELEASE.md) explains the artifact and provenance scope. Development follows the [Agentic Coding Starter](https://github.com/wolfmcnally/agentic-coding-starter), with requirements in [the brief](briefs/BRIEF.md), future work in [the plan](plan/INDEX.md), and contributor guidance in [AGENTS.md](AGENTS.md).

## License

Original Biblios work is Copyright © 2026 Wolf McNally, under the [MIT License](LICENSE). Castle stores bytes and namespaces, Drawbridge converts files, Palace indexes and searches, and Conric composes configuration. Separately installed dependencies and optional tools retain their own terms. Vendored documentation and dashboard assets retain their included licenses.
