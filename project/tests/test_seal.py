"""Seal, the read-only open, verification of a copy, and unseal."""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path

import pytest
from castle.namespace import MutationRequest, NamespaceError, execute_namespace
from palace.index import IndexError as PalaceIndexError

from biblios import (
    BibliosError,
    IndexLagError,
    SealedShelfError,
    open_shelf,
    open_stack,
    seal,
    unseal,
    verify_seal,
)
from biblios.progress import EntryProgress, read_watermark, recorded_root
from biblios.reconcile import reconcile
from biblios.seal import DIGESTED
from biblios.shelf import MANIFEST_NAME, SEAL_NAME, ShelfManifest
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import GUIDE, LONG, NOTES, TIDE, make_shelf


def _journal(db: Path) -> str:
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as conn:
        return conn.execute("PRAGMA journal_mode").fetchone()[0]


def _listing(root: Path, *, runtime: bool = True) -> dict[str, tuple[int, int]]:
    """Every file's size and mtime; ``runtime=False`` leaves out WAL sidecars, which a
    dynamic shelf's readers touch by design."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in root.rglob("*")
        if p.is_file() and (runtime or not (p.name.endswith("-shm") or p.name.endswith("-wal")))
    }


def _sidecars(db: Path) -> list[str]:
    return [
        p.name
        for p in (db.with_name(db.name + "-wal"), db.with_name(db.name + "-shm"))
        if p.exists()
    ]


def test_seal_is_refused_before_any_write_when_the_index_lags_or_the_stamp_disagrees(
    tmp_path: Path,
) -> None:
    """Failure mode: a seal writes part of its state before a precondition fails, ships a
    lagging index, or interleaves with a running step."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        with pytest.raises(BibliosError, match="never been built"):
            seal(shelf)
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        manifest_bytes = (shelf.root / MANIFEST_NAME).read_bytes()
        assert _journal(shelf.chunks_db) == "wal"
        shelf.add("guide.md", GUIDE.encode(), media_type="text/markdown")
        before = _listing(shelf.root, runtime=False)
        with pytest.raises(IndexLagError, match="1 pending"):
            seal(shelf)
        assert _listing(shelf.root, runtime=False) == before and not shelf.sealed

        refusals: list[str] = []

        def progress(row: EntryProgress) -> None:
            if row.stage == "converting" and row.path == "guide.md":
                with pytest.raises(IndexLagError) as refused:
                    seal(shelf)
                refusals.append(str(refused.value))

        report = shelf.reconcile(settings, embedder=embedder, progress=progress)
        assert report.indexed and refusals and "pending" in refusals[0]
        assert not shelf.index_status().lagging

        selector = shelf.palace_store / "meta" / "embedder.toml"
        original = selector.read_text()
        selector.write_text(original.replace("ollama", "openrouter"))
        before = _listing(shelf.root, runtime=False)
        with pytest.raises(PalaceIndexError):
            seal(shelf)
        selector.write_text(original)
        after = _listing(shelf.root, runtime=False)
        assert {k: v for k, v in after.items() if "embedder.toml" not in k} == {
            k: v for k, v in before.items() if "embedder.toml" not in k
        }
        assert (shelf.root / MANIFEST_NAME).read_bytes() == manifest_bytes
        assert _journal(shelf.chunks_db) == "wal" and not (shelf.root / SEAL_NAME).exists()


def test_a_failed_seal_leaves_a_usable_or_repairable_shelf(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: a mid-sequence failure leaves a shelf that neither opens nor unseals."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        watermark = read_watermark(shelf)
        real_replace = os.replace

        def failing_on(name: str):
            def replace(src: object, dst: object) -> None:
                if Path(str(dst)).name == name:
                    raise OSError("disk went away")
                real_replace(src, dst)

            return replace

        # Before the manifest is published: the shelf stays dynamic and usable.
        monkeypatch.setattr(os, "replace", failing_on(MANIFEST_NAME))
        with pytest.raises(OSError, match="disk went away"):
            seal(shelf)
        monkeypatch.setattr(os, "replace", real_replace)
        assert not shelf.sealed and _journal(shelf.chunks_db) == "wal"
        assert read_watermark(shelf) == watermark and not (shelf.root / SEAL_NAME).exists()
        assert not any(p.name.endswith(".tmp") for p in shelf.root.iterdir())
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)

        # Between the manifest and the record: an incomplete seal, refused and repairable.
        monkeypatch.setattr(os, "replace", failing_on(SEAL_NAME))
        with pytest.raises(OSError, match="disk went away"):
            seal(shelf)
        monkeypatch.setattr(os, "replace", real_replace)
        # The open object is sealed from the manifest's publication, record or no record.
        assert shelf.sealed, "the object is not sealed after its manifest was published"
        for call in (
            lambda: shelf.add("x.txt", b"x"),
            lambda: shelf.reconcile(settings, embedder=embedder),
        ):
            with pytest.raises(SealedShelfError):
                call()
        with pytest.raises(BibliosError, match="already sealed"):
            seal(shelf)
        assert not verify_seal(shelf).ok
    root = shelf.root
    assert not (root / SEAL_NAME).exists()
    with pytest.raises(BibliosError, match="incomplete seal"):
        open_shelf(root)
    with unseal(root) as repaired:
        assert not repaired.sealed and _journal(repaired.chunks_db) == "wal"
        repaired.add("guide.md", GUIDE.encode(), media_type="text/markdown")
        repaired.reconcile(settings, embedder=embedder)
        # The incomplete seal already published generation 1; the repaired seal is the second.
        assert seal(repaired).generation == 2


def test_sealed_shelf_has_no_sidecars_refuses_every_write_and_verifies_after_copy(
    tmp_path: Path,
) -> None:
    """Failure mode: a sidecar remains, a write path escapes the guard, or verification
    passes on a changed artifact."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        add_id = str(uuid.uuid4())
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain", operation_id=add_id)
        shelf.reconcile(settings, embedder=embedder)
        record = seal(shelf)
        assert shelf.sealed and shelf.manifest.generation == 1
        assert _sidecars(shelf.chunks_db) == [] and _journal(shelf.chunks_db) == "delete"
        assert set(record.digests) == set(DIGESTED)
        assert record.sealed_revision == record.snapshot.captured_revision + 1 == shelf.revision
        view = shelf._read("inspect-snapshot", {"snapshot_id": record.snapshot.snapshot_id})
        assert view.snapshot == record.snapshot
        before = _listing(shelf.root)
        for call in (
            lambda: shelf.add("x.txt", b"x"),
            lambda: shelf.update("notes.txt", b"y"),
            lambda: shelf.remove("notes.txt"),
            lambda: shelf.rename("notes.txt", "z.txt"),
            lambda: shelf.set_tags("notes.txt", ("t",)),
            lambda: shelf.add(
                "tide.txt", TIDE.encode(), media_type="text/plain", operation_id=add_id
            ),
            lambda: shelf.reconcile(settings, embedder=embedder),
            lambda: reconcile(shelf, settings, embedder=embedder),
        ):
            with pytest.raises(SealedShelfError):
                call()
        assert _listing(shelf.root) == before
        # Another reader is admitted while the sealing handle is still open.
        with open_shelf(shelf.root) as reader:
            assert reader.sealed and [e.path for e in reader.ls()] == ["notes.txt", "tide.txt"]
            with pytest.raises(SealedShelfError):
                reader.add("x.txt", b"x")
            info = reader._read("info", {})
            with pytest.raises(NamespaceError, match="namespace-access-denied"):
                execute_namespace(
                    reader._handle,
                    MutationRequest(
                        info.namespace_id,
                        info.cas,
                        info.revision,
                        str(uuid.uuid4()),
                        "create-directory",
                        {"parent_id": info.root_id, "name": "x"},
                    ),
                    {},
                )
        assert verify_seal(shelf).ok and shelf.verify().ok

    copy = tmp_path / "copy"
    shutil.copytree(shelf.root, copy)
    with open_shelf(copy) as copied:
        assert copied.sealed and verify_seal(copied).ok, verify_seal(copied).problems
        assert copied.resolve(notes.entry_id).path == "notes.txt"
    db = copy / "palace" / "index" / "chunks.sqlite"
    data = bytearray(db.read_bytes())
    data[-1] ^= 0xFF
    db.write_bytes(bytes(data))
    (copy / MANIFEST_NAME).write_text(
        (copy / MANIFEST_NAME).read_text().replace('"name": "test shelf"', '"name": "other"')
    )
    with open_shelf(copy) as tampered:
        problems = verify_seal(tampered).problems
        assert any("chunks.sqlite" in p for p in problems) and any(
            "shelf.json" in p for p in problems
        )


def test_a_copied_sealed_shelf_answers_search_and_citations_without_writing(tmp_path: Path) -> None:
    """Failure mode: the copy refuses hits because of the recorded watch root, or a read
    creates or changes a file under the artifact."""
    shelf, settings = make_shelf(tmp_path)
    embedder, reranker = HashEmbedder(), OverlapReranker()
    with shelf:
        shelf.mkdir("docs")
        notes = shelf.add("docs/notes.txt", NOTES.encode(), media_type="text/plain")
        guide = shelf.add("guide.md", GUIDE.encode(), media_type="text/markdown")
        shelf.reconcile(settings, embedder=embedder)
        seal(shelf)
    copy = tmp_path / "elsewhere" / "shelf-copy"
    shutil.copytree(shelf.root, copy)
    before = _listing(copy)
    with open_shelf(copy) as copied:
        assert recorded_root(copied) != str(copied.mirror_root.resolve())
        found = copied.search("fresnel lens kerosene", embedder=embedder, reranker=reranker)
        assert found.hits[0].entry_id == notes.entry_id and found.hits[0].path == "docs/notes.txt"
        assert not found.indexing and found.refused == ()
        assert "second section" in copied.read(guide.entry_id, page=2).text
        assert copied.download(notes.entry_id) == NOTES.encode()
        stacked = open_stack([copied]).search("bowline fixed loop", embedder=embedder, rerank=False)
        assert stacked.hits[0].entry_id == guide.entry_id and stacked.indexing == ()
        assert copied.verify().ok, copied.verify().problems
    assert _listing(copy) == before


def test_unseal_returns_to_dynamic_and_a_moved_copy_is_rebuilt(tmp_path: Path) -> None:
    """Failure mode: unseal leaves the store read-only or on the rollback journal, or a
    moved store keeps rows under a foreign root."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        record = seal(shelf)
    copy = tmp_path / "moved"
    shutil.copytree(shelf.root, copy)

    with unseal(shelf.root) as original:
        assert not original.sealed and _journal(original.chunks_db) == "wal"
        assert not (original.root / SEAL_NAME).exists() and original.manifest.generation == 1
        view = original._read("inspect-snapshot", {"snapshot_id": record.snapshot.snapshot_id})
        assert view.snapshot == record.snapshot
        tide = original.add("tide.txt", TIDE.encode(), media_type="text/plain")
        report = original.reconcile(settings, embedder=embedder)
        assert report.indexed == (tide.entry_id,) and not original.index_status().lagging

    with unseal(copy) as moved:
        assert recorded_root(moved) != str(moved.mirror_root.resolve())
        moved.add("long.md", LONG.encode(), media_type="text/markdown")
        report = moved.reconcile(settings, embedder=embedder)
        assert report.built and recorded_root(moved) == str(moved.mirror_root.resolve())
        found = moved.search("fresnel lens kerosene", embedder=embedder, rerank=False)
        assert found.hits[0].entry_id == notes.entry_id and not found.indexing
        assert moved.verify().ok, moved.verify().problems


def test_a_step_or_an_opener_racing_a_seal_cannot_write_into_the_sealed_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: a step admitted while the shelf was dynamic resumes after the seal and
    rebuilds the sealed store, or an opener that read a dynamic manifest returns a writable
    handle over the sealed directory."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        seal(shelf)
    copy = tmp_path / "copy"
    shutil.copytree(shelf.root, copy)

    class PausingEmbedder(HashEmbedder):
        """Pauses inside the probe that precedes a step, after the step was admitted."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = threading.Event()
            self.resume = threading.Event()

        def probe(self) -> None:
            self.entered.set()
            assert self.resume.wait(60), "the paused step was never resumed"
            super().probe()

    def digests(root: Path) -> dict[str, str]:
        return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in DIGESTED}

    # A copy unsealed at a new location is current, so a seal admits it; the step admitted
    # just before, which would rebuild the moved store, must refuse once it resumes.
    pausing = PausingEmbedder()
    outcome: dict[str, BaseException | None] = {"error": None}

    def step(moved) -> None:
        try:
            moved.reconcile(settings, embedder=pausing)
        except BaseException as error:  # noqa: BLE001 - the proof inspects it
            outcome["error"] = error

    with unseal(copy) as moved:
        assert recorded_root(moved) != str(moved.mirror_root.resolve())
        worker = threading.Thread(target=step, args=(moved,))
        worker.start()
        try:
            assert pausing.entered.wait(60)
            record = seal(moved)
            sealed = digests(copy)
        finally:
            pausing.resume.set()
            worker.join(60)
        assert not worker.is_alive()
        assert isinstance(outcome["error"], SealedShelfError), (
            "a step admitted before the seal wrote after it: " + repr(outcome["error"])
        )
        assert digests(copy) == sealed == record.digests
        assert _sidecars(moved.chunks_db) == [] and verify_seal(moved).ok

    # An opener that read a dynamic manifest, then lost the race to a seal, must come back
    # with the sealed mode and a read-only handle, not a writable handle over the artifact.
    other, _ = make_shelf(tmp_path / "other")
    other.add("tide.txt", TIDE.encode(), media_type="text/plain")
    other.reconcile(settings, embedder=embedder)
    real_read = ShelfManifest.read
    entered, resume = threading.Event(), threading.Event()
    paused: list[int] = []
    opened: dict[str, object] = {}

    def pausing_read(path: Path) -> ShelfManifest:
        manifest = real_read(path)
        if threading.current_thread() is opener and not paused:
            paused.append(1)
            entered.set()
            assert resume.wait(60), "the paused opener was never resumed"
        return manifest

    monkeypatch.setattr(ShelfManifest, "read", staticmethod(pausing_read))
    opener = threading.Thread(target=lambda: opened.__setitem__("shelf", open_shelf(other.root)))
    opener.start()
    try:
        assert entered.wait(60)
        seal(other)
        other.close()
    finally:
        resume.set()
        opener.join(60)
    monkeypatch.setattr(ShelfManifest, "read", real_read)
    late = opened["shelf"]
    with late:
        assert late.sealed, "the opener returned a dynamic view over a sealed directory"
        assert late._handle.read_only
        with pytest.raises(SealedShelfError):
            late.add("x.txt", b"x")
        assert verify_seal(late).ok

    # An open refused after Castle's handle was acquired must release that handle: a
    # dynamic shelf's handle holds the exclusive lock, so a leak would refuse the next open.
    third, _ = make_shelf(tmp_path / "third")
    third.close()
    reads: list[int] = []

    def failing_second_read(path: Path) -> ShelfManifest:
        reads.append(1)
        if len(reads) == 2:
            raise BibliosError("manifest vanished")
        return real_read(path)

    monkeypatch.setattr(ShelfManifest, "read", staticmethod(failing_second_read))
    with pytest.raises(BibliosError, match="manifest vanished"):
        open_shelf(third.root)
    monkeypatch.setattr(ShelfManifest, "read", real_read)
    try:
        reopened = open_shelf(third.root)
    except Exception as error:  # noqa: BLE001 - the proof names the leak
        pytest.fail(f"a refused open leaked its handle: {error!r}")
    with reopened:
        assert not reopened.sealed
        reopened.add("x.txt", b"x")


def test_verification_reports_a_damaged_artifact_and_a_malformed_record_without_raising(
    tmp_path: Path,
) -> None:
    """Failure mode: a corrupted index or a malformed seal record escapes VerifyReport as
    an exception, so the receiver cannot read what is wrong."""
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        seal(shelf)
    copy = tmp_path / "copy"
    shutil.copytree(shelf.root, copy)
    db = copy / "palace" / "index" / "chunks.sqlite"
    intact = db.read_bytes()
    # A readable database that fails Biblios's own single-root validation.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE files SET watch_root = ? WHERE rowid = (SELECT min(rowid) FROM files)",
            ("/elsewhere/mirror",),
        )
    with open_shelf(copy) as two_roots:
        assert not verify_seal(two_roots).ok
        general = two_roots.verify()
    assert not general.ok and any("more than one root" in p for p in general.problems)
    db.write_bytes(intact)
    db.write_bytes(b"\0" * 100 + intact[100:])
    with open_shelf(copy) as damaged:
        report = verify_seal(damaged)
        assert not report.ok
        assert any("chunks.sqlite" in p for p in report.problems)
        assert any("unreadable" in p for p in report.problems), report.problems
        assert not damaged.verify().ok
    db.write_bytes(intact)

    import json

    record_path = copy / SEAL_NAME
    good = json.loads(record_path.read_text())
    for field, value in (("generation", None), ("sealed_revision", "3"), ("snapshot", "12")):
        bad = dict(good)
        if field == "snapshot":
            bad["snapshot"] = {**good["snapshot"], "captured_revision": value}
        else:
            bad[field] = value
        record_path.write_text(json.dumps(bad))
        with open_shelf(copy) as malformed:
            report = verify_seal(malformed)
            assert not report.ok and "seal record" in report.problems[0], report.problems
            assert not malformed.verify().ok
    record_path.write_text(json.dumps(good, indent=2, sort_keys=True) + "\n")
    with open_shelf(copy) as restored:
        assert verify_seal(restored).ok, verify_seal(restored).problems
