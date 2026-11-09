"""Search hits carry the citation, refuse dead entries, and rerank only when asked."""

from __future__ import annotations

from pathlib import Path

import pytest

from biblios import BibliosError, UndeclaredEmbedderError
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import GUIDE, NOTES, TIDE, make_shelf


def test_hits_carry_the_citation_and_read_follows_it_to_a_page(tmp_path: Path) -> None:
    """Failure mode: a hit names a mirror path instead of the entry, read cannot select a
    page, or a dead entry's chunk is returned."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        shelf.mkdir("docs")
        notes = shelf.add("docs/notes.txt", NOTES.encode(), media_type="text/plain")
        tide = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)

        result = shelf.search(
            "fresnel lens kerosene", embedder=embedder, reranker=OverlapReranker()
        )
        hit = result.hits[0]
        assert hit.shelf_id == shelf.shelf_id
        assert hit.entry_id == notes.entry_id and hit.path == "docs/notes.txt"
        assert "fresnel" in hit.chunk.lower()
        assert hit.rerank_status == "applied" and hit.rerank_score is not None
        assert result.refused == ()

        whole = shelf.read(notes.entry_id)
        assert "fresnel" in whole.text.lower() and whole.page_count == 0
        guide = shelf.add("guide.md", GUIDE.encode(), media_type="text/markdown")
        shelf.reconcile(settings, embedder=embedder)
        page = shelf.read(guide.entry_id, page=2)
        assert page.page == 2 and page.page_count == 2 and "second section" in page.text
        with pytest.raises(BibliosError, match="no page 3"):
            shelf.read(guide.entry_id, page=3)
        assert shelf.download(notes.entry_id) == NOTES.encode()

        # A chunk whose entry is no longer live is refused, never returned: simulate the
        # index lagging behind the namespace by deleting the entry without its rows.
        shelf._mutate("delete-entry", {"entry_id": tide.entry_id}, operation_id=None)
        lagging = shelf.search("spring tides new moon", embedder=embedder, rerank=False)
        assert tide.entry_id in lagging.refused
        assert all(h.entry_id != tide.entry_id for h in lagging.hits)


def test_rerank_can_be_disabled_or_fail_without_losing_the_fused_order(tmp_path: Path) -> None:
    """Failure mode: disabling rerank still constructs a reranker, or a reranker failure
    raises instead of degrading with a visible status."""
    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        spy = OverlapReranker()
        disabled = shelf.search("spring tides", embedder=embedder, reranker=spy, rerank=False)
        assert disabled.rerank_status == "disabled" and spy.calls == []
        assert disabled.hits and disabled.hits[0].rerank_score is None

        from palace.rerank import RerankUnavailableError

        class Broken:
            def score(self, query, texts):
                raise RerankUnavailableError("reranker down")

            def probe(self):
                return None

            def close(self):
                return None

        failed = shelf.search("spring tides", embedder=embedder, reranker=Broken())
        assert failed.rerank_status == "failed" and failed.hits

        # No declared selector, no search: Palace would otherwise fall back to its default.
        (shelf.palace_store / "meta" / "embedder.toml").unlink()
        probe = HashEmbedder()
        with pytest.raises(UndeclaredEmbedderError):
            shelf.search("spring tides", embedder=probe, reranker=spy, rerank=False)
        assert probe.calls == [] and spy.calls == []
