"""Strict provider selection and completion fences over actual Palace retrieval."""

from __future__ import annotations

import math
import random
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from castle.namespace_environment import LocalSession
from palace.index import ProviderCallError
from palace.index.pacing import (
    AttemptFailure,
    PacedRequest,
    PacerRefused,
    RetryPolicy,
    current_scope,
    perform,
)

from biblios.consumer import open_bound_stack
from biblios.control import CallControl, ConsumerError
from biblios.curation import CurationRequest, DeleteEntry, RelocateEntry, ReplaceContent
from biblios.history import ExcisionRequest, ExcisionSelection, recover_excision
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import consumer_fixture, identifier, indexed_fixture, inventory


@pytest.mark.parametrize("damage", ("missing", "corrupt", "certificate"))
def test_damaged_admitted_index_never_becomes_an_unbuilt_member(
    tmp_path: Path, damage: str
) -> None:
    fixture = indexed_fixture(tmp_path)
    database = fixture.binding.palace_root / "index" / "chunks.sqlite"
    if damage == "missing":
        database.unlink()
    elif damage == "certificate":
        fixture.bound.checkpoint()
        connection = sqlite3.connect(database)
        try:
            with connection:
                connection.execute("DELETE FROM index_meta WHERE key = 'embed_model'")
        finally:
            connection.close()
    else:
        database.write_bytes(b"not a SQLite database")
    before = inventory(fixture.binding.root)
    embedder, reranker = HashEmbedder(), OverlapReranker()
    try:
        with pytest.raises(ConsumerError):
            fixture.bound.search("query-marker", embedder=embedder, reranker=reranker)
        assert (embedder.calls, reranker.calls, inventory(fixture.binding.root)) == (
            [],
            [],
            before,
        ), "damaged member was skipped or invoked providers/repair"
    finally:
        fixture.close()


@pytest.mark.parametrize("race", ("epoch", "deadline"))
def test_retired_epoch_or_expired_provider_return_never_accepts_hits(
    tmp_path: Path, race: str, monkeypatch
) -> None:
    fixture = indexed_fixture(tmp_path)
    clock = [1.0]
    performed = []
    requests = []
    cleanup_errors = []
    if race == "deadline":
        monkeypatch.setattr("biblios.control.time.monotonic", lambda: clock[0])
    else:
        import biblios.history as history

        original = history._disposition

        def capture_cleanup(*args, **kwargs):
            try:
                return original(*args, **kwargs)
            except Exception as error:
                cleanup_errors.append(
                    (type(error).__name__, getattr(error, "sqlite_errorcode", None))
                )
                raise

        monkeypatch.setattr(history, "_disposition", capture_cleanup)

    class Retiring(HashEmbedder):
        def embed(self, texts):
            if race == "epoch":
                request = ExcisionRequest(
                    identifier(),
                    fixture.bound.view.revision,
                    ExcisionSelection(metadata_entry_ids=(fixture.bound.files()[0].entry_id,)),
                    (),
                    fixture.binding.root / "successor",
                )
                proposed = fixture.bound.preview_excision(request)
                requests.append(proposed.request)
                try:
                    performed.append(fixture.bound.excise(proposed.request))
                except ConsumerError as error:
                    performed.append((error.code, error.effect, fixture.bound._closed))
                    raise
            else:
                clock[0] = 3.0
                performed.append("expired")
            return super().embed(texts)

    try:
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.search(
                "lens",
                embedder=Retiring(),
                reranker=OverlapReranker(),
                control=CallControl(deadline=2.0) if race == "deadline" else CallControl(),
            )
        if race == "deadline":
            assert (len(performed), fixture.bound.active_work, refused.value.code) == (
                1,
                0,
                "deadline-exceeded",
            ), "provider completion accepted an expired answer"
        else:
            # Palace prepares the query (expansion and embedding) before it admits the
            # store, so no query connection is live in this reentrant callback: the
            # cutover and its derived cleanup complete, and the search that was in flight
            # is refused on the retired binding without accepting a hit.
            assert (
                [type(item).__name__ for item in performed],
                cleanup_errors,
                refused.value.code,
                fixture.bound.active_work,
            ) == (["ExcisionResult"], [], "closed", 0), (
                "query/epoch race hid retirement, accepted hits, or lost cleanup"
            )
            assert performed[0].mutation.outcome == "committed" and fixture.bound._closed
            with pytest.raises(ConsumerError, match="closed"):
                fixture.bound.files()
            session = LocalSession(
                fixture.binding.namespace_root,
                fixture.binding.castle_root,
                fixture.binding.root / "namespace.lock",
            )
            recovered = recover_excision(fixture.binding, session, requests[0])
            assert (
                recovered.successor_binding.expected_cas.epoch_id
                != fixture.binding.expected_cas.epoch_id
            ), "epoch race did not recover actual retired storage"
    finally:
        fixture.close()


def test_only_supplied_members_one_union_rerank_and_explicit_unbuilt_lag(tmp_path: Path) -> None:
    fixtures = [indexed_fixture(tmp_path / name) for name in ("one", "two", "excluded")]
    empty = consumer_fixture(tmp_path / "unbuilt")
    try:
        empty.bound.curate(empty.request(data=b"pending unbuilt source"))
        stack = open_bound_stack((fixtures[0].bound, fixtures[1].bound, empty.bound))
        reranker = OverlapReranker()
        result = stack.search("lighthouse lens", embedder=HashEmbedder(), reranker=reranker)
        assert (
            {hit.shelf_id for hit in result.hits},
            len(reranker.calls),
            empty.binding.shelf_id in result.indexing,
            result.status[empty.binding.shelf_id].built,
        ) == (
            {fixtures[0].binding.shelf_id, fixtures[1].binding.shelf_id},
            1,
            True,
            False,
        ), "consumer stack leaked members, reranked separately, or hid unbuilt lag"
    finally:
        for fixture in (*fixtures, empty):
            fixture.close()


@pytest.mark.parametrize("failed_role", ("embedding", "reranking"))
def test_strict_failures_never_return_degraded_hits(tmp_path: Path, failed_role: str) -> None:
    fixture = indexed_fixture(tmp_path)

    class FailedEmbedding(HashEmbedder):
        def embed(self, texts):
            raise RuntimeError("query-source-name-path-marker")

    class FailedReranking(OverlapReranker):
        def score(self, query, texts):
            raise RuntimeError("query-source-name-path-marker")

    try:
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.search(
                "lens",
                embedder=FailedEmbedding() if failed_role == "embedding" else HashEmbedder(),
                reranker=FailedReranking() if failed_role == "reranking" else OverlapReranker(),
            )
        assert (refused.value.code, refused.value.effect, "marker" in str(refused.value)) == (
            "callback-failed",
            "none",
            False,
        ), "strict consumer provider failure degraded or leaked unsafe diagnostics"
    finally:
        fixture.close()


@pytest.mark.parametrize("when", ("embedding", "reranking"))
@pytest.mark.parametrize("mutation", ("rename", "replace", "delete"))
def test_a_hit_changed_during_the_query_is_answered_as_a_search_after_the_change(
    tmp_path: Path, mutation: str, when: str
) -> None:
    """Failure mode: a hit whose entry was renamed, replaced or deleted while the query
    embedded is served from its old state, or the change refuses the whole answer
    instead of being judged per hit exactly as a search issued after it would be."""
    fixture = indexed_fixture(tmp_path)
    entry = fixture.bound.files()[0]
    performed = []

    def mutate() -> None:
        if performed:
            return
        action = {
            "rename": RelocateEntry(entry.entry_id, fixture.root_id, "new.txt"),
            "replace": ReplaceContent(entry.entry_id, b"different body"),
            "delete": DeleteEntry(entry.entry_id),
        }[mutation]
        result = fixture.bound.curate(
            CurationRequest(identifier(), fixture.bound.view.revision, action)
        )
        performed.append(result.mutation.revision_after)

    class AfterRetrieval(OverlapReranker):
        # After retrieval, before the per-hit check: the window where the retrieved
        # rows and the entry's current record can disagree.
        def score(self, query, texts):
            if when == "reranking":
                mutate()
            return super().score(query, texts)

    class Mutating(HashEmbedder):
        def embed(self, texts):
            if when == "embedding":
                mutate()
            return super().embed(texts)

    try:
        found = fixture.bound.search("lens", embedder=Mutating(), reranker=AfterRetrieval())
        served = {hit.entry_id: hit.path for hit in found.hits}
        withheld = {item.entry_id: item.category for item in found.withheld}
        expected = {
            "rename": ({entry.entry_id: "new.txt"}, {}),
            "replace": ({}, {entry.entry_id: "not-accepted"}),
            "delete": ({}, {entry.entry_id: "not-live"}),
        }[mutation]
        assert (served, withheld, len(performed), fixture.bound.active_work) == (
            *expected,
            1,
            0,
        ), "a hit changed during the query was not judged as a later search judges it"
    finally:
        fixture.close()


@pytest.mark.parametrize("through", ("shelf", "stack"))
def test_an_unrelated_entry_indexed_during_the_query_does_not_refuse_the_answer(
    tmp_path: Path, through: str
) -> None:
    """Failure mode (observed by a consuming service): another document committed and
    indexed on the same shelf while the query embeds changes the shelf's accumulated
    view, and the whole answer is refused ``stale-view`` although every returned hit
    is current."""
    fixture = indexed_fixture(tmp_path)
    original = fixture.bound.files()[0]
    stack = open_bound_stack([fixture.bound]) if through == "stack" else None

    class Indexing(HashEmbedder):
        fired = False

        def embed(self, texts):
            if not Indexing.fired:
                Indexing.fired = True
                with ThreadPoolExecutor(1) as pool:

                    def ingest() -> None:
                        fixture.bound.curate(fixture.request("small.txt", b"a small new note"))
                        fixture.bound._shelf.reconcile(fixture.settings, embedder=HashEmbedder())

                    pool.submit(ingest).result()
            return super().embed(texts)

    try:
        target = stack if stack is not None else fixture.bound
        found = target.search("lens", embedder=Indexing(), reranker=OverlapReranker())
        assert Indexing.fired and len(fixture.bound.files()) == 2
        assert original.entry_id in {hit.entry_id for hit in found.hits}
    finally:
        fixture.close()


def test_active_callback_blocks_close_and_cancelled_late_result_is_refused(tmp_path: Path) -> None:
    fixture = indexed_fixture(tmp_path)
    entered, released, cancelled = Event(), Event(), Event()

    class Waiting(HashEmbedder):
        def embed(self, texts):
            entered.set()
            if not released.wait(10):
                raise RuntimeError("fixture provider deadline")
            return super().embed(texts)

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                fixture.bound.search,
                "lens",
                embedder=Waiting(),
                reranker=OverlapReranker(),
                control=CallControl(deadline=time.monotonic() + 10, cancelled=cancelled.is_set),
            )
            try:
                assert entered.wait(10), "consumer callback race window was not reached"
                active = fixture.bound.active_work
                with pytest.raises(ConsumerError) as busy:
                    fixture.bound.close()
                cancelled.set()
            finally:
                released.set()
            with pytest.raises(ConsumerError) as late:
                future.result(timeout=10)
        assert (active, busy.value.code, late.value.code, fixture.bound.active_work) == (
            1,
            "work-active",
            "cancelled",
            0,
        ), "consumer accepted late cancellation or closed active provider work"
    finally:
        released.set()
        fixture.close()


def test_dynamic_readonly_search_requires_writer_checkpoint_and_has_no_sidecars(
    tmp_path: Path,
) -> None:
    fixture = indexed_fixture(tmp_path)
    try:
        fixture.bound.checkpoint()
        fixture.bound.close()
        fixture.handle.close()
        before = inventory(fixture.binding.root)
        reader = fixture.reopen(read_only=True)
        result = reader.search("lens", embedder=HashEmbedder(), reranker=OverlapReranker())
        reader.close()
        assert (bool(result.hits), inventory(fixture.binding.root)) == (True, before), (
            "dynamic read-only search changed storage or created sidecars"
        )
    finally:
        fixture.close()


_MARKER = "query-source-name-path-marker"


class _RemoteEmbedder(HashEmbedder):
    """A remote embedder's request path: every embed runs Palace's own retry loop, whose
    attempts follow ``script`` (an ``AttemptFailure``, or ``None`` to answer), so the
    fence classifies the typed error Palace itself raises."""

    def __init__(self, *script, pacer=None, on_attempt=None) -> None:
        super().__init__()
        self.script, self.pacer, self.on_attempt = list(script), pacer, on_attempt
        self.attempts, self.sleeps, self.scopes = 0, [], []

    def embed(self, texts):
        self.scopes.append(current_scope())

        def send(info):
            self.attempts += 1
            if self.on_attempt is not None:
                self.on_attempt()
            step = self.script.pop(0) if self.script else None
            if step is not None:
                raise step
            return HashEmbedder.embed(self, texts)

        request = PacedRequest(
            "fixture:region", "fixture-model", "embed", "query", len(texts), 8 * len(texts)
        )
        policy = RetryPolicy(max_attempts=3, base_seconds=20.0, max_wait_seconds=30.0)
        return perform(
            send,
            request=request,
            policy=policy,
            pacer=self.pacer,
            rng=random.Random(0).random,
            sleep=self.sleeps.append,
        )


def _failure(kind: str, delivery: str, *, retryable: bool = True, hint=None) -> AttemptFailure:
    return AttemptFailure(
        f"{kind} {_MARKER}",
        kind=kind,
        delivery=delivery,
        code=f"Fixture{kind.title()}",
        retryable=retryable,
        retry_after=hint,
    )


class _RefusingPacer:
    def acquire(self, request, *, deadline, cancelled):
        raise PacerRefused(_MARKER)

    def settle(self, reservation, outcome, *, retry_after):
        raise AssertionError("a refused acquire has no reservation to settle")


@pytest.mark.parametrize(
    ("case", "through"),
    (
        ("definite-throttle", "shelf"),
        ("definite-throttle", "stack"),
        ("throttle-after-timeout", "shelf"),
        ("rejected-credential", "shelf"),
        ("pacer-refused", "shelf"),
        ("provider-raised-consumer-error", "shelf"),
    ),
)
def test_a_query_embedding_failure_is_a_throttle_only_when_nothing_was_performed(
    tmp_path: Path, case: str, through: str
) -> None:
    """Failure modes: a definite provider throttle reaches the caller as
    ``callback-failed`` with no hint, so a scheduler cannot tell it from a genuine
    failure; or the throttle class is trusted for certainty, so a throttle after an
    attempt that may have been processed is reported as a clean throttle; or every typed
    failure, or a provider's own ConsumerError, is passed through as Biblios's verdict;
    or provider text reaches the diagnostic."""
    fixture = indexed_fixture(tmp_path)
    embedder = {
        # A hint beyond the policy's cap ends the request at once with the uncapped hint.
        "definite-throttle": lambda: _RemoteEmbedder(_failure("throttled", "rejected", hint=60.0)),
        "throttle-after-timeout": lambda: _RemoteEmbedder(
            _failure("timeout", "unknown"), _failure("throttled", "rejected", hint=60.0)
        ),
        "rejected-credential": lambda: _RemoteEmbedder(
            _failure("rejected", "rejected", retryable=False)
        ),
        "pacer-refused": lambda: _RemoteEmbedder(pacer=_RefusingPacer()),
        "provider-raised-consumer-error": lambda: _RemoteEmbedder(
            on_attempt=lambda: (_ for _ in ()).throw(
                ConsumerError("deadline-exceeded", stage="embedding")
            )
        ),
    }[case]()
    reranker = OverlapReranker()
    target = open_bound_stack([fixture.bound]) if through == "stack" else fixture.bound
    try:
        with pytest.raises(ConsumerError) as refused:
            target.search("lens", embedder=embedder, reranker=reranker)
        error = refused.value
        hint = error.retry_after
        observed = (
            error.code,
            error.stage,
            error.effect,
            hint is not None and 59.0 < hint <= 60.0,
            hint is None,
            embedder.attempts,
            type(error.__cause__).__name__,
            _MARKER in str(error),
            reranker.calls,
            fixture.bound.active_work,
        )
        expected = {
            "definite-throttle": ("provider-throttled", True, False, 1, "ProviderThrottledError"),
            "throttle-after-timeout": ("callback-failed", False, True, 2, "ProviderThrottledError"),
            "rejected-credential": ("callback-failed", False, True, 1, "ProviderCallError"),
            "pacer-refused": ("pacer-refused", False, True, 0, "ProviderCallError"),
            "provider-raised-consumer-error": ("callback-failed", False, True, 1, "ConsumerError"),
        }[case]
        code, hinted, unhinted, attempts, cause = expected
        assert observed == (
            code,
            "embedding",
            "none",
            hinted,
            unhinted,
            attempts,
            cause,
            False,
            [],
            0,
        ), "query embedding failure misclassified, mis-hinted or leaked provider text"
    finally:
        fixture.close()


@pytest.mark.parametrize("case", ("deadline", "deadline-after-ambiguous", "cancelled", "infinite"))
def test_the_query_embedder_runs_under_the_calls_deadline_and_cancellation(
    tmp_path: Path, case: str
) -> None:
    """Failure modes: the search's deadline or cancellation never reaches Palace, so its
    retry loop sleeps and starts further attempts the caller has already given up on; a
    deadline that stops a request after an attempt that may have been processed is
    reported as a clean deadline; or a non-finite deadline, which CallControl treats as
    none, makes Palace refuse the scope and fails every search."""
    fixture = indexed_fixture(tmp_path)
    cancelled = Event()
    control = {
        # Palace's first backoff (10 to 20 seconds) cannot finish before this deadline,
        # which still leaves search setup ample time to reach the embedder.
        "deadline": CallControl(deadline=time.monotonic() + 10.0),
        "deadline-after-ambiguous": CallControl(deadline=time.monotonic() + 10.0),
        "cancelled": CallControl(cancelled=cancelled.is_set),
        "infinite": CallControl(deadline=math.inf),
    }[case]
    embedder = {
        "deadline": lambda: _RemoteEmbedder(_failure("connection", "not-sent")),
        "deadline-after-ambiguous": lambda: _RemoteEmbedder(_failure("timeout", "unknown")),
        "cancelled": lambda: _RemoteEmbedder(
            _failure("connection", "not-sent"), on_attempt=cancelled.set
        ),
        "infinite": lambda: _RemoteEmbedder(_failure("connection", "not-sent")),
    }[case]()
    try:
        if case == "infinite":
            found = fixture.bound.search(
                "lens", embedder=embedder, reranker=OverlapReranker(), control=control
            )
            assert (bool(found.hits), embedder.attempts, embedder.scopes[0].deadline) == (
                True,
                2,
                None,
            ), "a non-finite deadline refused the search or bounded Palace"
            return
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.search(
                "lens", embedder=embedder, reranker=OverlapReranker(), control=control
            )
        scope = embedder.scopes[0]
        expected_code = {
            "deadline": "deadline-exceeded",
            "deadline-after-ambiguous": "callback-failed",
            "cancelled": "cancelled",
        }[case]
        assert (
            refused.value.code,
            refused.value.effect,
            refused.value.retry_after,
            isinstance(refused.value.__cause__, ProviderCallError),
            embedder.attempts,
            embedder.sleeps,
            scope.deadline == control.deadline,
            len(scope.cancelled) == (control.cancelled is not None),
            fixture.bound.active_work,
        ) == (expected_code, "none", None, True, 1, [], True, True, 0), (
            "the search's control did not bound Palace's retry loop, or overclaimed a deadline"
        )
    finally:
        fixture.close()
