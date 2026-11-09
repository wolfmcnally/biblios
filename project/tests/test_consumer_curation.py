"""Strict caller retries, frozen imports and truthful mutation effects."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path
from threading import Barrier

import pytest
from castle.namespace import recover_namespace
from castle.namespace_environment import LocalSession

import biblios.curation as curation_module
from biblios.binding import open_bound_shelf
from biblios.control import ConsumerError
from biblios.curation import (
    CreateDirectory,
    CreateFile,
    CurationRequest,
    ImportFolder,
    ImportItem,
    RelocateEntry,
    ReplaceContent,
)
from tests.consumer_fixtures import (
    ADDED,
    binding_from_document,
    consumer_fixture,
    identifier,
    inventory,
)


def _crash_import(directory: Path, boundary: str) -> None:
    import castle.namespace_transactions as transactions

    fixture = consumer_fixture(directory)
    folder, empty, one, two = (identifier() for _ in range(4))
    request = CurationRequest(
        identifier(),
        0,
        ImportFolder(
            fixture.root_id,
            (
                ImportItem(folder, fixture.root_id, "folder"),
                ImportItem(empty, folder, "empty"),
                ImportItem(one, folder, "one.txt", b"one", "text/plain", ADDED),
                ImportItem(two, folder, "two.txt", b"two", "text/plain", ADDED),
            ),
        ),
    )
    document = {"binding": asdict(fixture.binding), "request": asdict(request)}
    (directory / "curation-input.json").write_text(
        json.dumps(
            document, default=lambda value: value.hex() if isinstance(value, bytes) else str(value)
        )
    )
    original = (
        transactions.content_io.publish
        if boundary == "content"
        else curation_module.execute_namespace
    )

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        os._exit(78)

    if boundary == "content":
        transactions.content_io.publish = interrupt
    else:
        curation_module.execute_namespace = interrupt
    fixture.bound.curate(request)
    raise AssertionError("curation interruption boundary was not reached")


@pytest.mark.parametrize("boundary", ("content", "namespace"))
def test_real_import_interruption_recovers_complete_original_request(
    tmp_path: Path, boundary: str
) -> None:
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from tests.test_consumer_curation import _crash_import; "
            "import sys; _crash_import(Path(sys.argv[1]), sys.argv[2])",
            str(tmp_path),
            boundary,
        ],
        env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1])),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 78, "curation child missed durable boundary: " + child.stderr
    document = json.loads((tmp_path / "curation-input.json").read_text())
    binding = binding_from_document(document["binding"])
    session = LocalSession(
        binding.namespace_root, binding.castle_root, binding.root / "namespace.lock"
    )
    recovery = recover_namespace(binding.namespace_root, binding.castle_root, session, None)
    bound = open_bound_shelf(replace(binding, ownership="owned"), session)
    values = document["request"]
    action = values["action"]
    entries = []
    for row in action["entries"]:
        row["data"] = bytes.fromhex(row["data"]) if row["data"] is not None else None
        row["tags"], row["dates"] = tuple(row["tags"]), tuple(row["dates"])
        entries.append(ImportItem(**row))
    request = CurationRequest(
        values["operation_id"],
        values["expected_revision"],
        ImportFolder(action["parent_id"], tuple(entries)),
    )
    try:
        result = bound.curate(request)
        assert (
            recovery.state,
            result.replayed,
            bound.view.revision,
            [bound.resolve(row.entry_id).kind for row in entries],
            [bound.download(row.entry_id) for row in entries if row.data is not None],
        ) == (
            "ready",
            True,
            1,
            ["directory", "directory", "file", "file"],
            [b"one", b"two"],
        ), "process interruption accepted a partial import or reminted original work"
    finally:
        bound.close()


def test_original_request_replays_after_restart_and_changed_requests_never_remint(
    tmp_path: Path,
) -> None:
    fixture = consumer_fixture(tmp_path)
    request = fixture.request()
    try:
        first = fixture.bound.curate(request)
        entry_id = first.mutation.affected_entry_ids[0]
        fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                RelocateEntry(entry_id, fixture.root_id, "moved.txt"),
            )
        )
        bound = fixture.reopen()
        replay = bound.curate(request)
        before = inventory(fixture.binding.root)
        action = request.action
        changes = (
            replace(request, expected_revision=request.expected_revision + 1),
            replace(request, action=replace(action, data=b"changed")),
            replace(request, action=replace(action, name="other.txt")),
            replace(request, action=replace(action, parent_id=identifier())),
            replace(request, action=replace(action, tags=("changed",))),
            replace(request, action=CreateDirectory(fixture.root_id, "other")),
        )
        errors = []
        for changed in changes:
            with pytest.raises(ConsumerError) as refused:
                bound.curate(changed)
            errors.append(refused.value.code)
        assert (
            replay.replayed,
            replay.mutation,
            errors,
            bound.resolve(entry_id).path,
            bound.download(entry_id),
            inventory(fixture.binding.root),
        ) == (
            True,
            first.mutation,
            ["namespace-operation-conflict"] * 6,
            "moved.txt",
            b"fixture body",
            before,
        ), "caller retry changed durable identity/result or restaged a changed request"
    finally:
        fixture.close()


def test_frozen_import_and_concurrent_revision_admission_are_atomic(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    folder, empty, one, two = (identifier() for _ in range(4))
    try:
        fixture.bound.curate(
            CurationRequest(
                identifier(),
                0,
                ImportFolder(
                    fixture.root_id,
                    (
                        ImportItem(folder, fixture.root_id, "folder"),
                        ImportItem(empty, folder, "empty"),
                        ImportItem(one, folder, "one.txt", b"one", "text/plain", ADDED),
                        ImportItem(two, folder, "two.txt", b"two", "text/plain", ADDED),
                    ),
                ),
            )
        )
        barrier = Barrier(2, timeout=10)
        revision = fixture.bound.view.revision

        def attempt(index: int):
            request = CurationRequest(
                identifier(),
                revision,
                CreateFile(
                    folder,
                    f"race-{index}.txt",
                    f"race-{index}".encode(),
                    "text/plain",
                    ADDED,
                ),
            )
            barrier.wait()
            try:
                return index, fixture.bound.curate(request).mutation.revision_after
            except ConsumerError as error:
                return index, error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(attempt, range(2)))
        loser = next(index for index, outcome in results if isinstance(outcome, str))
        rejected_digest = hashlib.sha256(f"race-{loser}".encode()).hexdigest()
        object_names = "\n".join(
            str(file.relative_to(fixture.binding.castle_root))
            for file in fixture.binding.castle_root.rglob("*")
            if file.is_file()
        )
        assert (
            fixture.bound.resolve(empty).kind,
            fixture.bound.download(one),
            fixture.bound.download(two),
            sorted(str(outcome) for _, outcome in results),
            fixture.bound.view.revision,
            rejected_digest in object_names,
        ) == ("directory", b"one", b"two", ["2", "namespace-revision-conflict"], 2, False), (
            "frozen import or stale concurrent request published partial/unadmitted content"
        )
    finally:
        fixture.close()


def test_declared_import_envelope_copy_and_overlimit_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixture = consumer_fixture(tmp_path)
    settings = replace(fixture.settings, max_file_bytes=4)
    monkeypatch.setattr(curation_module, "_MIN_PAYLOAD_BYTES", 64)
    folders = [identifier() for _ in range(100)]
    # Scale bytes only; preserve the full declared file/folder count boundary.
    payload = b"xxxx"
    items = [
        ImportItem(key, fixture.root_id if i == 0 else folders[0], f"dir-{i}")
        for i, key in enumerate(folders)
    ]
    items += [
        ImportItem(
            identifier(),
            folders[0],
            "n" * 251 + ".txt" if i == 0 else f"file-{i}",
            payload if i < 16 else b"",
            "text/plain",
            ADDED,
        )
        for i in range(100)
    ]
    try:
        fixture.bound.curate(
            CurationRequest(identifier(), 0, ImportFolder(fixture.root_id, tuple(items))),
            settings=settings,
        )
        copy = fixture.bound.copy_request(
            operation_id=identifier(),
            expected_revision=1,
            entry_id=folders[0],
            parent_id=fixture.root_id,
            name="copy",
        )
        fixture.bound.curate(copy, settings=settings)
        before = inventory(fixture.binding.root)
        cases = (
            CreateFile(fixture.root_id, "too-large", payload + b"x", "text/plain", ADDED),
            CreateDirectory(fixture.root_id, "n" * 256),
            ImportFolder(
                fixture.root_id,
                tuple(ImportItem(identifier(), fixture.root_id, f"x-{i}") for i in range(101)),
            ),
            ImportFolder(
                fixture.root_id,
                tuple(
                    ImportItem(identifier(), fixture.root_id, f"x-{i}", b"", "text/plain", ADDED)
                    for i in range(101)
                ),
            ),
            ImportFolder(
                fixture.root_id,
                tuple(
                    ImportItem(
                        identifier(), fixture.root_id, f"x-{i}", payload, "text/plain", ADDED
                    )
                    for i in range(17)
                ),
            ),
        )
        effects = []
        for action in cases:
            with pytest.raises(ConsumerError) as refused:
                fixture.bound.curate(CurationRequest(identifier(), 2, action), settings=settings)
            effects.append(refused.value.effect)
        original_ids = {item.entry_id for item in items if item.data is not None}
        copied_ids = {entry.entry_id for entry in fixture.bound.files()} - original_ids
        assert (len(original_ids), len(copied_ids), effects, inventory(fixture.binding.root)) == (
            100,
            100,
            ["none"] * 5,
            before,
        ), "consumer envelope narrowed or overlimit/copy changed unadmitted storage"
    finally:
        fixture.close()


def test_configured_source_limit_reaches_create_replace_import_and_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.support import write_settings

    fixture = consumer_fixture(tmp_path)
    settings = write_settings(
        tmp_path / "limits",
        "palace:\n  embedding:\n    provider: ollama\nbiblios:\n  max_file_bytes: 4\n",
    )
    # Exercise aggregate uplift with bytes, rather than allocating a large upload.
    monkeypatch.setattr(curation_module, "_MIN_PAYLOAD_BYTES", 2)
    try:
        created = fixture.request(data=b"1234")
        result = fixture.bound.curate(created, settings=settings)
        entry = result.mutation.affected_entry_ids[0]
        replaced = CurationRequest(identifier(), 1, ReplaceContent(entry, b"4321"))
        fixture.bound.curate(replaced, settings=settings)
        imported = identifier()
        fixture.bound.curate(
            CurationRequest(
                identifier(),
                2,
                ImportFolder(
                    fixture.root_id,
                    (
                        ImportItem(
                            imported, fixture.root_id, "imported", b"abcd", "text/plain", ADDED
                        ),
                    ),
                ),
            ),
            settings=settings,
        )
        copy = fixture.bound.copy_request(
            operation_id=identifier(),
            expected_revision=3,
            entry_id=imported,
            parent_id=fixture.root_id,
            name="copy",
        )
        copied = fixture.bound.curate(copy, settings=settings).mutation.affected_entry_ids[0]
        assert (
            fixture.bound.download(entry),
            fixture.bound.download(imported),
            fixture.bound.download(copied),
        ) == (b"4321", b"abcd", b"abcd"), "configured allowance did not reach all curation paths"
        before = inventory(fixture.binding.root)
        actions = (
            fixture.request(name="large", data=b"12345").action,
            ReplaceContent(entry, b"12345"),
            ImportFolder(
                fixture.root_id,
                (
                    ImportItem(
                        identifier(), fixture.root_id, "large", b"12345", "text/plain", ADDED
                    ),
                ),
            ),
            ImportFolder(
                fixture.root_id,
                tuple(
                    ImportItem(
                        identifier(), fixture.root_id, f"batch-{i}", b"abc", "text/plain", ADDED
                    )
                    for i in range(2)
                ),
            ),
        )
        codes = []
        for action in actions:
            with pytest.raises(ConsumerError) as refused:
                fixture.bound.curate(CurationRequest(identifier(), 4, action), settings=settings)
            codes.append((refused.value.code, refused.value.effect))
        smaller = replace(settings, max_file_bytes=3)
        monkeypatch.setattr(curation_module, "_MIN_PAYLOAD_BYTES", 8)
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.curate(
                fixture.bound.copy_request(
                    operation_id=identifier(),
                    expected_revision=4,
                    entry_id=entry,
                    parent_id=fixture.root_id,
                    name="refused-copy",
                ),
                settings=smaller,
            )
        codes.append((refused.value.code, refused.value.effect))
        assert (codes, inventory(fixture.binding.root)) == (
            [("payload-limit", "none")] * 3 + [("inventory-limit", "none")] * 2,
            before,
        ), "configured per-file or aggregate refusal mutated storage or lost its safe error"
    finally:
        fixture.close()


def test_observer_failure_preserves_commit_and_original_retry(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    request = fixture.request()

    def failed(observation):
        raise RuntimeError("source-name-path-query-marker")

    try:
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.curate(request, observer=failed)
        saved = fixture.bound.operation(request.operation_id)
        replay = fixture.bound.curate(request)
        assert (
            refused.value.code,
            refused.value.effect,
            refused.value.observation.operation_id,
            saved.state,
            replay.replayed,
            fixture.bound.view.revision,
            "marker" in str(refused.value),
        ) == (
            "callback-failed",
            "namespace-committed",
            request.operation_id,
            "committed",
            True,
            1,
            False,
        ), "observer failure hid or duplicated a committed namespace mutation"
    finally:
        fixture.close()


def test_unknown_dispatch_and_failed_completion_never_report_no_effect(
    tmp_path: Path, monkeypatch
) -> None:
    fixture = consumer_fixture(tmp_path)
    original = curation_module.execute_namespace
    calls = []

    def dispatched(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        raise RuntimeError("unsafe-provider-marker")

    try:
        monkeypatch.setattr(curation_module, "execute_namespace", dispatched)
        request = fixture.request()
        with pytest.raises(ConsumerError) as uncertain:
            fixture.bound.curate(request)
        monkeypatch.setattr(curation_module, "execute_namespace", original)
        replay = fixture.bound.curate(request)
        admit = fixture.bound._admit

        def completion(*, write=False):
            if not write:
                raise ConsumerError("test-local-failure", stage="binding")
            return admit(write=write)

        second = fixture.request("second.txt", b"second")
        monkeypatch.setattr(fixture.bound, "_admit", completion)
        with pytest.raises(ConsumerError) as committed:
            fixture.bound.curate(second)
        monkeypatch.setattr(fixture.bound, "_admit", admit)
        status = fixture.bound.operation(second.operation_id)
        assert (
            len(calls),
            uncertain.value.effect,
            replay.replayed,
            committed.value.effect,
            status.state,
            fixture.bound.view.revision,
        ) == (
            1,
            "uncertain",
            True,
            "namespace-committed",
            "committed",
            2,
        ), "post-dispatch or post-commit failure falsely reported no durable effect"
    finally:
        fixture.close()
