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
        assert result.withheld == ()

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
        assert tide.entry_id in {w.entry_id for w in lagging.withheld}
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


@pytest.mark.parametrize(
    ("altered", "category"), [("vector", "vector-mismatch"), ("body", "body-mismatch")]
)
def test_a_served_vector_or_body_altered_at_rest_withholds_only_its_entry(
    tmp_path: Path, altered: str, category: str
) -> None:
    """Failure mode: Palace's stored vector (or body, with its own recorded hash kept
    consistent) is changed after acceptance, and the hit is served because only the
    store's self-consistency, not the entry's accepted manifest, was checked."""
    import sqlite3
    import struct

    import sqlite_vec
    from palace.index.representation import input_digest

    shelf, settings = make_shelf(tmp_path)
    embedder = HashEmbedder()
    with shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        tide = shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        assert shelf.search("fresnel lens kerosene", embedder=embedder, rerank=False).hits
        conn = sqlite3.connect(shelf.palace_store / "index" / "chunks.sqlite")
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        with conn:
            for chunk_id, body in conn.execute(
                "SELECT chunk_id, body FROM chunks WHERE path=?", (notes.entry_id + ".md",)
            ).fetchall():
                if altered == "vector":
                    (blob,) = conn.execute(
                        "SELECT embedding FROM chunks_vec WHERE chunk_id=?", (chunk_id,)
                    ).fetchone()
                    values = list(struct.unpack(f"<{len(blob) // 4}f", blob))
                    values[0] += 0.5
                    conn.execute(
                        "UPDATE chunks_vec SET embedding=? WHERE chunk_id=?",
                        (struct.pack(f"<{len(values)}f", *values), chunk_id),
                    )
                else:
                    forged = body + " forged"
                    conn.execute(
                        "UPDATE chunks SET body=?, body_hash=? WHERE chunk_id=?",
                        (forged, input_digest(forged), chunk_id),
                    )
        conn.close()
        found = shelf.search("fresnel lens kerosene", embedder=embedder, rerank=False)
        other = shelf.search("spring tides new moon", embedder=embedder, rerank=False)
        assert (
            {(w.entry_id, w.category) for w in found.withheld},
            notes.entry_id in {hit.entry_id for hit in found.hits},
            tide.entry_id in {hit.entry_id for hit in other.hits},
        ) == ({(notes.entry_id, category)}, False, True), "an altered served row was answered"
