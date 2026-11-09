"""Stacks: one identity, one rerank over the union, attribution to the shelf, lagging
members admitted and named, dead hits refused, citations across shelves."""

from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path

import pytest

from biblios import BibliosError, MixedIdentityError, open_stack
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import GUIDE, NOTES, OPENROUTER_SETTINGS_TEXT, TIDE, make_shelves


def test_stack_search_attributes_hits_and_reranks_once(tmp_path: Path) -> None:
    """Failure mode: attribution by store path fails, a hit's path is stale after a rename,
    or the union is reranked per store instead of once."""
    (a, b), settings = make_shelves(tmp_path, ["a", "b"])
    embedder, reranker = HashEmbedder(), OverlapReranker()
    with ExitStack() as stack:
        stack.enter_context(a)
        stack.enter_context(b)
        notes = a.add("notes.txt", NOTES.encode(), media_type="text/plain")
        log = b.add("log.txt", (NOTES + "\nSecond copy kept by the relief keeper.\n").encode())
        b.add("tide.txt", TIDE.encode(), media_type="text/plain")
        a.reconcile(settings, embedder=embedder)
        b.reconcile(settings, embedder=embedder)
        a.rename("notes.txt", "keeper.txt")
        federated = open_stack([a, b])
        # The rename advanced a's revision past its watermark: the stack says so until a
        # step runs, and that step embeds nothing.
        assert federated.search("fresnel", embedder=embedder, rerank=False).indexing == (
            a.shelf_id,
        )
        embedder.calls.clear()
        assert a.reconcile(settings, embedder=embedder).indexed == () and embedder.calls == []
        assert federated.shelf_ids == (a.shelf_id, b.shelf_id)
        result = federated.search("fresnel lens kerosene", embedder=embedder, reranker=reranker)
        by_shelf = {}
        for hit in result.hits:
            by_shelf.setdefault(hit.shelf_id, hit)
        assert set(by_shelf) == {a.shelf_id, b.shelf_id}
        assert by_shelf[a.shelf_id].entry_id == notes.entry_id
        assert by_shelf[a.shelf_id].path == "keeper.txt"
        assert (
            by_shelf[b.shelf_id].entry_id == log.entry_id and by_shelf[b.shelf_id].path == "log.txt"
        )
        assert len(reranker.calls) == 1 and result.rerank_status == "applied"
        assert result.indexing == () and result.refused == ()
        assert not result.status[a.shelf_id].lagging and not result.status[b.shelf_id].lagging


def test_mixed_identity_is_refused_at_construction(tmp_path: Path) -> None:
    """Failure mode: a stack over two embedding identities is accepted, or refused only
    once a query has already run."""
    (a, b), settings = make_shelves(tmp_path / "same", ["a", "b"])
    (c,), other = make_shelves(tmp_path / "other", ["c"], settings_text=OPENROUTER_SETTINGS_TEXT)
    embedder, reranker = HashEmbedder(), OverlapReranker()
    with ExitStack() as stack:
        for shelf in (a, b, c):
            stack.enter_context(shelf)
        a.add("notes.txt", NOTES.encode(), media_type="text/plain")
        a.reconcile(settings, embedder=embedder)
        embedder.calls.clear()
        with pytest.raises(MixedIdentityError) as refused:
            open_stack([a, b, c])
        message = str(refused.value)
        assert "ollama" in message and "openrouter" in message
        assert message.endswith(f"odd ones out: {c.shelf_id}")
        assert set(refused.value.identities) == {a.shelf_id, b.shelf_id, c.shelf_id}
        assert embedder.calls == [] and reranker.calls == []
        with pytest.raises(BibliosError, match="at least one shelf"):
            open_stack([])
        with pytest.raises(BibliosError, match="named twice"):
            open_stack([a, a])


def test_lagging_member_is_admitted_and_named(tmp_path: Path) -> None:
    """Failure mode: an unbuilt or lagging member is refused or handed to Palace, or the
    answer claims coverage it does not have."""
    (a, b), settings = make_shelves(tmp_path, ["a", "b"])
    embedder = HashEmbedder()
    with ExitStack() as stack:
        stack.enter_context(a)
        stack.enter_context(b)
        notes = a.add("notes.txt", NOTES.encode(), media_type="text/plain")
        a.reconcile(settings, embedder=embedder)
        tide = b.add(
            "tide.txt", (TIDE + "\nThe keeper's fresnel lens is cleaned at neap tide.\n").encode()
        )
        federated = open_stack([a, b])
        behind = federated.search("fresnel lens", embedder=embedder, rerank=False)
        assert [hit.entry_id for hit in behind.hits] == [notes.entry_id]
        assert behind.indexing == (b.shelf_id,)
        assert behind.status[b.shelf_id].lagging and not behind.status[b.shelf_id].built
        assert behind.status[b.shelf_id].pending == 1

        b.reconcile(settings, embedder=embedder)
        current = federated.search("fresnel lens", embedder=embedder, rerank=False)
        assert {hit.entry_id for hit in current.hits} == {notes.entry_id, tide.entry_id}
        assert current.indexing == ()

        b.add("more.md", GUIDE.encode(), media_type="text/markdown")
        again = federated.search("fresnel lens", embedder=embedder, rerank=False)
        assert again.indexing == (b.shelf_id,) and again.status[b.shelf_id].built
        assert {hit.entry_id for hit in again.hits} == {notes.entry_id, tide.entry_id}


def test_dead_entry_hit_is_refused_and_reported_by_shelf(tmp_path: Path) -> None:
    """Failure mode: a chunk whose entry is no longer live in its shelf is returned."""
    (a, b), settings = make_shelves(tmp_path, ["a", "b"])
    embedder = HashEmbedder()
    with ExitStack() as stack:
        stack.enter_context(a)
        stack.enter_context(b)
        a.add("notes.txt", NOTES.encode(), media_type="text/plain")
        tide = b.add("tide.txt", TIDE.encode(), media_type="text/plain")
        a.reconcile(settings, embedder=embedder)
        b.reconcile(settings, embedder=embedder)
        federated = open_stack([a, b])
        b._mutate("delete-entry", {"entry_id": tide.entry_id}, operation_id=None)
        result = federated.search("spring tides new moon", embedder=embedder, rerank=False)
        assert (b.shelf_id, tide.entry_id) in result.refused
        assert all(hit.entry_id != tide.entry_id for hit in result.hits)


def test_cross_shelf_citations_reach_the_right_shelf(tmp_path: Path) -> None:
    """Failure mode: a citation addressed by shelf id resolves against the wrong shelf, or
    an unknown shelf id is answered instead of refused."""
    (a, b), settings = make_shelves(tmp_path, ["a", "b"])
    embedder = HashEmbedder()
    with ExitStack() as stack:
        stack.enter_context(a)
        stack.enter_context(b)
        notes = a.add("notes.txt", NOTES.encode(), media_type="text/plain")
        guide = b.add("guide.md", GUIDE.encode(), media_type="text/markdown")
        a.reconcile(settings, embedder=embedder)
        b.reconcile(settings, embedder=embedder)
        federated = open_stack([a, b])
        assert federated.resolve(a.shelf_id, notes.entry_id).path == "notes.txt"
        assert federated.download(b.shelf_id, guide.entry_id) == GUIDE.encode()
        page = federated.read(b.shelf_id, guide.entry_id, page=2)
        assert page.page == 2 and "second section" in page.text
        with pytest.raises(BibliosError, match="no live entry"):
            federated.resolve(b.shelf_id, notes.entry_id)
        with pytest.raises(BibliosError, match="no shelf"):
            federated.resolve("nobody", notes.entry_id)
