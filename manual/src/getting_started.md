# Get Started

This guide sets up Biblios's library and CLI from a checkout. You need Git and [uv](https://docs.astral.sh/uv/); uv installs the pinned Python 3.12 runtime itself. The checkout targets experimental 0.2.2. Python 3.12 and 3.13 are supported; macOS and Linux have been exercised, and Windows is unqualified. No AI coding harness is needed to use the tool. MCP is not included.

## Set up the environment

From the repository root, provision the locked environment:

The complete checkout setup also needs Node v24.13.1 for its offline infrastructure checks. Install that exact version in the usual nvm layout, or set `BIBLIOS_NODE_BIN` to the absolute directory containing its `node` and `npm` executables. Setup validates this prerequisite and installs the pinned CDK CLI; it does not provision Node. The repository's `infra/README.md` describes the toolchain. For library/CLI use without contributor tooling, follow the standalone `project/README.md` installation, which needs no Node.

```sh
./bin/setup
```

Setup installs the managed interpreter and the exact locked dependencies, then probes that the `biblios` package, pytest and ruff load inside that environment. A refusal names the missing prerequisite.

## Run the checks

Run the full gate:

```sh
./bin/check all
```

Run only the product tests while iterating:

```sh
./bin/test project/tests
```

For consumer-owned storage and provider lifetimes, use the [Consumer Adoption](consumer_adoption.md) guide. This library lane does not add CLI or MCP commands.

## Use the library

The library is the contract; the [command-line tool](command_line.md) exposes its operations from a shell. Import it through the repository's selected interpreter:

```sh
./bin/python -c "import biblios; print(biblios.__version__)"
```

A shelf is configured through one Conric composition. The `palace` section is Palace's own: it selects the embedding provider, model and dimension, and Biblios hands it to Palace exactly as composed, never reading it for itself; the `drawbridge` and `vellric` sections travel to Drawbridge unchanged; the optional `biblios.max_file_bytes` setting declares the consumer curation per-source-file byte limit (default 104857600, exactly 100 MiB). Pass the loaded settings to `bound.curate(request, settings=settings)`; calls omitting settings use that default. This positive integer allowance is separate from conversion-output, chunk, checkpoint and snapshot budgets. For example:

```yaml
palace:
  embedding:
    provider: ollama
biblios:
  max_file_bytes: 104857600
drawbridge: {}
vellric: {}
```

Bedrock through Palace is the same section with its own fields, as Palace documents them: `provider: bedrock`, `model: cohere.embed-v4:0`, `region`, `dim` (256, 512, 1024 or 1536), the published-corpus assertion (`published_corpus`, `published_corpus_note`, `published_corpus_asserted_at`) and an optional `profile`; the instance role or the SDK's credential chain signs the requests.

With that file at `config/settings.yaml`, use the library directly as follows; the Command-Line Tool chapter exposes the same operations from the shell:

```python
from pathlib import Path
from biblios import init_shelf, load_settings

settings = load_settings([Path("config/settings.yaml")], Path("config"))
with init_shelf(Path("my-shelf"), settings, name="notes") as shelf:
    added = shelf.add("notes.txt", Path("notes.txt").read_bytes())
    print(shelf.index_status().pending)  # 1: stored, not yet indexed
    shelf.reconcile(settings, progress=print)
    result = shelf.search("fresnel lens")
    print(result.indexing)  # False once the index has caught up
    for hit in result.hits:
        print(hit.path, hit.heading, hit.chunk[:80])
    print(shelf.read(added.entry_id).text)
    shelf.rename("notes.txt", "log.txt")
    print(shelf.verify())
```

Every curation call returns a typed `Mutation` naming the entry it acted on; `stat` and `resolve` read the current tree. A retry carrying the same operation id and the same request returns the original outcome; the same id with a different request is a different operation. `reconcile` converts and indexes, one entry at a time and smallest first, every file whose mirror is missing or stale, reporting each entry's progress to the callback and yielding between entries when asked; `index_status` says how far the index is behind; `search` answers from the index as it stands, says when the shelf is still indexing, and returns hits that name the entry, its current path and its heading; `read` and `download` follow a hit back to its page and its exact bytes. Without an injected embedder, Palace resolves the one the shelf declared, so the example above expects a local Ollama. The runnable walkthrough `project/examples/shelf_demo.py` uses the deterministic stand-in embedder from `biblios.testing` instead and needs no service:

```sh
./bin/python project/examples/shelf_demo.py /tmp/biblios-demo ~/Documents/notes.txt ~/Documents/report.md
```

Several shelves are searched as one through a stack:

```python
from biblios import open_stack

stack = open_stack([notes_shelf, papers_shelf])
result = stack.search("fresnel lens")
for hit in result.hits:
    print(hit.shelf_id, hit.path, hit.heading)
print(result.indexing)  # the shelf ids still catching up, if any
```

The stack demo builds two shelves from two folders, drives each one's `reconcile` from a thread of its own, prints the status table while they catch up, and takes commands for searching, adding and removing while the indexes lag:

```sh
./bin/python project/examples/stack_demo.py /tmp/biblios-stack ~/Documents/folder-a ~/Documents/folder-b
```

A current shelf can be sealed into a static artifact, carried as a directory, and unsealed again:

```python
from biblios import open_shelf, seal, unseal, verify_seal

with open_shelf(Path("my-shelf")) as shelf:
    record = seal(shelf)             # refused while the index lags; the handle turns read-only
with open_shelf(Path("/media/stick/my-shelf")) as copy:  # read-only wherever it was copied
    print(verify_seal(copy).ok, copy.search("fresnel lens").hits)
with unseal(Path("my-shelf")) as shelf:  # needs every reader of that directory closed
    shelf.add("more.txt", b"...")    # dynamic again
```

The seal demo builds a shelf from a folder and takes commands for sealing, copying, searching the copy, trying a write on it, verifying, tampering and unsealing:

```sh
./bin/python project/examples/seal_demo.py /tmp/biblios-seal ~/Documents/folder-a
```

The command-line tool is available now; an MCP server follows in a later phase. Each phase's acceptance names what you can observe when it closes.
