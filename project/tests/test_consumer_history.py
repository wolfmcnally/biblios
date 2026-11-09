"""Exact retained bytes and real Castle cutover, never a fabricated recovery receipt."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from castle.namespace_environment import LocalSession

from biblios.binding import open_bound_shelf
from biblios.control import ConsumerError
from biblios.curation import (
    CreateDirectory,
    CurationRequest,
    DeleteEntry,
    ReplaceContent,
    TakeSnapshot,
)
from biblios.history import ExcisionRequest, ExcisionSelection, SnapshotAction, recover_excision
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import binding_from_document, consumer_fixture, identifier, inventory


def test_retained_references_and_snapshot_keep_exact_old_bytes_after_deletion(
    tmp_path: Path,
) -> None:
    fixture = consumer_fixture(tmp_path)
    try:
        first = fixture.bound.curate(fixture.request(data=b"old source bytes"))
        entry_id = first.mutation.affected_entry_ids[0]
        reference = next(
            item
            for item in fixture.bound.retained()
            if item.owner_kind == "entry" and item.owner_id == entry_id
        )
        captured = fixture.bound.curate(
            CurationRequest(identifier(), fixture.bound.view.revision, TakeSnapshot())
        ).mutation.snapshot
        fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                ReplaceContent(entry_id, b"new source bytes"),
            )
        )
        fixture.bound.curate(
            CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(entry_id))
        )
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.read_retained(replace(reference, epoch_id=identifier()))
        inspected = fixture.bound.snapshot(captured.snapshot_id)
        assert (
            fixture.bound.read_retained(reference),
            refused.value.code,
            inspected.snapshot,
            fixture.bound.files(),
        ) == (
            b"old source bytes",
            "retained-reference-invalid",
            captured,
            (),
        ), "retained reference lost exact historical bytes or escaped active binding"
    finally:
        fixture.close()


def test_metadata_only_cutover_preserves_downloads_and_requires_explicit_successor_open(
    tmp_path: Path,
) -> None:
    fixture = consumer_fixture(tmp_path)
    successor_bound = None
    try:
        chosen = fixture.bound.curate(fixture.request(data=b"selected metadata only"))
        selected_id = chosen.mutation.affected_entry_ids[0]
        survivor = fixture.bound.curate(fixture.request("survivor.txt", b"survivor bytes"))
        survivor_id = survivor.mutation.affected_entry_ids[0]
        request = ExcisionRequest(
            identifier(),
            fixture.bound.view.revision,
            ExcisionSelection(metadata_entry_ids=(selected_id,)),
            (),
            fixture.binding.root / "successor",
        )
        proposed = fixture.bound.preview_excision(request)
        if proposed.request != request:
            with pytest.raises(ConsumerError, match="closure-not-accepted"):
                fixture.bound.excise(request)
        assert proposed.request is not None, (
            "real metadata-only cutover has unresolved source conflicts"
        )
        result = fixture.bound.excise(proposed.request)
        with pytest.raises(ConsumerError) as old:
            fixture.bound.files()
        declared = replace(result.successor_binding, ownership="owned")
        session = LocalSession(
            declared.namespace_root, declared.castle_root, declared.root / "namespace.lock"
        )
        successor_bound = open_bound_shelf(declared, session)
        assert (
            old.value.code,
            result.service_resumed,
            successor_bound.resolve(selected_id).metadata,
            successor_bound.download(selected_id),
            successor_bound.download(survivor_id),
            successor_bound.resolve(survivor_id).metadata.media_type,
        ) == (
            "closed",
            False,
            None,
            b"selected metadata only",
            b"survivor bytes",
            "text/plain",
        ), "cutover restored redacted metadata, lost survivors, or implicitly resumed service"
    finally:
        if successor_bound is not None:
            successor_bound.close()
        fixture.close()


def _crash_cutover(directory: Path, boundary: str) -> None:
    """Disposable child only; interrupt actual persisted stages without fake receipts."""
    import castle.namespace_excision as castle_excision

    import biblios.history as history

    fixture = consumer_fixture(directory)
    chosen = fixture.bound.curate(fixture.request("selected.txt", b"selected-secret-marker"))
    survivor = fixture.bound.curate(fixture.request("survivor.txt", b"surviving lighthouse"))
    fixture.bound._shelf.reconcile(fixture.settings, embedder=HashEmbedder())
    selected_id = chosen.mutation.affected_entry_ids[0]
    survivor_id = survivor.mutation.affected_entry_ids[0]
    request = ExcisionRequest(
        identifier(),
        fixture.bound.view.revision,
        ExcisionSelection(entry_ids=(selected_id,)),
        (),
        fixture.binding.root / "successor",
    )
    proposed = fixture.bound.preview_excision(request)
    assert proposed.request is not None
    request = proposed.request
    payload = {
        "binding": asdict(fixture.binding),
        "request": asdict(request),
        "selected": selected_id,
        "survivor": survivor_id,
    }
    (directory / "recovery-input.json").write_text(json.dumps(payload, default=str))

    def interrupt(original):
        def run(*args, **kwargs):
            result = original(*args, **kwargs)
            os._exit(77)
            return result

        return run

    if boundary == "cas-retired":
        castle_excision._cas_transition = interrupt(castle_excision._cas_transition)
    elif boundary == "derived-pending":
        history._disposition = lambda *args: os._exit(77)
    elif boundary == "row-deleted":
        history.delete_path = interrupt(history.delete_path)
    else:
        raise AssertionError("unknown bounded interruption")
    fixture.bound.excise(request)
    raise AssertionError("actual interruption boundary was not reached")


def test_shared_deleted_snapshot_and_name_only_closure_is_explicit(tmp_path: Path) -> None:
    fixture = consumer_fixture(tmp_path)
    reader = None
    try:

        def create(name, data):
            return fixture.bound.curate(fixture.request(name, data)).mutation.affected_entry_ids[0]

        selected = create("selected.txt", b"selected-version-unique-marker")
        shared = create("shared.txt", b"selected-version-unique-marker")
        content_id = fixture.bound.resolve(selected).cas_id
        snapshot = fixture.bound.curate(
            CurationRequest(identifier(), fixture.bound.view.revision, TakeSnapshot())
        ).mutation.snapshot
        fixture.bound.curate(
            CurationRequest(
                identifier(),
                fixture.bound.view.revision,
                ReplaceContent(selected, b"other-deleted-version"),
            )
        )
        fixture.bound.curate(
            CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(selected))
        )
        name_only = create("name-only.txt", b"")
        survivor = create("surviving.txt", b"unselected exact survivor")
        empty_dir = fixture.bound.curate(
            CurationRequest(
                identifier(), fixture.bound.view.revision, CreateDirectory(fixture.root_id, "empty")
            )
        ).mutation.affected_entry_ids[0]
        request = ExcisionRequest(
            identifier(),
            fixture.bound.view.revision,
            ExcisionSelection(content_ids=(content_id,), entry_ids=(selected,)),
            (SnapshotAction(snapshot.snapshot_id, "drop"),),
            fixture.binding.root / "successor",
        )
        before = inventory(fixture.binding.root)
        with pytest.raises(ConsumerError, match="namespace-dependency-conflict"):
            fixture.bound.preview_excision(request)
        assert inventory(fixture.binding.root) == before, "incomplete shared closure had effects"
        complete = replace(
            request,
            selection=ExcisionSelection(
                content_ids=(content_id, snapshot.cas_id),
                entry_ids=(selected, shared, empty_dir),
                metadata_entry_ids=(name_only,),
            ),
        )
        proposed = fixture.bound.preview_excision(complete)
        assert proposed.request is not None, "explicit shared/snapshot closure remained unresolved"
        result = fixture.bound.excise(proposed.request)
        declared = replace(result.successor_binding, ownership="owned")
        reader = open_bound_shelf(
            declared,
            LocalSession(
                declared.namespace_root, declared.castle_root, declared.root / "namespace.lock"
            ),
        )
        retained_ids = {row.cas_id for row in reader.retained()}
        assert (
            reader.download(survivor),
            reader.download(name_only),
            reader.resolve(name_only).metadata,
            {entry.entry_id for entry in reader.files()},
            content_id in retained_ids,
            snapshot.cas_id in retained_ids,
        ) == (b"unselected exact survivor", b"", None, {survivor, name_only}, False, False), (
            "shared closure kept selected history or lost unselected/name-only bytes"
        )
    finally:
        if reader is not None:
            reader.close()
        fixture.close()


@pytest.mark.parametrize("boundary", ("cas-retired", "derived-pending", "row-deleted"))
def test_real_process_interruption_requires_exact_recovery_and_preserves_indexed_survivor(
    tmp_path: Path,
    boundary: str,
) -> None:
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; "
            "from tests.test_consumer_history import _crash_cutover; "
            "import sys; _crash_cutover(Path(sys.argv[1]), sys.argv[2])",
            str(tmp_path),
            boundary,
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert child.returncode == 77, "durable cutover child missed its real boundary: " + child.stderr
    payload = json.loads((tmp_path / "recovery-input.json").read_text())
    binding = binding_from_document(payload["binding"])
    values = payload["request"]
    values["selection"] = ExcisionSelection(
        **{key: tuple(value) for key, value in values["selection"].items()}
    )
    values["snapshot_actions"] = ()
    values["successor_root"] = Path(values["successor_root"])
    request = ExcisionRequest(**values)
    session = LocalSession(
        binding.namespace_root, binding.castle_root, binding.root / "namespace.lock"
    )
    before = inventory(tmp_path)
    with pytest.raises(ConsumerError, match="recovery-binding-mismatch"):
        recover_excision(binding, session, replace(request, operation_id=identifier()))
    assert inventory(tmp_path) == before, "mismatched recovery changed durable storage"
    result = recover_excision(binding, session, request)
    successor = replace(result.successor_binding, ownership="owned")
    reader = open_bound_shelf(
        successor,
        LocalSession(
            successor.namespace_root, successor.castle_root, successor.root / "namespace.lock"
        ),
    )
    try:
        with pytest.raises(ConsumerError, match="read-failed"):
            reader.resolve(payload["selected"])
        search = reader.search("lighthouse", embedder=HashEmbedder(), reranker=OverlapReranker())
        database = (binding.palace_root / "index" / "chunks.sqlite").read_bytes()
        assert (
            reader.download(payload["survivor"]),
            {hit.entry_id for hit in search.hits},
            result.service_resumed,
            (binding.mirror_root / (payload["selected"] + ".md")).exists(),
            b"selected-secret-marker" in database,
            json.loads((binding.palace_root / "consumer-excision.json").read_text())["state"],
        ) == (b"surviving lighthouse", {payload["survivor"]}, False, False, False, "complete"), (
            "recovery exposed selected data or lost an indexed survivor"
        )
    finally:
        reader.close()
