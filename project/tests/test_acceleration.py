"""Opt-in exact vector acceleration at the bound seam: parity, visibility and refusals.

Every proof is offline over synthetic shelves. A dense, text-seeded embedder gives every
chunk a distinct distance to the query, so the cache can certify its cut. With the
token-hashing test embedder most chunks tie, every leg goes to the exact oracle, and
parity would hold without the cache serving anything; the parity proof therefore
requires cached legs and no oracle legs, and that requirement may not be relaxed.
"""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import threading
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import sqlite_vec
from castle.namespace_environment import LocalSession
from palace.writer_lock import writer_lock

from biblios import VectorAcceleration
from biblios.acceleration import vector_error
from biblios.binding import open_bound_shelf
from biblios.consumer import open_bound_stack
from biblios.control import ConsumerError
from biblios.curation import CurationRequest, DeleteEntry, ReplaceContent
from biblios.history import ExcisionRequest, ExcisionSelection
from biblios.testing import OverlapReranker
from tests.acceleration_support import QUERY, DenseEmbedder
from tests.consumer_fixtures import consumer_fixture, identifier

TEXTS = (
    b"Lighthouse lens tide and harbour maintenance.",
    b"Harbour pilot boats and tide tables.",
    b"Lens grinding for lighthouse optics.",
)
BUDGET = 256 * 2**20


def _shelf(directory: Path, texts=TEXTS, *, feed: bool = True):
    fixture = consumer_fixture(directory)
    for index, text in enumerate(texts):
        fixture.bound.curate(fixture.request(f"note-{index}.txt", text))
    _reconcile(fixture)
    if feed:
        fixture.bound.enable_vector_feed()
    return fixture


def _reconcile(fixture) -> None:
    fixture.bound._shelf.reconcile(fixture.settings, embedder=DenseEmbedder())


def _search(target, acceleration=None):
    return target.search(
        QUERY, embedder=DenseEmbedder(), reranker=OverlapReranker(), acceleration=acceleration
    )


def _same_answer(exact, fast) -> bool:
    return (exact.hits, exact.withheld, exact.stages, exact.rerank_status) == (
        fast.hits,
        fast.withheld,
        fast.stages,
        fast.rerank_status,
    )


def _resident(acceleration) -> set[str | None]:
    return {item.shelf_id for item in acceleration.stats().shelves}


def test_without_acceleration_search_is_the_released_path(tmp_path: Path) -> None:
    """Failure mode: an eager import or a default makes every search load Palace's cache
    or feed module, or report a route, although no consumer opted in. The shelves are
    built here; the child only searches, because any Palace write loads the feed module."""
    fixtures = [_shelf(tmp_path / name, feed=False) for name in ("one", "two")]
    documents = [asdict(replace(fixture.binding, ownership="owned")) for fixture in fixtures]
    for fixture in fixtures:
        fixture.close()
    script = f"""
import json, sys
sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
from castle.namespace_environment import LocalSession
from biblios.binding import open_bound_shelf
from biblios.consumer import open_bound_stack
from biblios.testing import OverlapReranker
from tests.consumer_fixtures import binding_from_document
from tests.acceleration_support import QUERY, DenseEmbedder
bound = []
for document in json.loads(sys.argv[1]):
    binding = binding_from_document(document)
    session = LocalSession(
        binding.namespace_root, binding.castle_root, binding.root / "namespace.lock"
    )
    bound.append(open_bound_shelf(binding, session))
providers = dict(embedder=DenseEmbedder(), reranker=OverlapReranker())
single = bound[0].search(QUERY, **providers)
union = open_bound_stack(bound).search(QUERY, **providers)
modules = ("palace.vector_cache", "palace.index.vector_feed")
loaded = [name for name in modules if name in sys.modules]
found = [bool(single.hits), bool(union.hits)]
print(json.dumps([*found, single.vector_routes, union.vector_routes, loaded]))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, json.dumps(documents, default=str)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout.strip().splitlines()[-1]) == [True, True, None, None, []], (
        "a search without acceleration reported a route or loaded the cache or feed"
    )


@pytest.mark.parametrize("shape", ("single", "stack"))
def test_accelerated_search_matches_exact_search_through_every_writer(
    tmp_path: Path, shape: str
) -> None:
    """Failure mode: the cache is handed the wrong store, the stack loses evidence on the
    prepare/execute path, or a Biblios writer changes the index without the feed, so an
    accelerated answer differs from the exact one or the feed diverges."""
    first = _shelf(tmp_path / "first")
    fixtures = [first]
    if shape == "stack":
        fixtures.append(_shelf(tmp_path / "second", (b"Tide gauge calibration notes.",)))
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="refuse")
    try:

        def target():
            if shape == "single":
                return first.bound
            return open_bound_stack(tuple(fixture.bound for fixture in fixtures))

        def check(step: str) -> None:
            exact, fast = _search(target()), _search(target(), acceleration)
            stored = {
                fixture.binding.shelf_id: fixture.bound.vector_status() for fixture in fixtures
            }
            resident = {item.shelf_id: item for item in acceleration.stats().shelves}
            observed = {
                route.shelf_id: (
                    route.cached_legs >= 1,
                    route.oracle_legs,
                    route.fallback_legs,
                    stored[route.shelf_id].diverged,
                    resident[route.shelf_id].live_rows == stored[route.shelf_id].vector_rows,
                )
                for route in fast.vector_routes
            }
            assert (_same_answer(exact, fast), exact.vector_routes, observed) == (
                True,
                None,
                dict.fromkeys(stored, (True, 0, 0, False, True)),
            ), f"accelerated search differed from exact search or was not served after {step}"

        check("enabling the feed")
        created = first.bound.curate(first.request("added.txt", b"Lighthouse keeper logbook tide."))
        added = created.mutation.affected_entry_ids[0]
        _reconcile(first)
        check("adding an entry")
        first.bound.curate(
            CurationRequest(
                identifier(),
                first.bound.view.revision,
                ReplaceContent(added, b"Lens cleaning routine for the lighthouse."),
            )
        )
        _reconcile(first)
        check("replacing an entry's content")
        first.bound.curate(
            CurationRequest(identifier(), first.bound.view.revision, DeleteEntry(added))
        )
        _reconcile(first)
        check("deleting an entry")
        _excise_first(first)
        check("excising an entry")
    finally:
        for fixture in fixtures:
            fixture.close()


def _excise_first(fixture):
    """Excise the first file and rebind the fixture to the successor; return the retired
    bound shelf."""
    retired = fixture.bound
    request = ExcisionRequest(
        identifier(),
        retired.view.revision,
        ExcisionSelection(metadata_entry_ids=(retired.files()[0].entry_id,)),
        (),
        fixture.binding.root / "successor",
    )
    result = retired.excise(retired.preview_excision(request).request)
    declared = replace(result.successor_binding, ownership="owned")
    fixture.handle.close()
    fixture.bound = open_bound_shelf(
        declared,
        LocalSession(
            declared.namespace_root, declared.castle_root, declared.root / "namespace.lock"
        ),
    )
    fixture.binding = declared
    return retired


def test_an_excised_shelf_can_be_evicted_through_its_retired_binding(tmp_path: Path) -> None:
    """Failure mode: eviction needs admission, so the binding an excision retired (the
    one a consumer holds when it learns of the excision) cannot release the vectors the
    process still holds."""
    fixture = _shelf(tmp_path)
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="refuse")
    try:
        _search(fixture.bound, acceleration)
        retired = _excise_first(fixture)
        resident, before = _resident(acceleration), acceleration.stats()
        evicted, after = acceleration.evict(retired), acceleration.stats()
        assert (
            resident,
            evicted,
            _resident(acceleration),
            (after.live_bytes, after.pooled_bytes, after.owned_bytes),
        ) == (
            {fixture.binding.shelf_id},
            True,
            set(),
            (0, before.live_bytes, before.owned_bytes),
        ), "the retired binding could not evict, or eviction's memory movement differs"
    finally:
        fixture.close()


@pytest.mark.parametrize("policy", ("refuse", "fallback"))
def test_a_budget_too_small_refuses_or_falls_back_visibly(tmp_path: Path, policy: str) -> None:
    """Failure mode: budget pressure becomes an anonymous search failure, or a fallback
    answers without saying so."""
    fixture = _shelf(tmp_path)
    acceleration = VectorAcceleration(budget_bytes=1, on_unavailable=policy)
    try:
        if policy == "refuse":
            with pytest.raises(ConsumerError) as refused:
                _search(fixture.bound, acceleration)
            observed = (refused.value.code, refused.value.effect)
            expected = ("vector-cache-budget", "none")
        else:
            exact, fast = _search(fixture.bound), _search(fixture.bound, acceleration)
            (route,) = fast.vector_routes
            observed = (_same_answer(exact, fast), route.fallback_legs >= 1, route.reasons)
            expected = (True, True, ("budget",))
        stats = acceleration.stats()
        counter = ("refusals" if policy == "refuse" else "fallbacks") + ".budget"
        assert (
            observed,
            stats.budget_bytes,
            stats.counters.get(counter, 0) >= 1,
            stats.owned_bytes,
            stats.headroom_bytes,
        ) == (expected, 1, True, 0, 1), (
            "budget pressure was not reported as its own refusal or visible fallback"
        )
    finally:
        fixture.close()


@pytest.mark.parametrize("policy", ("refuse", "fallback"))
def test_a_shelf_without_a_feed_is_unavailable_not_failed(tmp_path: Path, policy: str) -> None:
    """Failure mode: a shelf that never opted in reports zero stored vectors, or its
    unavailability is hidden or reported as a failure."""
    fixture = _shelf(tmp_path, feed=False)
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable=policy)
    try:
        status = fixture.bound.vector_status()
        assert (status.feed, status.vector_rows, status.live_chunks, status.diverged) == (
            "disabled",
            None,
            None,
            None,
        ), "a shelf without a feed reported counts it cannot know"
        if policy == "refuse":
            with pytest.raises(ConsumerError) as refused:
                _search(fixture.bound, acceleration)
            assert refused.value.code == "vector-cache-feed-disabled"
        else:
            exact, fast = _search(fixture.bound), _search(fixture.bound, acceleration)
            (route,) = fast.vector_routes
            assert (_same_answer(exact, fast), route.cached_legs, route.reasons) == (
                True,
                0,
                ("feed-disabled",),
            ), "a shelf without a feed was not reported as a visible fallback"
    finally:
        fixture.close()


def test_an_unbuilt_shelf_reports_an_empty_route_not_an_exact_search(tmp_path: Path) -> None:
    """Failure mode: an accelerated search that searched nothing reports ``None``, which
    a consumer reads as "acceleration was off", or a stack reports a route for an
    unbuilt member."""
    built = _shelf(tmp_path / "built")
    unbuilt = consumer_fixture(tmp_path / "unbuilt")
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="refuse")
    try:
        unbuilt.bound.curate(unbuilt.request(data=b"pending unbuilt source"))
        alone = _search(unbuilt.bound, acceleration)
        union = _search(open_bound_stack((built.bound, unbuilt.bound)), acceleration)
        assert (alone.vector_routes, [route.shelf_id for route in union.vector_routes]) == (
            (),
            [built.binding.shelf_id],
        )
    finally:
        built.close()
        unbuilt.close()


@pytest.mark.parametrize("refusal", ("false", "raises"))
def test_a_revoked_or_failing_hook_fails_closed_under_fallback(
    tmp_path: Path, refusal: str
) -> None:
    """Failure mode: an authorization refusal or a failing hook is treated as ordinary
    unavailability and answered by fallback, or the revoked shelf stays resident."""
    fixture = _shelf(tmp_path)
    seen: list[str] = []
    revoked: list[bool] = []

    def authorize(view):
        seen.append(view.shelf_id)
        if revoked and refusal == "raises":
            raise RuntimeError("policy store unavailable")
        return not revoked

    acceleration = VectorAcceleration(
        budget_bytes=BUDGET, on_unavailable="fallback", authorize=authorize
    )
    try:
        assert acceleration.warm(fixture.bound) and _resident(acceleration) == {
            fixture.binding.shelf_id
        }
        revoked.append(True)
        with pytest.raises(ConsumerError) as refused:
            _search(fixture.bound, acceleration)
        assert (
            refused.value.code,
            _resident(acceleration),
            acceleration.stats().counters.get("evictions.authorization", 0),
            set(seen),
        ) == ("vector-cache-unauthorized", set(), 1, {fixture.binding.shelf_id}), (
            "an authorization failure fell back or left the shelf resident"
        )
    finally:
        fixture.close()


def test_the_cache_serves_only_stores_admitted_by_the_current_call(tmp_path: Path) -> None:
    """Failure mode: admission leaks past the call that made it (same thread, after it
    returned) or across threads (another thread's call in progress), so the cache serves
    a store no current call admitted."""
    fixture = _shelf(tmp_path)
    store = fixture.bound._shelf.palace_store
    entered, release, hold = threading.Event(), threading.Event(), []

    def holding(view):
        if hold:
            entered.set()
            assert release.wait(10)
        return True

    acceleration = VectorAcceleration(
        budget_bytes=BUDGET, on_unavailable="fallback", authorize=holding
    )

    def lease_codes() -> str:
        try:
            with acceleration._cache.lease([store]):
                return "served"
        except Exception as error:
            mapped = vector_error(error)
            return mapped.code if mapped is not None else type(error).__name__

    try:
        _search(fixture.bound, acceleration)
        after_call = lease_codes()
        evicted = (
            _resident(acceleration),
            acceleration.stats().counters["evictions.authorization"],
        )
        hold.append(True)
        outcome: list[object] = []
        searcher = threading.Thread(
            target=lambda: outcome.append(_search(fixture.bound, acceleration))
        )
        searcher.start()
        assert entered.wait(10), "the concurrent call never reached authorization"
        during_other_call = lease_codes()
        release.set()
        searcher.join(10)
        assert (after_call, evicted, during_other_call, len(outcome), searcher.is_alive()) == (
            "vector-cache-unauthorized",
            (set(), 1),
            "vector-cache-unauthorized",
            1,
            False,
        ), "the cache served a store the current call had not admitted"
    finally:
        release.set()
        fixture.close()


def test_a_diverged_feed_fails_closed_until_a_new_lineage(tmp_path: Path) -> None:
    """Failure mode: a chunk written outside the feed is served stale by fallback, the
    stored status hides it, or a plain re-enable silently clears it."""
    fixture = _shelf(tmp_path)
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="fallback")
    try:
        connection = sqlite3.connect(fixture.bound._shelf.chunks_db)
        try:
            with connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(chunks)")]
                row = connection.execute(
                    f"SELECT {', '.join(columns)} FROM chunks LIMIT 1"
                ).fetchone()
                values = list(row)
                values[columns.index("chunk_id")] = "outside-the-feed"
                connection.execute(
                    f"INSERT INTO chunks ({', '.join(columns)}) VALUES "
                    f"({', '.join('?' for _ in columns)})",
                    values,
                )
        finally:
            connection.close()
        with pytest.raises(ConsumerError) as refused:
            _search(fixture.bound, acceleration)
        kept = fixture.bound.enable_vector_feed()
        restarted = fixture.bound.enable_vector_feed(new_lineage=True)
        assert (
            refused.value.code,
            kept.diverged,
            restarted.diverged,
            restarted.epoch != kept.epoch,
        ) == ("vector-feed-diverged", True, False, True), (
            "a diverged feed fell back, was hidden, or was cleared without a new lineage"
        )
    finally:
        fixture.close()


def test_an_unreconcilable_load_fails_closed_with_its_own_code(tmp_path: Path) -> None:
    """Failure mode: a cache integrity failure that Palace raises as its plain error is
    reported as an ordinary search failure, or is answered by fallback, while the exact
    search still answers."""
    fixture = _shelf(tmp_path)
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="fallback")
    try:
        # Outside every writer: a vector row removed without touching chunks, which the
        # feed cannot detect and the cache's count reconciliation must.
        connection = sqlite3.connect(fixture.bound._shelf.chunks_db)
        try:
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            with connection:
                (chunk_id,) = connection.execute(
                    "SELECT chunk_id FROM chunks_vec LIMIT 1"
                ).fetchone()
                connection.execute("DELETE FROM chunks_vec WHERE chunk_id = ?", (chunk_id,))
        finally:
            connection.close()
        exact = _search(fixture.bound)
        with pytest.raises(ConsumerError) as refused:
            _search(fixture.bound, acceleration)
        assert (refused.value.code, bool(exact.hits), fixture.bound.vector_status().diverged) == (
            "vector-cache-integrity",
            True,
            False,
        ), "an unreconcilable cache load was not reported as its own integrity failure"
    finally:
        fixture.close()


@pytest.mark.parametrize("blocker", ("read-only", "busy"))
def test_feed_writes_need_the_writer(tmp_path: Path, blocker: str, monkeypatch) -> None:
    """Failure mode: a feed write bypasses the shelf's one writer, or a repeated enable
    starts a new lineage and so hides a divergence."""
    fixture = _shelf(tmp_path)
    try:
        epoch = fixture.bound.vector_status().epoch
        assert fixture.bound.enable_vector_feed().epoch == epoch, "a repeated enable reset"
        if blocker == "read-only":
            fixture.bound.checkpoint()
            fixture.reopen(read_only=True)
            with pytest.raises(ConsumerError) as refused:
                fixture.bound.enable_vector_feed(new_lineage=True)
            expected = "read-only"
        else:
            import biblios.consumer as consumer

            monkeypatch.setattr(
                consumer,
                "writer_lock",
                lambda *args, **kwargs: writer_lock(*args, **kwargs, timeout=0),
            )
            with writer_lock(fixture.binding.palace_root, operation="test-holder"):
                with pytest.raises(ConsumerError) as refused:
                    fixture.bound.disable_vector_feed()
            expected = "writer-busy"
        assert (refused.value.code, fixture.bound.vector_status().epoch) == (expected, epoch)
    finally:
        fixture.close()


def test_a_malformed_feed_fails_closed_with_its_own_code(tmp_path: Path) -> None:
    """Failure mode: a malformed feed state row (raised as Palace's plain error from its
    feed code) is reported as an ordinary or generic failure by search or status."""
    fixture = _shelf(tmp_path)
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="fallback")
    try:
        connection = sqlite3.connect(fixture.bound._shelf.chunks_db)
        try:
            with connection:
                connection.execute("DELETE FROM chunks_vec_feed_state")
        finally:
            connection.close()
        with pytest.raises(ConsumerError) as searched:
            _search(fixture.bound, acceleration)
        with pytest.raises(ConsumerError) as status:
            fixture.bound.vector_status()
        assert (searched.value.code, status.value.code) == (
            "vector-feed-malformed",
            "vector-feed-malformed",
        ), "a malformed feed was not reported with its own code"
    finally:
        fixture.close()


@pytest.mark.parametrize("failure", ("budget", "diverged"))
def test_a_stack_search_refuses_with_the_members_typed_code(tmp_path: Path, failure: str) -> None:
    """Failure mode: the stack seam maps a cache refusal or a member's divergence to the
    released anonymous search failure, unlike the single-shelf seam."""
    fixtures = [_shelf(tmp_path / name) for name in ("one", "two")]
    if failure == "budget":
        acceleration = VectorAcceleration(budget_bytes=1, on_unavailable="refuse")
    else:
        acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="fallback")
        connection = sqlite3.connect(fixtures[1].bound._shelf.chunks_db)
        try:
            with connection:
                columns = [row[1] for row in connection.execute("PRAGMA table_info(chunks)")]
                listed = ", ".join(columns)
                row = connection.execute(f"SELECT {listed} FROM chunks LIMIT 1").fetchone()
                values = list(row)
                values[columns.index("chunk_id")] = "outside-the-feed"
                marks = ", ".join("?" for _ in columns)
                connection.execute(f"INSERT INTO chunks ({listed}) VALUES ({marks})", values)
        finally:
            connection.close()
    try:
        stack = open_bound_stack(tuple(fixture.bound for fixture in fixtures))
        with pytest.raises(ConsumerError) as refused:
            _search(stack, acceleration)
        expected = "vector-cache-budget" if failure == "budget" else "vector-feed-diverged"
        assert refused.value.code == expected, "the stack seam lost the member's typed code"
    finally:
        for fixture in fixtures:
            fixture.close()


def test_warming_an_unbuilt_shelf_loads_nothing(tmp_path: Path) -> None:
    """Failure mode: warming a shelf that has not been indexed yet raises a search
    failure, although a search of the same shelf reports an empty route."""
    fixture = consumer_fixture(tmp_path)
    acceleration = VectorAcceleration(budget_bytes=BUDGET, on_unavailable="refuse")
    try:
        fixture.bound.curate(fixture.request(data=b"pending unbuilt source"))
        assert (acceleration.warm(fixture.bound), _resident(acceleration)) == (False, set())
    finally:
        fixture.close()
