"""The shell boundary must preserve the library's calls, results and handle lifetime."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import sysconfig
from dataclasses import asdict
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest
from castle.namespace import SnapshotRecord

from biblios import (
    BibliosError,
    BringCurrentResult,
    Entry,
    EntryMetadata,
    EntryOutcome,
    EntryProgress,
    IndexStatus,
    Mutation,
    Page,
    SealRecord,
    SearchResult,
    Shelf,
    ShelfManifest,
    Stack,
    StackResult,
    VerifyReport,
    Withheld,
    cli,
    open_shelf,
)
from biblios.testing import HashEmbedder
from tests.support import SETTINGS_TEXT, make_shelf

ENTRY = Entry("entry", "file", "résumé.txt", 3, "content", EntryMetadata("text/plain", "x", "now"))
MUTATION = Mutation("request", "create-file", "entry", ("entry",), 1, 2, False)
STATUS = IndexStatus(
    "shelf",
    2,
    1,
    True,
    False,
    1,
    0,
    1,
    0,
    (EntryProgress("shelf", "entry", "résumé.txt", "stored"),),
)
PAGE = Page(ENTRY, 2, "page two\n", 3)
REPORT = BringCurrentResult(
    "00000000-0000-4000-8000-000000000000", 2, (EntryOutcome("entry", "searchable"),), ()
)

# An independent contract inventory: these inputs must retain all public arguments.
CASES = [
    (
        "add",
        [
            "résumé.txt",
            "SOURCE",
            "--tag",
            "a",
            "--date",
            "created=now",
            "--media-type",
            "text/x",
            "--allow-duplicate",
            "--operation-id",
            "retry",
        ],
        "add",
        ("résumé.txt", b"\x00\xffx"),
        {
            "media_type": "text/x",
            "tags": ["a"],
            "dates": {"created": "now"},
            "allow_duplicate": True,
            "operation_id": "retry",
        },
        MUTATION,
    ),
    (
        "mkdir",
        ["folder", "--operation-id", "retry"],
        "mkdir",
        ("folder",),
        {"operation_id": "retry"},
        MUTATION,
    ),
    (
        "update",
        ["résumé.txt", "SOURCE", "--operation-id", "retry"],
        "update",
        ("résumé.txt", b"\x00\xffx"),
        {"operation_id": "retry"},
        MUTATION,
    ),
    (
        "rename",
        ["old", "new", "--operation-id", "retry"],
        "rename",
        ("old", "new"),
        {"operation_id": "retry"},
        MUTATION,
    ),
    (
        "move",
        ["old", "folder", "--operation-id", "retry"],
        "move",
        ("old", "folder"),
        {"operation_id": "retry"},
        MUTATION,
    ),
    (
        "copy",
        ["old", "folder", "--operation-id", "retry"],
        "copy",
        ("old", "folder"),
        {"operation_id": "retry"},
        MUTATION,
    ),
    (
        "remove",
        ["old", "--operation-id", "retry"],
        "remove",
        ("old",),
        {"operation_id": "retry"},
        MUTATION,
    ),
    (
        "grant",
        ["notes/a.md", "--operation-id", "retry"],
        "grant_first_ingestion",
        ("notes/a.md",),
        {"operation_id": "retry"},
        True,
    ),
    ("ls", ["folder"], "ls", ("folder",), {}, [ENTRY]),
    ("stat", ["résumé.txt"], "stat", ("résumé.txt",), {}, ENTRY),
    ("tags", ["résumé.txt"], "tags", ("résumé.txt",), {}, ("one", "two")),
    (
        "set-tags",
        ["résumé.txt", "--tag", "one", "--date", "created=now"],
        "set_tags",
        ("résumé.txt", ["one"]),
        {"dates": {"created": "now"}},
        ENTRY,
    ),
    ("status", [], "index_status", (), {}, STATUS),
    ("verify", [], "verify", (), {}, VerifyReport(False, ("damaged",))),
    ("verify-seal", [], "verify_seal", (), {}, VerifyReport(True, ())),
    (
        "seal",
        [],
        "seal",
        (),
        {},
        SealRecord(1, "now", SnapshotRecord("snapshot", "cas", 4, 7), 8, {"shelf.json": "digest"}),
    ),
    ("resolve", ["entry"], "resolve", ("entry",), {}, ENTRY),
    ("read", ["entry", "--page", "2"], "read", ("entry",), {"page": 2}, PAGE),
    ("download", ["entry"], "download", ("entry",), {}, b"\x00\xffx"),
]


@pytest.mark.parametrize(
    "command,arguments,method,positional,keywords,result", CASES, ids=[row[0] for row in CASES]
)
def test_delegation_and_complete_json(
    command, arguments, method, positional, keywords, result, tmp_path, monkeypatch, capsys
):
    """Parsing must preserve options, one operation, result fields and handle lifetime."""
    source = tmp_path / "source"
    source.write_bytes(b"\x00\xffx")
    shelf = MagicMock(spec=Shelf)
    shelf.__enter__.return_value = shelf
    getattr(shelf, method).return_value = result
    opened = MagicMock(return_value=shelf)
    monkeypatch.setattr(cli, "open_shelf", opened)
    argv = [command, "--shelf", str(tmp_path), "--json"]
    argv += [str(source) if arg == "SOURCE" else arg for arg in arguments]
    exit_code = cli.main(argv)
    output = capsys.readouterr()
    assert exit_code == (1 if command == "verify" else 0), "operation exit status"
    opened.assert_called_once_with(tmp_path)
    actual_calls = [c for c in shelf.method_calls if not c[0].startswith("__")]
    assert actual_calls == [getattr(call, method)(*positional, **keywords)], "delegation arguments"
    expected = (
        base64.b64encode(result).decode()
        if isinstance(result, bytes)
        else json.loads(json.dumps(result, default=asdict))
    )
    assert json.loads(output.out) == expected, "complete JSON result"
    assert output.err == "", "unexpected diagnostic"
    shelf.__exit__.assert_called_once()


@pytest.mark.parametrize("command", ["init", "unseal"])
def test_direct_handles_and_explicit_configuration(command, tmp_path, monkeypatch, capsys):
    """Failure mode: unseal pre-opens an exclusive handle or config files/root are changed."""
    settings = object()
    load = MagicMock(return_value=settings)
    monkeypatch.setattr(cli, "load_settings", load)
    forbidden = MagicMock(side_effect=AssertionError("pre-opened direct operation"))
    monkeypatch.setattr(cli, "open_shelf", forbidden)
    shelf = MagicMock(spec=Shelf)
    shelf.__enter__.return_value = shelf
    shelf.manifest = ShelfManifest("shelf", "named", "description")
    operation = MagicMock(return_value=shelf)
    monkeypatch.setattr(cli, "init_shelf" if command == "init" else "unseal", operation)
    argv = [command, "--shelf", str(tmp_path), "--json"]
    if command == "init":
        argv += [
            "--config",
            "a.yaml",
            "--config",
            "b.yaml",
            "--config-root",
            "cfg",
            "--name",
            "named",
            "--description",
            "description",
        ]
    assert cli.main(argv) == 0, "direct operation exit"
    assert json.loads(capsys.readouterr().out) == asdict(shelf.manifest), "manifest output"
    if command == "init":
        load.assert_called_once_with([Path("a.yaml"), Path("b.yaml")], Path("cfg"))
        operation.assert_called_once_with(
            tmp_path, settings, name="named", description="description"
        )
    else:
        operation.assert_called_once_with(tmp_path)
    shelf.__exit__.assert_called_once()


def test_search_single_stack_and_partial_open_cleanup(tmp_path, monkeypatch, capsys):
    """Search must preserve lag, select one/many members and close a partial open."""
    a, b = MagicMock(spec=Shelf), MagicMock(spec=Shelf)
    for shelf in (a, b):
        shelf.__enter__.return_value = shelf
    single = SearchResult((), "disabled", (Withheld("a", "dead", "not-live", 1),), (), 2, 1, True)
    a.search.return_value = single
    stack = MagicMock(spec=Stack)
    union = StackResult(
        (), "failed", (Withheld("shelf", "dead", "not-live", 1),), (), ("shelf",), {"shelf": STATUS}
    )
    stack.search.return_value = union
    declared = MagicMock(return_value=stack)
    monkeypatch.setattr(cli, "open_stack", declared)
    opened = MagicMock(return_value=a)
    monkeypatch.setattr(cli, "open_shelf", opened)
    argv = ["search", "phrase", "--shelf", "a", "--limit", "9", "--no-rerank", "--json"]
    assert cli.main(argv) == 0, "single search exit"
    a.search.assert_called_once_with("phrase", limit=9, rerank=False)
    declared.assert_not_called()
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(asdict(single))), (
        "single lag"
    )
    opened.side_effect = [a, b]
    assert cli.main(argv + ["--shelf", "b"]) == 0, "stack search exit"
    declared.assert_called_once_with([a, b])
    stack.search.assert_called_once_with("phrase", limit=9, rerank=False)
    assert json.loads(capsys.readouterr().out) == json.loads(json.dumps(asdict(union))), "stack lag"
    a.reset_mock()
    opened.side_effect = [a, BibliosError("missing member")]
    assert cli.main(argv + ["--shelf", "missing"]) == 1, "partial-open error exit"
    assert "missing member" in capsys.readouterr().err, "partial-open diagnostic"
    a.__exit__.assert_called_once()


def test_progress_binary_and_failure_streams(tmp_path, monkeypatch, capsysbinary):
    """Failure mode: progress contaminates JSON, bytes are recoded or an error returns success."""
    shelf = MagicMock(spec=Shelf)
    shelf.__enter__.return_value = shelf
    monkeypatch.setattr(cli, "open_shelf", lambda root: shelf)
    settings = object()
    load = MagicMock(return_value=settings)
    monkeypatch.setattr(cli, "load_settings", load)
    progress = EntryProgress("shelf", "entry", "résumé.txt", "indexing", 1, 2, 3)

    def reconcile(received, *, progress, entry_ids, sweep):
        assert entry_ids is None and not sweep, "reconcile sweeps nothing unless asked"
        assert received is settings, "reconcile configuration"
        progress(EntryProgress("shelf", "entry", "résumé.txt", "indexing", 1, 2, 3))
        return REPORT

    shelf.reconcile.side_effect = reconcile
    assert (
        cli.main(
            [
                "reconcile",
                "--shelf",
                str(tmp_path),
                "--config",
                "a.yaml",
                "--config-root",
                "cfg",
                "--json",
            ]
        )
        == 0
    ), "reconcile exit"
    output = capsysbinary.readouterr()
    assert json.loads(output.out) == json.loads(json.dumps(asdict(REPORT))), "reconcile result"
    assert json.loads(output.err) == asdict(progress), "progress stream"
    load.assert_called_once_with([Path("a.yaml")], Path("cfg"))
    shelf.download.return_value = b"\x00\xff\r\n"
    assert cli.main(["download", "--shelf", str(tmp_path), "entry"]) == 0, "binary exit"
    assert capsysbinary.readouterr().out == b"\x00\xff\r\n", "raw binary capture"
    destination = tmp_path / "output"
    assert (
        cli.main(["download", "--shelf", str(tmp_path), "entry", "--output", str(destination)]) == 0
    ), "file exit"
    assert destination.read_bytes() == b"\x00\xff\r\n", "exact downloaded bytes"
    shelf.read.return_value = PAGE
    assert cli.main(["read", "--shelf", str(tmp_path), "entry"]) == 0, "text read exit"
    assert capsysbinary.readouterr().out == b"page two\n", "human page text"
    shelf.stat.side_effect = BibliosError("no entry")
    assert cli.main(["stat", "--shelf", str(tmp_path), "missing"]) == 1, "refusal exit"
    output = capsysbinary.readouterr()
    assert output.out == b"" and b"no entry" in output.err, "refusal streams"


def test_real_curation_and_explicit_init(tmp_path, capsys):
    """Failure mode: doubles hide incorrect namespace destinations or lost source bytes."""
    config = tmp_path / "settings.yaml"
    config.write_text(SETTINGS_TEXT)
    source = tmp_path / "source.txt"
    source.write_bytes(b"original\x00bytes")
    root = tmp_path / "shelf"
    commands = [
        ["init", "--config", str(config), "--config-root", str(tmp_path), "--name", "notes"],
        ["add", "note.txt", str(source), "--tag", "topic"],
        ["mkdir", "folder"],
        ["rename", "note.txt", "renamed.txt"],
        ["move", "renamed.txt", "folder/moved.txt"],
        ["copy", "folder/moved.txt", "copy.txt"],
        ["update", "folder/moved.txt", str(config)],
        ["remove", "copy.txt"],
    ]
    outcomes = []
    for arguments in commands:
        code = cli.main([*arguments, "--shelf", str(root), "--json"])
        output = capsys.readouterr()
        outcomes.append((arguments[0], code, output.err))
    with open_shelf(root) as shelf:
        entries = {item.path: item for item in shelf.ls("folder")}
        moved = entries.get("folder/moved.txt")
        actual = (
            outcomes,
            sorted(item.path for item in shelf.ls()),
            None if moved is None else shelf.download(moved.entry_id),
            None if moved is None else shelf.tags(moved.path),
        )
    assert actual == (
        [(arguments[0], 0, "") for arguments in commands],
        ["folder"],
        config.read_bytes(),
        ("topic",),
    ), "persisted CLI curation"


def test_real_shelf_workflow(tmp_path, capsys):
    """Failure mode: doubles conceal a wrong persisted path, bytes, sealed guard or leaked lock."""
    shelf, settings = make_shelf(tmp_path)
    shelf.add("note.txt", b"A distinctive local knowledge note.\n")
    shelf.reconcile(settings, embedder=HashEmbedder())
    entry = shelf.stat("note.txt")
    root = shelf.root
    shelf.close()

    def run(command, *arguments):
        code = cli.main([command, "--shelf", str(root), "--json", *arguments])
        output = capsys.readouterr()
        return code, output

    code, output = run("set-tags", "note.txt", "--tag", "topic")
    assert code == 0 and json.loads(output.out)["metadata"]["tags"] == ["topic"], "persisted tags"
    code, output = run("set-tags", "note.txt", "--tag", "topic", "--date", "created=now")
    assert (code, json.loads(output.out)["metadata"]["dates"]) == (0, {"created": "now"}), (
        "replaced dates"
    )
    code, output = run("set-tags", "note.txt", "--tag", "topic")
    assert (code, json.loads(output.out)["metadata"]["dates"]) == (0, {"created": "now"}), (
        "preserved dates"
    )
    code, output = run("set-tags", "note.txt", "--tag", "topic", "--clear-dates")
    assert (code, json.loads(output.out)["metadata"]["dates"]) == (0, {}), "cleared dates"
    code, output = run("download", entry.entry_id)
    assert (
        code == 0
        and base64.b64decode(json.loads(output.out)) == b"A distinctive local knowledge note.\n"
    ), "persisted download"
    code, output = run("read", entry.entry_id)
    assert code == 0 and "distinctive" in json.loads(output.out)["text"], "persisted page"
    # Updating metadata advances the namespace; catch up before sealing through the library.
    with open_shelf(root) as shelf:
        shelf.reconcile(settings, embedder=HashEmbedder())
    code, output = run("seal")
    assert code == 0 and json.loads(output.out)["generation"] == 1, "real seal"
    source = tmp_path / "source.txt"
    source.write_text("another note")
    code, output = run("add", "another.txt", str(source))
    assert code == 1 and "sealed" in output.err, "sealed refusal"
    code, output = run("verify")
    assert code == 0 and json.loads(output.out)["ok"], "real verify"
    code, output = run("unseal")
    assert code == 0 and json.loads(output.out)["mode"] == "dynamic", "real unseal"
    code, output = run("add", "another.txt", str(source))
    assert code == 0, "unsealed add"
    code, output = run("add", "duplicate.txt", str(source))
    assert code == 1 and "content already held" in output.err, "duplicate refusal"
    code, output = run("status")
    assert code == 0 and json.loads(output.out)["lagging"], "stored before indexing"
    with open_shelf(root) as shelf:
        assert shelf.stat("another.txt").size == len(b"another note"), "handle closed"


@pytest.mark.parametrize("entrypoint", ["console", "module"])
def test_console_and_module_entrypoints(entrypoint):
    """Failure mode: script metadata or module wiring is broken while direct main tests pass."""
    console = Path(sysconfig.get_path("scripts")) / "biblios"
    argv = (
        [str(console), "--help"]
        if entrypoint == "console"
        else [sys.executable, "-m", "biblios", "--help"]
    )
    result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, "entrypoint exit"
    expected = {
        "init",
        "add",
        "mkdir",
        "update",
        "rename",
        "move",
        "copy",
        "remove",
        "ls",
        "stat",
        "tags",
        "set-tags",
        "reconcile",
        "grant",
        "status",
        "verify",
        "verify-seal",
        "seal",
        "unseal",
        "search",
        "resolve",
        "read",
        "download",
    }
    commands = {
        name.strip() for name in result.stdout.partition("{")[2].partition("}")[0].split(",")
    }
    assert commands == expected, "entrypoint operations"
