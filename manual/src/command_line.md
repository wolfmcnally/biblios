# Command-Line Tool

Use `biblios` to curate a shelf, catch up its index, search it and follow results back to their source. The commands call the same operations as the Python library. They do not reconcile automatically.

## Start a shelf

After [setting up the environment](getting_started.md), run the module through the repository's selected interpreter. From the standalone `project/` directory, `uv run --locked biblios` is the equivalent installed console command.

```sh
./bin/python -m biblios --help
```

Save the settings shown in [Get Started](getting_started.md#use-the-library) as `config/settings.yaml`. Initialization and reconciliation require an explicit configuration root and one or more configuration files; repeat `--config` to compose several, in order.

```sh
./bin/python -m biblios init --shelf my-shelf --config config/settings.yaml --config-root config --name notes
```

Store an existing file at a path inside the shelf:

```sh
./bin/python -m biblios add --shelf my-shelf notes.txt notes.txt --tag optics --json
```

Adding stores bytes immediately. Reconciliation performs conversion and indexing through the provider declared in the settings; the minimal example expects local Ollama. It reports progress records to stderr and its final result to stdout.

```sh
./bin/python -m biblios reconcile --shelf my-shelf --config config/settings.yaml --config-root config --json
```

```sh
./bin/python -m biblios search --shelf my-shelf 'fresnel lens' --json
```

Search answers from the current index and preserves the library's lag, refusal and rerank information. Repeat `--shelf` to search a stack with one shared rerank:

```sh
./bin/python -m biblios search --shelf my-shelf --shelf papers 'fresnel lens' --limit 10 --json
```

Use an `entry_id` from the result to read a mirror page or recover the original bytes. Replace `ENTRY_ID` with that value.

```sh
./bin/python -m biblios read --shelf my-shelf ENTRY_ID --page 1
```

```sh
./bin/python -m biblios download --shelf my-shelf ENTRY_ID --output recovered.txt
```

## Curate and inspect

Every command requires `--shelf DIRECTORY`. Namespace paths refer to entries inside that shelf; source files and output destinations refer to the filesystem. Quote paths containing spaces. For move and copy, the destination is the full new path, including its name, and its parent directory must exist.

| Command arguments | Operation |
|---|---|
| `init --name NAME --config FILE --config-root ROOT` | Create a shelf; optional `--description TEXT`. |
| `add PATH FILE` | Store exact bytes; optional repeated `--tag TAG`, repeated `--date NAME=VALUE`, `--media-type TYPE`, `--allow-duplicate`. |
| `mkdir PATH` | Create a directory. |
| `update PATH FILE` | Replace bytes while keeping the entry id. |
| `rename PATH NAME` | Change an entry's name. |
| `move PATH DESTINATION` | Move to a full destination path. |
| `copy PATH DESTINATION` | Copy to a full destination path under new entry ids. |
| `remove PATH` | Remove from the live tree; stored bytes remain. |
| `ls [PATH]` | List a directory, defaulting to the root. |
| `stat PATH` | Read a live entry. |
| `tags PATH` | Read its tags. |
| `set-tags PATH [--tag TAG] [--date NAME=VALUE \| --clear-dates]` | Replace tags; omit date options to preserve dates, use repeated `--date` to replace them, or `--clear-dates` to remove all dates. |
| `reconcile --config FILE --config-root ROOT` | Update derived mirrors and index with progress. |
| `status` | Report index lag and per-entry progress. |
| `search QUERY [--limit N] [--no-rerank]` | Search one shelf or several repeated shelf arguments. |
| `resolve ENTRY_ID` | Find an entry's current path. |
| `read ENTRY_ID [--page N]` | Read its mirror or a page. |
| `download ENTRY_ID [--output FILE]` | Recover exact source bytes. |
| `verify` | Check the shelf, including its seal when present. |
| `seal` | Make a current, indexed shelf read-only and portable. |
| `verify-seal` | Check the recorded sealed digests. |
| `unseal` | Return to dynamic mode; also repair an incomplete seal publication. |

`add`, `mkdir`, `update`, `rename`, `move`, `copy` and `remove` accept `--operation-id ID`, forwarded unchanged to the library for idempotent retries. Other commands do not add their own retry mechanism. Use each command's `--help` for its argument grammar.

## Output and errors

Every command accepts `--json`. JSON preserves the complete library result with its field names: dataclasses become objects, tuples become arrays, and downloaded bytes become one base64 string. Without that flag, structured results are indented JSON, `read` writes mirror text, and `download` writes raw bytes to stdout or `--output FILE`. `--json` and `--output` cannot be combined.

Success returns exit status 0. A library or filesystem refusal, or a failed verification report, returns 1. Invalid arguments return 2. Diagnostics and reconciliation progress go to stderr; result data goes to stdout. Each operation closes every shelf handle it opens, including after an error.

Sealed shelves refuse mutations and reconciliation. Seal only after `status` shows the index is current; unseal before further curation. Conditional excision remains deferred until its exposed scope and library contract are settled. No excision command is shipped.
