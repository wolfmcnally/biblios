"""The command-line plane: arguments and presentation over the public library."""

from __future__ import annotations

import argparse
import base64
import json
import sys
from contextlib import ExitStack
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from castle.namespace import CastleError
from palace.index._errors import IndexError as PalaceIndexError

from biblios import BibliosError, init_shelf, load_settings, open_shelf, open_stack, unseal


def _date(value: str) -> tuple[str, str]:
    key, separator, text = value.partition("=")
    if not separator or not key:
        raise argparse.ArgumentTypeError("expected NAME=VALUE")
    return key, text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="biblios", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    descriptions = {
        "init": "Create a shelf with an explicit configuration",
        "add": "Store a file's exact bytes at a namespace path",
        "mkdir": "Create a namespace directory",
        "update": "Replace a file's bytes, preserving its entry id",
        "rename": "Rename an entry",
        "move": "Move an entry to a full destination path",
        "copy": "Copy an entry to a full destination path",
        "remove": "Logically remove an entry; stored bytes remain",
        "ls": "List a namespace directory",
        "stat": "Inspect an entry at its current path",
        "tags": "Read a file's tags",
        "set-tags": "Replace a file's tags and optionally dates",
        "reconcile": "Bring mirrors and index up to date, reporting progress",
        "status": "Report the index's lag and per-entry progress",
        "verify": "Verify the shelf and, when sealed, its seal",
        "verify-seal": "Verify a sealed artifact's recorded digests",
        "seal": "Make a current shelf a portable read-only artifact",
        "unseal": "Return a sealed or incompletely sealed shelf to dynamic",
        "search": "Search one shelf or a stack; report lagging members",
        "resolve": "Resolve a live entry id to its current path",
        "read": "Read a mirror or one of its pages by entry id",
        "download": "Write an entry's exact source bytes",
    }
    path_commands = {
        "add",
        "mkdir",
        "update",
        "rename",
        "move",
        "copy",
        "remove",
        "stat",
        "tags",
        "set-tags",
    }
    mutations = {"add", "mkdir", "update", "rename", "move", "copy", "remove"}
    for name, help_text in descriptions.items():
        command = commands.add_parser(name, help=help_text, description=help_text)
        command.add_argument(
            "--shelf", type=Path, required=True, action="append" if name == "search" else "store"
        )
        command.add_argument("--json", action="store_true", help="Emit the complete JSON result")
        if name in path_commands:
            command.add_argument("path", help="Path inside the shelf")
        if name == "ls":
            command.add_argument("path", nargs="?", default="")
        if name in {"add", "update"}:
            command.add_argument("file", type=Path, help="Source file on disk")
        if name in mutations:
            command.add_argument("--operation-id", help="Library idempotency key")
        if name in {"add", "set-tags"}:
            command.add_argument("--tag", action="append", default=[])
            dates = command.add_mutually_exclusive_group()
            dates.add_argument("--date", type=_date, action="append")
            if name == "set-tags":
                dates.add_argument("--clear-dates", action="store_true", help="Remove all dates")
        if name == "add":
            command.add_argument("--media-type")
            command.add_argument("--allow-duplicate", action="store_true")
        if name == "rename":
            command.add_argument("name", help="New entry name")
        if name in {"move", "copy"}:
            command.add_argument("destination", help="Full destination path inside the shelf")
        if name in {"init", "reconcile"}:
            command.add_argument("--config", type=Path, action="append", required=True)
            command.add_argument("--config-root", type=Path, required=True)
        if name == "init":
            command.add_argument("--name", required=True)
            command.add_argument("--description", default="")
        if name == "search":
            command.add_argument("query")
            command.add_argument("--limit", type=int, default=5)
            command.add_argument("--no-rerank", action="store_true")
        if name in {"resolve", "read", "download"}:
            command.add_argument("entry", help="Stable entry id")
        if name == "read":
            command.add_argument("--page", type=int)
        if name == "download":
            command.add_argument("--output", type=Path, help="Write bytes here instead of stdout")
    return parser


def _json_default(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    raise TypeError(f"unsupported result type: {type(value).__name__}")


def _progress(value: Any) -> None:
    print(json.dumps(value, default=_json_default, ensure_ascii=False), file=sys.stderr, flush=True)


def _invoke(args: argparse.Namespace, resources: ExitStack) -> Any:
    name = args.command
    if name == "init":
        settings = load_settings(args.config, args.config_root)
        shelf = resources.enter_context(
            init_shelf(args.shelf, settings, name=args.name, description=args.description)
        )
        return shelf.manifest
    if name == "unseal":
        return resources.enter_context(unseal(args.shelf)).manifest
    if name == "search":
        shelves = [resources.enter_context(open_shelf(root)) for root in args.shelf]
        target = shelves[0] if len(shelves) == 1 else open_stack(shelves)
        return target.search(args.query, limit=args.limit, rerank=not args.no_rerank)
    shelf = resources.enter_context(open_shelf(args.shelf))
    match name:
        case "add":
            return shelf.add(
                args.path,
                args.file.read_bytes(),
                media_type=args.media_type,
                tags=args.tag,
                dates=dict(args.date or ()),
                allow_duplicate=args.allow_duplicate,
                operation_id=args.operation_id,
            )
        case "mkdir" | "remove":
            return getattr(shelf, name)(args.path, operation_id=args.operation_id)
        case "update":
            return shelf.update(args.path, args.file.read_bytes(), operation_id=args.operation_id)
        case "rename":
            return shelf.rename(args.path, args.name, operation_id=args.operation_id)
        case "move" | "copy":
            return getattr(shelf, name)(args.path, args.destination, operation_id=args.operation_id)
        case "ls" | "stat" | "tags":
            return getattr(shelf, name)(args.path)
        case "set-tags":
            return shelf.set_tags(
                args.path,
                args.tag,
                dates={} if args.clear_dates else None if args.date is None else dict(args.date),
            )
        case "status":
            return shelf.index_status()
        case "reconcile":
            return shelf.reconcile(load_settings(args.config, args.config_root), progress=_progress)
        case "verify" | "verify-seal" | "seal":
            return getattr(shelf, name.replace("-", "_"))()
        case "resolve" | "download":
            return getattr(shelf, name)(args.entry)
        case "read":
            return shelf.read(args.entry, page=args.page)
    raise AssertionError(f"unhandled command: {name}")


def main(argv: list[str] | None = None) -> int:
    """Run one operation; argparse owns usage/help exits, results own stdout."""
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "download" and args.json and args.output is not None:
        parser.error("--json and --output are mutually exclusive")
    try:
        with ExitStack() as resources:
            result = _invoke(args, resources)
            if args.command == "download" and not args.json:
                if args.output is not None:
                    args.output.write_bytes(result)
                else:
                    sys.stdout.buffer.write(result)
            elif args.command == "read" and not args.json:
                print(result.text, end="")
            else:
                print(
                    json.dumps(
                        result,
                        default=_json_default,
                        ensure_ascii=False,
                        indent=None if args.json else 2,
                    )
                )
            return int(args.command in {"verify", "verify-seal"} and not result.ok)
    except (BibliosError, CastleError, PalaceIndexError, OSError, ValueError) as error:
        print(f"biblios: {error}", file=sys.stderr)
        return 1
