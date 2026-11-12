"""Reconcile derives mirrors and the index from the live namespace, and nothing else."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import biblios.representation as convert_module
from biblios import UndeclaredEmbedderError
from biblios.progress import indexed_files
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import NOTES, RECIPE, TIDE, make_shelf


def test_curate_reconcile_search_follows_the_phase_scenario(tmp_path: Path) -> None:
    """Failure mode: a replaced file keeps its old chunks, a renamed file re-embeds or
    loses its hit, a removed file keeps answering, or removed bytes vanish from Castle."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    reranker = OverlapReranker()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        recipe = shelf.add("recipe.txt", RECIPE.encode(), media_type="text/plain")
        first = shelf.reconcile(settings, embedder=embedder)
        assert _searchable(first) == {notes.entry_id, recipe.entry_id}
        assert shelf.verify().ok, shelf.verify().problems

        shelf.update("notes.txt", TIDE.encode())
        assert not shelf.verify().ok
        shelf.rename("recipe.txt", "bread.txt")
        embedder.calls.clear()
        second = shelf.reconcile(settings, embedder=embedder)
        # The replaced entry is a new ingestion. The renamed one is re-indexed under its
        # current name with its mirror untouched: only its name row is embedded again.
        assert _outcomes(second) == {notes.entry_id: "searchable", recipe.entry_id: "searchable"}
        assert {stage.entry_id for stage in second.accepted} == {notes.entry_id, recipe.entry_id}
        renamed = next(item for item in second.entries if item.entry_id == recipe.entry_id)
        assert renamed.embedded == 1
        assert any("bread.txt" in " ".join(call) for call in embedder.calls)
        assert "recipe.txt" in shelf.mirror_path(recipe.entry_id).read_text()
        assert shelf.verify().ok, shelf.verify().problems

        found = shelf.search("neap tides smallest range", embedder=embedder, reranker=reranker)
        assert [hit.entry_id for hit in found.hits][:1] == [notes.entry_id]
        assert found.hits[0].path == "notes.txt" and found.rerank_status == "applied"
        gone = shelf.search("fresnel lens kerosene", embedder=embedder, reranker=reranker)
        assert all(
            hit.entry_id != notes.entry_id or "fresnel" not in hit.chunk for hit in gone.hits
        )

        embedder.calls.clear()
        bread = shelf.search("sourdough starter cold retard", embedder=embedder, reranker=reranker)
        assert bread.hits[0].entry_id == recipe.entry_id and bread.hits[0].path == "bread.txt"
        assert embedder.calls == [("sourdough starter cold retard",)]

        recipe_bytes = shelf.resolve(recipe.entry_id).cas_id
        embedder.calls.clear()
        shelf.remove("bread.txt")
        # remove returns at the namespace commit: the row is derived state and leaves at
        # the next reconcile; meanwhile the dead entry's hits are refused, never returned.
        assert recipe.entry_id in indexed_files(shelf) and embedder.calls == []
        meanwhile = shelf.search("sourdough starter cold retard", embedder=embedder, rerank=False)
        assert recipe.entry_id in {w.entry_id for w in meanwhile.withheld}
        assert all(hit.entry_id != recipe.entry_id for hit in meanwhile.hits)
        from castle import cas

        assert cas.require_object(shelf.castle_root, recipe_bytes).read_bytes() == RECIPE.encode()
        third = shelf.reconcile(settings, embedder=embedder)
        assert _outcomes(third) == {recipe.entry_id: "removed"}  # notes: not a candidate
        assert recipe.entry_id not in indexed_files(shelf)
        assert not shelf.mirror_path(recipe.entry_id).exists()
        after = shelf.search("sourdough starter cold retard", embedder=embedder, reranker=reranker)
        assert all(hit.entry_id != recipe.entry_id for hit in after.hits)
        assert shelf.verify().ok, shelf.verify().problems


def test_unconvertible_and_replaced_by_unconvertible_files_are_marked_and_absent(
    tmp_path: Path,
) -> None:
    """Failure mode: a file Drawbridge defers is indexed anyway, or a replacement by such
    a file leaves the old chunks searchable."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        binary = b"\x00\x01\x02 not text \x00"
        blob = shelf.add("blob.bin", binary)
        text = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        report = shelf.reconcile(settings, embedder=embedder)
        assert _outcomes(report) == {blob.entry_id: "unconverted", text.entry_id: "searchable"}
        assert not shelf.mirror_path(blob.entry_id).exists()
        assert set(indexed_files(shelf)) == {text.entry_id}

        shelf.update("tide.txt", binary + b"\x00more")
        again = shelf.reconcile(settings, embedder=embedder)
        assert _outcomes(again) == {text.entry_id: "unconverted"}
        assert indexed_files(shelf) == {}
        assert shelf.search("neap tides", embedder=embedder, rerank=False).hits == ()
        assert shelf.verify().ok, shelf.verify().problems


def test_identity_discipline_refuses_before_any_write(tmp_path: Path) -> None:
    """Failure mode: a store with no declared selector is built under Palace's default
    identity, or a failing embedder probe leaves a half-written store behind."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        selector = shelf.palace_store / "meta" / "embedder.toml"
        declared = selector.read_text()
        selector.unlink()
        with pytest.raises(UndeclaredEmbedderError):
            shelf.reconcile(settings, embedder=embedder)
        assert not shelf.chunks_db.exists()
        selector.write_text(declared)

        shelf.reconcile(settings, embedder=embedder)
        before = shelf.chunks_db.read_bytes()
        shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        failing = HashEmbedder()
        failing.probe_failure = "embedding service unreachable"
        with pytest.raises(Exception, match="unreachable"):
            shelf.reconcile(settings, embedder=failing)
        assert shelf.chunks_db.read_bytes() == before


def test_interrupted_builds_recover_on_the_next_reconcile(tmp_path: Path) -> None:
    """Failure mode: a database left by an interrupted bootstrap is treated as an index and
    refused forever, or a build interrupted after bootstrap never completes."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        one = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.chunks_db.parent.mkdir(parents=True)
        sqlite3.connect(shelf.chunks_db).close()
        report = shelf.reconcile(settings, embedder=embedder)
        assert _searchable(report) == {one.entry_id} == set(indexed_files(shelf))

        two = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        interrupted = HashEmbedder()

        def embed_never(texts):
            raise RuntimeError("interrupted mid-build")

        interrupted.embed = embed_never  # type: ignore[method-assign]
        # A provider failure is scoped to its entry and reported, never the whole call.
        failed = shelf.reconcile(settings, embedder=interrupted)
        assert _outcomes(failed) == {two.entry_id: "uncertain"}
        shelf.reconcile(settings, embedder=embedder)
        assert set(indexed_files(shelf)) == {one.entry_id, two.entry_id}
        assert shelf.verify().ok, shelf.verify().problems


def test_a_configured_stage_rendering_becomes_the_mirror(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: a stage the composed configuration enabled is suppressed, or its
    rendering is produced and then discarded; or the conversion is handed no OCR backend,
    which leaves every image pending in Drawbridge whatever tools the machine has."""
    from drawbridge import CliOcrBackend
    from drawbridge.documents import FileConversion
    from drawbridge.mirror import Mirror, build_header

    shelf, settings = make_shelf(tmp_path)
    seen: dict[str, object] = {}

    def fake_convert_file(path, *, ocr, options, media_type, stages):
        seen["stages"] = stages
        assert isinstance(ocr, CliOcrBackend)
        header = build_header(Path(path), method="text-decode:utf-8", media_type=media_type)
        fidelity = Mirror(header, "# fidelity\n\n## Page 1\n\nraw words")
        semantic = Mirror(header, "# groomed\n\n## Page 1\n\ngroomed words")
        return FileConversion(
            fidelity, media_type, "text-decode:utf-8", semantic=semantic if stages else None
        )

    monkeypatch.setattr(convert_module, "convert_file", fake_convert_file)
    with shelf:
        entry = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=HashEmbedder())
        assert seen["stages"] is False
        assert "raw words" in shelf.read(entry.entry_id).text

    staged = tmp_path / "staged"
    staged.mkdir()
    (staged / "settings.yaml").write_text(
        "palace:\n  embedding:\n    provider: ollama\n"
        "drawbridge:\n  endpoints:\n    local:\n      kind: openai-compatible\n"
        "      egress: local\n      base_url: http://127.0.0.1:9/v1\n"
        "  stages:\n    structure:\n      endpoint: local\n      model: stub\n"
        "vellric: {}\n"
    )
    from biblios import load_settings

    configured = load_settings([staged / "settings.yaml"], staged)
    with biblios_open(shelf.root) as reopened:
        reopened.update("tide.txt", NOTES.encode())
        reopened.reconcile(configured, embedder=HashEmbedder())
        assert seen["stages"] is True
        page = reopened.read(entry.entry_id)
        assert "groomed words" in page.text


def biblios_open(root: Path):
    from biblios import open_shelf

    return open_shelf(root)


def _outcomes(result) -> dict[str, str]:
    return {item.entry_id: item.outcome for item in result.entries}


def _searchable(result) -> set[str]:
    return {item.entry_id for item in result.entries if item.outcome == "searchable"}
