"""Per-hit membership proof (F004): each served hit proves its one accepted row against
the root its signed stage carries, in O(log n), and the entry is bound by that stage."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from biblios import membership
from biblios.derived import DERIVED_NAME
from biblios.testing import HashEmbedder, OverlapReranker
from tests.support import make_shelf
from tests.test_core import _bring, _Holding

MAX_CHUNKS = 16384  # the policy's admitted maximum


def _leaves(count: int) -> list[bytes]:
    return [
        membership.leaf_hash(i, f"c{i}", f"{i:064x}", f"{i + 1:064x}", f"{i + 2:064x}")
        for i in range(count)
    ]


def _check(position: int, count: int, path, root: str) -> bool:
    return membership.verify(
        position=position,
        chunk_id=f"c{position}",
        body_sha256=f"{position:064x}",
        input_sha256=f"{position + 1:064x}",
        vector_sha256=f"{position + 2:064x}",
        count=count,
        path=path,
        root_sha256=root,
    )


def test_the_tree_at_the_admitted_maximum_has_2n_minus_1_nodes_and_paths_of_14():
    """Failure mode: storage grows faster than O(n), or a path longer than ceil(log2 n),
    at the largest entry the policy admits."""
    nodes = membership.tree_nodes(_leaves(MAX_CHUNKS))
    assert len(nodes) == 2 * MAX_CHUNKS - 1
    root = membership.root_of(nodes, MAX_CHUNKS).hex()
    for position in (0, 1, MAX_CHUNKS // 2 - 1, MAX_CHUNKS // 2, MAX_CHUNKS - 1):
        ranges = membership.path_ranges(position, MAX_CHUNKS)
        assert len(ranges) == 14
        assert _check(position, MAX_CHUNKS, [nodes[key].hex() for key in ranges], root)
    # Just past a power of two the deepest leaf needs one more node, never more.
    nodes = membership.tree_nodes(_leaves(MAX_CHUNKS - 1))
    assert len(nodes) == 2 * (MAX_CHUNKS - 1) - 1
    assert max(len(membership.path_ranges(p, MAX_CHUNKS - 1)) for p in (0, MAX_CHUNKS - 2)) == 14


@pytest.mark.parametrize("count", [1, 2, 3, 5, 7, 8, 9])
def test_every_row_proves_and_no_other_position_count_or_root_does(count):
    """Failure mode: an unbalanced right edge mis-proves; a proof verifies at another
    position (reorder), another count (truncation or extension) or another root."""
    nodes = membership.tree_nodes(_leaves(count))
    root = membership.root_of(nodes, count).hex()
    for position in range(count):
        path = [nodes[key].hex() for key in membership.path_ranges(position, count)]
        assert _check(position, count, path, root)
        for other in range(count):
            if other != position:
                assert not membership.verify(
                    position=other,
                    chunk_id=f"c{position}",
                    body_sha256=f"{position:064x}",
                    input_sha256=f"{position + 1:064x}",
                    vector_sha256=f"{position + 2:064x}",
                    count=count,
                    path=path,
                    root_sha256=root,
                )
        assert not _check(position, count, path, "0" * 64)
        if path:
            flipped = list(path)
            flipped[0] = ("1" if flipped[0][0] == "0" else "0") + flipped[0][1:]
            assert not _check(position, count, flipped, root)


def test_an_interior_node_is_never_accepted_as_a_row():
    """Failure mode (second preimage): an interior node's children, presented as the rows
    of a smaller tree, prove membership. They reach the same root only as raw nodes; a
    row's leaf is always hashed under the 0x00 prefix and the count is the signed one."""
    nodes = membership.tree_nodes(_leaves(4))
    root = membership.root_of(nodes, 4)
    left, right = nodes[(0, 2)], nodes[(2, 4)]
    assert membership.root_from_path(left, 0, 2, [right]) == root  # why both defences exist
    assert membership.root_from_path(left, 0, 4, [right]) is None  # the signed count: 2 nodes
    assert all(
        membership.leaf_hash(0, f"c{i}", f"{i:064x}", f"{i + 1:064x}", f"{i + 2:064x}") != left
        for i in range(4)
    )


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        yield shelf, settings


_TEXT = b"# A\n\n" + b"".join(b"## part %d\n\nlighthouse %d keepers\n\n" % (i, i) for i in range(5))


def _stage_document(canonical: bytes) -> dict:
    return json.loads(canonical)


def _verified(hit, stage) -> bool:
    """The caller's check: its own verified copy of the stage names the hit's entry and
    signs the root and count the hit's row and path must reach."""
    document = _stage_document(stage.canonical)
    if hashlib.sha256(stage.canonical).hexdigest() != stage.stage_sha256:
        return False
    if document["entry"]["entry_id"] != hit.entry_id or stage.stage_sha256 != hit.stage_sha256:
        return False
    row = hit.manifest
    return membership.verify(
        position=row.position,
        chunk_id=row.chunk_id,
        body_sha256=row.body_sha256,
        input_sha256=row.input_sha256,
        vector_sha256=row.vector_sha256,
        count=document["entry"]["row_count"],
        path=row.path,
        root_sha256=document["entry"]["chunk_root_sha256"],
    )


def _search(shelf, embedder, query="lighthouse"):
    # Every chunk of these small shelves is a candidate, so a withheld entry never
    # crowds a sibling out of a short result list.
    return shelf.search(query, limit=50, embedder=embedder, reranker=OverlapReranker())


def test_a_hit_proves_its_row_against_the_signed_stage_with_keyed_reads_only(shelf):
    """Failure mode: a hit carries no proof the caller can check alone, its body or input
    digest is not in the proof, or the path is read by scanning the entry's rows."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    _bring(shelf, settings, embedder, entry_id)
    result = _search(shelf, embedder)
    stages = {stage.entry_id: stage for stage in result.stages}
    assert result.hits
    for hit in result.hits:
        assert _verified(hit, stages[hit.entry_id])
        assert hit.manifest.body_sha256 == hashlib.sha256(hit.chunk.encode()).hexdigest()
        forged = dataclasses.replace(hit.manifest, input_sha256="0" * 64)
        assert not _verified(dataclasses.replace(hit, manifest=forged), stages[hit.entry_id])
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
    count = conn.execute(
        "SELECT COUNT(*) FROM accepted_chunks WHERE entry_id=?", (entry_id,)
    ).fetchone()[0]
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM accepted_nodes WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]
        == 2 * count - 1
    )
    conn.close()


def test_the_audit_path_read_costs_the_same_however_large_the_entry():
    """Failure mode: the path read seeks the entry and then filters its rows, so a hit
    in a large entry costs O(entry rows) instead of O(path length)."""
    import tempfile

    from biblios.derived import DerivedStore
    from biblios.search import path_statement

    with tempfile.TemporaryDirectory() as root:
        store = DerivedStore(Path(root), read_only=False)
        conn = store.conn
        statement = path_statement(14)
        plan = [row[-1] for row in conn.execute("EXPLAIN QUERY PLAN " + statement, (0,) * 29)]
        assert any("(entry_id=? AND lo=? AND hi=?)" in line for line in plan), plan
        steps = []
        for count in (MAX_CHUNKS // 64, MAX_CHUNKS):
            nodes = membership.tree_nodes(_leaves(count))
            conn.execute("DELETE FROM accepted_nodes")
            conn.executemany(
                "INSERT INTO accepted_nodes VALUES (?, ?, ?, ?)",
                [("e", lo, hi, value) for (lo, hi), value in nodes.items()],
            )
            ranges = membership.path_ranges(count - 1, count)
            visited = [0]
            conn.set_progress_handler(lambda: visited.__setitem__(0, visited[0] + 1), 1)
            found = conn.execute(
                path_statement(len(ranges)), ("e", *(b for pair in ranges for b in pair))
            ).fetchall()
            conn.set_progress_handler(None, 0)
            assert len(found) == len(ranges)
            steps.append((len(ranges), visited[0]))
        (short, small), (long, large) = steps
        # Work grows with the path (8 -> 14 nodes), not with the entry (256 -> 16384 rows).
        assert large <= small * long / short * 1.5, steps
        store.close()


@pytest.mark.parametrize(
    "malformed",
    [
        {"position": True},
        {"position": 1.0},
        {"position": -1},
        {"count": 0},
        {"count": MAX_CHUNKS + 1},
        {"count": "4"},
        {"body_sha256": "Z" * 64},
        {"vector_sha256": "0" * 63},
        {"root_sha256": None},
        {"path": None},
        {"path": ["zz"]},
        {"path": [b"\x00" * 32]},
        {"path": "0" * 64},
    ],
)
def test_a_malformed_proof_input_is_false_and_never_raises(malformed):
    """Failure mode: a wrong type, range or length in a caller-supplied or stored proof
    raises out of verification instead of failing it."""
    nodes = membership.tree_nodes(_leaves(4))
    good = {
        "position": 1,
        "chunk_id": "c1",
        "body_sha256": f"{1:064x}",
        "input_sha256": f"{2:064x}",
        "vector_sha256": f"{3:064x}",
        "count": 4,
        "path": [nodes[key].hex() for key in membership.path_ranges(1, 4)],
        "root_sha256": membership.root_of(nodes, 4).hex(),
    }
    assert membership.verify(**good)
    assert membership.verify(**{**good, **malformed}) is False


@pytest.mark.parametrize(
    ("statement", "category"),
    [
        ("UPDATE accepted_chunks SET position = 'one' WHERE entry_id=?", "proof-invalid"),
        ("UPDATE accepted_chunks SET position = 999 WHERE entry_id=?", "proof-invalid"),
        ("UPDATE accepted_nodes SET hash = 'text' WHERE entry_id=?", "proof-invalid"),
        ("UPDATE accepted_nodes SET hash = x'00' WHERE entry_id=?", "proof-invalid"),
        ("UPDATE accepted_nodes SET lo = lo + 100000 WHERE entry_id=?", "proof-missing"),
    ],
)
def test_a_malformed_stored_proof_withholds_only_its_entry(shelf, statement, category):
    """Failure mode: a malformed stored row or node raises out of search, losing every
    other entry's answer, instead of withholding that entry alone."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    ours = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    theirs = shelf.add("notes/b.md", _TEXT.replace(b"keepers", b"pilots")).affected_entry_ids[0]
    for entry_id in (ours, theirs):
        _bring(shelf, settings, embedder, entry_id)
    _tamper(shelf, (statement, (ours,)))
    result = _search(shelf, embedder)
    assert ours not in {hit.entry_id for hit in result.hits}
    assert theirs in {hit.entry_id for hit in result.hits}
    assert {w.category for w in result.withheld if w.entry_id == ours} == {category}


def _tamper(shelf, *statements):
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME, isolation_level=None)
    for statement, params in statements:
        conn.execute(statement, params)
    conn.close()


@pytest.mark.parametrize(
    "tamper",
    ["reorder", "missing-node", "flipped-node", "foreign-nodes"],
)
def test_a_tampered_proof_withholds_the_entry_and_serves_nothing(shelf, tamper):
    """Failure mode: rows reordered, a path node removed or changed, or another entry's
    tree substituted, and the hit is still served."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    ours = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    theirs = shelf.add("notes/b.md", _TEXT.replace(b"keepers", b"pilots")).affected_entry_ids[0]
    for entry_id in (ours, theirs):
        _bring(shelf, settings, embedder, entry_id)
    if tamper == "reorder":
        # Every row moved one place: each row now claims another row's position.
        conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
        count = conn.execute(
            "SELECT COUNT(*) FROM accepted_chunks WHERE entry_id=?", (ours,)
        ).fetchone()[0]
        conn.close()
        _tamper(
            shelf,
            ("UPDATE accepted_chunks SET position = -1 - position WHERE entry_id=?", (ours,)),
            (
                "UPDATE accepted_chunks SET position = (-position) % ? WHERE entry_id=?",
                (count, ours),
            ),
        )
        expected = "proof-invalid"
    elif tamper == "missing-node":
        _tamper(shelf, ("DELETE FROM accepted_nodes WHERE entry_id=?", (ours,)))
        expected = "proof-missing"
    elif tamper == "flipped-node":
        _tamper(shelf, ("UPDATE accepted_nodes SET hash = zeroblob(32) WHERE entry_id=?", (ours,)))
        expected = "proof-invalid"
    else:
        _tamper(
            shelf,
            ("DELETE FROM accepted_nodes WHERE entry_id=?", (ours,)),
            (
                "INSERT INTO accepted_nodes SELECT ?, lo, hi, hash FROM accepted_nodes "
                "WHERE entry_id=?",
                (ours, theirs),
            ),
        )
        expected = "proof-invalid"
    result = _search(shelf, embedder, "lighthouse keepers")
    assert ours not in {hit.entry_id for hit in result.hits}
    assert ours not in {stage.entry_id for stage in result.stages}
    assert {w.category for w in result.withheld if w.entry_id == ours} == {expected}


def test_membership_binds_a_row_to_a_root_and_only_the_stage_binds_the_entry(shelf):
    """Failure mode: membership is taken to bind an entry, so another entry's genuine row
    and proof, presented as this entry's hit, is accepted without the stage naming it."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    first = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    second = shelf.add("notes/b.md", _TEXT.replace(b"keepers", b"pilots")).affected_entry_ids[0]
    for entry_id in (first, second):
        _bring(shelf, settings, embedder, entry_id)
    result = _search(shelf, embedder)
    stages = {stage.entry_id: stage for stage in result.stages}
    genuine = next(hit for hit in result.hits if hit.entry_id == second)
    assert _verified(genuine, stages[second])
    presented = dataclasses.replace(genuine, entry_id=first)
    document = _stage_document(stages[second].canonical)["entry"]
    row = presented.manifest
    assert membership.verify(  # the row is genuinely under that root...
        position=row.position,
        chunk_id=row.chunk_id,
        body_sha256=row.body_sha256,
        input_sha256=row.input_sha256,
        vector_sha256=row.vector_sha256,
        count=document["row_count"],
        path=row.path,
        root_sha256=document["chunk_root_sha256"],
    )
    assert not _verified(presented, stages[second])  # ...but that stage names another entry
    assert not _verified(presented, stages[first])  # and it is not under this entry's root


def test_acceptance_is_atomic_with_its_proof_and_resumes_without_embedding(shelf, monkeypatch):
    """Failure mode: an acceptance interrupted while writing its proof leaves rows served
    without nodes (or nodes without rows), or the restart embeds again."""
    from biblios import core

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]

    def interrupted(self, stage, rows):
        raise RuntimeError("interrupted while writing the proof")

    monkeypatch.setattr(core._Run, "_accept_nodes", interrupted)
    failed = _bring(shelf, settings, embedder, entry_id)
    assert failed.outcome(entry_id).outcome == "recovery-required"
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
    assert (
        conn.execute(
            "SELECT (SELECT COUNT(*) FROM accepted_chunks WHERE entry_id=?)"
            " + (SELECT COUNT(*) FROM accepted_nodes WHERE entry_id=?)",
            (entry_id, entry_id),
        ).fetchone()[0]
        == 0
    )
    conn.close()
    assert entry_id not in {hit.entry_id for hit in _search(shelf, embedder).hits}
    monkeypatch.undo()
    calls = len(embedder.calls)
    resumed = _bring(shelf, settings, embedder, entry_id)
    assert resumed.outcome(entry_id).outcome == "searchable"
    assert len(embedder.calls) == calls  # the persisted stage is finished, never re-embedded
    hits = [hit for hit in _search(shelf, embedder).hits if hit.entry_id == entry_id]
    stages = {stage.entry_id: stage for stage in _search(shelf, embedder).stages}
    assert hits and all(_verified(hit, stages[entry_id]) for hit in hits)


def _as_first_version(shelf, entry_id, *, keep_nodes: bool = False) -> str:
    """Rewrite an accepted entry as an earlier release's ``biblios.stage/1`` acceptance:
    the same rows, no membership root and no nodes."""
    from biblios.core import _parse_stage, build_stage

    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME, isolation_level=None)
    sha, canonical = conn.execute(
        "SELECT stage_sha256, canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
    ).fetchone()
    stage = _parse_stage(bytes(canonical), sha)
    fields = {f.name: getattr(stage, f.name) for f in dataclasses.fields(stage)}
    for name in ("stage_sha256", "canonical"):
        fields.pop(name)
    fields["entry"] = dataclasses.replace(stage.entry, chunk_root_sha256=None, checkpoint=None)
    old = build_stage(schema="biblios.stage/1", **fields)
    assert _parse_stage(old.canonical, old.stage_sha256).canonical == old.canonical
    conn.execute(
        "UPDATE accepted_stages SET stage_sha256=?, canonical=? WHERE entry_id=?",
        (old.stage_sha256, old.canonical, entry_id),
    )
    conn.execute(
        "UPDATE entries SET accepted_stage_sha256=? WHERE entry_id=?", (old.stage_sha256, entry_id)
    )
    if not keep_nodes:
        conn.execute("DELETE FROM accepted_nodes WHERE entry_id=?", (entry_id,))
    conn.close()
    return old.stage_sha256


def test_an_earlier_version_stage_is_unproven_even_where_nodes_exist(shelf):
    """Failure mode: a ``biblios.stage/1`` acceptance is served because stored nodes
    happen to exist; it signs no root, so nothing stored can prove its rows."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    _bring(shelf, settings, embedder, entry_id)
    _as_first_version(shelf, entry_id, keep_nodes=True)
    result = _search(shelf, embedder)
    assert result.hits == ()
    assert {(w.entry_id, w.category) for w in result.withheld} == {(entry_id, "proof-missing")}


def test_a_stage_whose_signed_root_disagrees_with_its_rows_is_never_accepted(shelf, monkeypatch):
    """Failure mode: acceptance writes the membership tree of rows other than the ones the
    signed root covers, so a stage is accepted whose proofs can never verify."""
    from biblios import core

    shelf, settings = shelf
    embedder = HashEmbedder()
    entry_id = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    monkeypatch.setattr(core, "chunk_root", lambda rows: "0" * 64 if rows else None)
    result = _bring(shelf, settings, embedder, entry_id)
    assert (result.outcome(entry_id).outcome, result.outcome(entry_id).code) == (
        "recovery-required",
        "stage-rows-tampered",
    )
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM accepted_nodes WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]
        == 0
    )
    conn.close()
    assert entry_id not in {hit.entry_id for hit in _search(shelf, embedder).hits}


def _caller(shelf, settings, embedder, authority):
    """Bring entries current (all dirty ones for ``None``) under ``authority``."""
    import uuid as _uuid

    from biblios.consumer import BoundShelf
    from biblios.core import BringCurrentRequest, bring_current
    from biblios.pipeline import ConsumerPipeline
    from biblios.representation import default_policy, drawbridge_converter, envelope_renderer

    def call(entry_ids, operation=None):
        return bring_current(
            BoundShelf(shelf.local_binding(), shelf),
            BringCurrentRequest(operation or str(_uuid.uuid4()), entry_ids),
            pipeline=ConsumerPipeline(
                converter=drawbridge_converter(settings),
                renderer=envelope_renderer,
                input_view=None,
                policy=default_policy(),
                embedder=embedder,
            ),
            authority=authority,
        )

    return call


def test_an_earlier_version_stage_is_withheld_until_named_and_restaged_without_spend(shelf):
    """Failure mode: a ``biblios.stage/1`` acceptance is rewritten, served without a proof,
    re-staged without the caller naming it, re-staged by spending on the provider, or
    answered differently on replay."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    holding = _Holding()
    holding.release = True
    holding.head = holding.authorized  # the caller records each stage it signs as accepted
    call = _caller(shelf, settings, embedder, holding)
    entry_id = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    # Accepted earlier by the same caller, its checkpoint sealed, and acknowledged, so no
    # stage is retained. (An acceptance made without an authority is the limitation proof
    # that follows.)
    call((entry_id,))
    call((entry_id,))  # the caller's head now records it: the retained stage is cleaned up
    old = _as_first_version(shelf, entry_id)
    holding.head.append(old)  # the earlier release's caller had accepted the stage/1 record
    result = _search(shelf, embedder)
    assert result.hits == ()
    assert {(w.entry_id, w.category) for w in result.withheld} == {(entry_id, "proof-missing")}
    calls = len(embedder.calls)
    unnamed = call(None)
    assert unnamed.accepted == () and _search(shelf, embedder).hits == ()
    operation = str(uuid.uuid4())
    sealed = len(holding.sealed)
    migrated = call((entry_id,), operation)
    [accepted] = migrated.accepted
    assert accepted.stage_sha256 != old
    assert all(call == ("lighthouse",) for call in embedder.calls[calls:])  # queries only
    assert len(holding.sealed) == sealed  # the persisted checkpoint is verified, never sealed
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
    canonical = conn.execute(
        "SELECT canonical FROM accepted_stages WHERE entry_id=?", (entry_id,)
    ).fetchone()[0]
    conn.close()
    document = json.loads(canonical)
    assert document["schema"] == "biblios.stage/2"
    assert document["predecessor_sha256"] == old and document["reused_from_sha256"] == old
    assert document["entry"]["checkpoint"]["reused"] is True
    found = _search(shelf, embedder)
    stages = {stage.entry_id: stage for stage in found.stages}
    assert found.hits and all(_verified(hit, stages[entry_id]) for hit in found.hits)
    replay = call((entry_id,), operation)
    assert replay.replayed and dataclasses.replace(replay, replayed=False) == migrated


def test_an_earlier_version_stage_whose_checkpoint_was_never_sealed_stays_pending_without_spend(
    shelf,
):
    """Limitation and security proof. An acceptance made without a stage authority (as
    every earlier release's checkpoint is unsealed) is later named under one: its
    recorded rendering is not the caller's, so it is never used; the projection falls
    back to keeping every mirror byte, the committed rows no longer match, and the
    migration is pending, with no provider call, no seal over persisted content, and the
    earlier acceptance and mirror untouched. Failure mode: unsealed checkpoint content
    reaches a stage the caller signs, or the refusal spends or reconverts silently."""
    shelf, settings = shelf
    embedder = HashEmbedder()
    holding = _Holding()
    holding.release = True
    entry_id = shelf.add("notes/a.md", _TEXT).affected_entry_ids[0]
    _bring(shelf, settings, embedder, entry_id)  # accepted without an authority
    old = _as_first_version(shelf, entry_id)
    holding.head = [old]
    mirror = shelf.mirror_path(entry_id).read_bytes()
    calls = len(embedder.calls)
    call = _caller(shelf, settings, embedder, holding)
    assert call(None).entries == ()
    named = call((entry_id,))
    assert [(item.outcome, item.code) for item in named.entries] == [
        ("pending", "proof-migration-needs-reembed")
    ]
    assert named.accepted == () and holding.authorized == []
    assert len(embedder.calls) == calls and holding.sealed == []
    conn = sqlite3.connect(shelf.palace_store / DERIVED_NAME)
    assert conn.execute(
        "SELECT stage_sha256 FROM accepted_stages WHERE entry_id=?", (entry_id,)
    ).fetchall() == [(old,)]
    conn.close()
    assert shelf.mirror_path(entry_id).read_bytes() == mirror
    assert {(w.entry_id, w.category) for w in _search(shelf, embedder).withheld} == {
        (entry_id, "proof-missing")
    }
