# Biblios

Portable knowledge bases called shelves, searched alone or federated in a stack. The development version is 0.2.0; the published initial release is 0.1.0. The Python library and thin 22-command CLI remain available. It is experimental: APIs and storage formats may change before a stable release. The generic consumer library lane adds explicit binding, retry, injected conversion, strict search and finite retained-content/excision coordination; see [ADOPTION.md](ADOPTION.md). The CLI does not expose excision; remove remains logical deletion and retains bytes in Castle. MCP is not included. A shelf has no security boundary of its own.

## Install

Use Python 3.12 or 3.13, Git and uv. macOS and Linux have been exercised; Windows is not qualified. From this directory in a source checkout or extracted source distribution:

```sh
uv sync --locked --managed-python
```

```sh
uv run --locked --managed-python python -c "import biblios; print(biblios.__version__)"
```

Expected checkout version: `0.2.0`. The lockfile pins public Git revisions for the substrate libraries. A new installation needs GitHub and the package index. The wheel also records those exact Git requirements; installers need Git as well as access to those sources. No private account configuration or AI coding harness is required.

Version 0.2.0 is a development target, not a newly published artifact. For a reviewed wheel built from this source, create a separate environment and install the exact artifact you inspected:

```sh
uv venv --python 3.12 .venv
```

```sh
uv pip install --python .venv/bin/python biblios-0.2.0-py3-none-any.whl
```

These paths are for macOS/Linux. See RELEASE.md for update/uninstall and reporting guidance.

## Try it

The installed console script and module share one interface:

```sh
uv run --locked --managed-python biblios --help
```

```sh
uv run --locked --managed-python python -m biblios --help
```

For a hermetic shelf walkthrough from this source directory, choose a new disposable directory and a Markdown or text file:

```sh
uv run --locked --managed-python python examples/shelf_demo.py /tmp/biblios-demo notes.md
```

Expected: the source is stored, converted and indexed, a search resolves to its live entry, renaming preserves identity, and verification reports `ok=True`. The example uses deterministic test embeddings and a test reranker with no retrieval quality; it calls no model service. Plain-text/Markdown need no OCR engine. Other formats may require separately installed converters and are reported unconverted when unavailable.

For real retrieval, explicitly configure the provider you intend to use. The settings file is a Conric composition with `palace`, `drawbridge` and `vellric` sections, forwarded unchanged. A `biblios` section is optional. For example, this declares Ollama; it does not install or start it:

```yaml
palace:
  embedding:
    provider: ollama
drawbridge: {}
vellric: {}
```

With that file at `config/settings.yaml`, initialize a new shelf and add a source:

```sh
uv run --locked --managed-python biblios init --shelf my-shelf --config config/settings.yaml --config-root config --name notes
```

```sh
uv run --locked --managed-python biblios add --shelf my-shelf notes.txt notes.txt --json
```

Run `reconcile` with the same explicit configuration before searching newly stored content. It uses the declared provider and reports progress to stderr; a remote provider may transmit content and incur charges. Every command accepts `--json`; download bytes become base64 in JSON, or exact bytes without it. `download --output FILE` saves a source. Move and copy take complete destination paths including the new name.

## Troubleshooting and safety

A failed install should identify the unavailable Git revision, index package or unsupported interpreter. Use the supported Python range and checked-in lock; do not substitute similarly named index packages. An unavailable embedder is a configuration error, not permission to start a service or switch models. An unconverted file remains stored but is not searchable. Index lag is explicit; seal is refused until the index is current.

Keep original documents. Avoid synced folders that change Castle's read-only file modes, and verify sealed copies at their destination. Updating or uninstalling the application does not delete shelves, configuration or external tools. See LICENSE, NOTICE and RELEASE.md for ownership, separate dependency terms and experimental limits.

## Development checks

From this source directory:

```sh
uv run --locked --managed-python ruff check biblios tests
```

```sh
uv run --locked --managed-python ruff format --check biblios tests
```

```sh
uv run --locked --managed-python python -m pytest -q
```
