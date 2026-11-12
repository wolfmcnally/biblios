"""Bounded concurrency (ONE-03): independent entries' provider calls overlap up to the
smaller of the policy's bound and the embedder's declared ``max_concurrency``, while
everything else, every caller callback, store write and commit, stays on the calling
thread in entry order."""

from __future__ import annotations

import dataclasses
import threading
import time
import uuid
from pathlib import Path

import pytest

from biblios.consumer import BoundShelf
from biblios.control import CallControl, ConsumerError
from biblios.core import BringCurrentRequest, bring_current
from biblios.pipeline import ConsumerPipeline
from biblios.representation import default_policy, drawbridge_converter, envelope_renderer
from biblios.testing import HashEmbedder
from tests.support import make_shelf
from tests.test_core import _Holding

PROVIDER_THREAD = "biblios-provider"


class _Declared(HashEmbedder):
    """A provider that declares it is safe from ``capability`` threads. It records the
    most calls it had outstanding at once and the threads it ran on; with ``meet`` it
    holds each call until that many are outstanding, so overlap is forced, not hoped for.
    ``fail`` names inputs whose call raises."""

    def __init__(self, capability: int, meet: int = 1, fail: str | None = None) -> None:
        super().__init__()
        self.max_concurrency = capability
        self.lock = threading.Lock()
        self.barrier = threading.Barrier(meet, timeout=2) if meet > 1 else None
        self.outstanding = self.most = 0
        self.threads: set[int] = set()
        self.fail = fail

    def embed(self, texts):
        with self.lock:
            self.outstanding += 1
            self.most = max(self.most, self.outstanding)
            self.threads.add(threading.get_ident())
        try:
            if self.barrier is not None:
                try:
                    self.barrier.wait()
                except threading.BrokenBarrierError:
                    pass  # the last group may be short
            time.sleep(0.01)
            with self.lock:
                vectors = super().embed(texts)
            if self.fail is not None and any(self.fail in text for text in texts):
                raise RuntimeError("provider failed")
            return vectors
        finally:
            with self.lock:
                self.outstanding -= 1


class _Owner(_Holding):
    """A caller whose state is owned by the thread that invoked it: it holds its own
    reentrant lock for the whole call, and every callback re-acquires it without
    blocking, which succeeds only on the owning thread."""

    def __init__(self) -> None:
        super().__init__()
        self.release = True
        self.owner = threading.RLock()
        self.foreign: list[str] = []
        self.seen: set[str] = set()

    def held(self, name: str) -> None:
        self.seen.add(name)
        if self.owner.acquire(blocking=False):
            self.owner.release()
        else:
            self.foreign.append(name)

    def authorize_creation(self, creation, entry_id):
        self.held("authorize_creation")
        return super().authorize_creation(creation, entry_id)

    def authorize(self, stage):
        self.held("authorize")
        return super().authorize(stage)

    def promote(self, stage, verified):
        self.held("promote")
        return super().promote(stage, verified)

    def recheck(self, stage, verified, authorization, promotion):
        self.held("recheck")
        return super().recheck(stage, verified, authorization, promotion)


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        ids = [
            shelf.add(
                f"notes/n{i}.md",
                (
                    f"# N{i}\n\n" + "".join(f"harbour {i} line {j}\n\n" for j in range(i * 9))
                ).encode(),
            ).affected_entry_ids[0]
            for i in range(6)
        ]
        yield shelf, settings, ids


def _run(shelf, settings, embedder, caller, *, bound, operation=None, control=None, **fields):
    converter = drawbridge_converter(settings)

    def converting(*args, **kwargs):
        caller.held("converter")
        return converter(*args, **kwargs)

    pipeline = ConsumerPipeline(
        converter=converting,
        renderer=envelope_renderer,
        input_view=None,
        policy=dataclasses.replace(default_policy(), max_concurrency=bound),
        embedder=embedder,
        progress=lambda progress: caller.held("progress"),
    )
    with caller.owner:
        return bring_current(
            BoundShelf(shelf.local_binding(), shelf),
            BringCurrentRequest(operation or str(uuid.uuid4()), None, **fields),
            pipeline=pipeline,
            authority=caller,
            control=control or CallControl(),
        )


def _provider_threads() -> list[threading.Thread]:
    return [t for t in threading.enumerate() if t.name.startswith(PROVIDER_THREAD)]


@pytest.mark.parametrize(
    ("bound", "capability", "most"),
    [(2, 8, 2), (8, 3, 3), (4, 1, 1)],
    ids=["policy-bound", "provider-bound", "provider-serial"],
)
def test_provider_calls_overlap_within_the_bound_and_all_else_stays_on_the_calling_thread(
    shelf, bound, capability, most
):
    """Failure mode: independent entries' provider calls never overlap, or exceed the
    smaller bound; a width-1 provider runs off the calling thread; any caller callback
    (conversion, authority, progress) runs on another thread, where it cannot hold the
    caller's own lock; or overlap changes the outcomes, their order or the replay."""
    shelf, settings, ids = shelf
    embedder = _Declared(capability, meet=most)
    caller = _Owner()
    operation = str(uuid.uuid4())
    result = _run(shelf, settings, embedder, caller, bound=bound, operation=operation)
    assert {item.entry_id: item.outcome for item in result.entries} == {
        entry_id: "searchable" for entry_id in ids
    }
    assert embedder.most == most
    assert caller.foreign == []
    assert {"converter", "authorize", "promote", "progress"} <= caller.seen
    if most == 1:
        assert embedder.threads == {threading.get_ident()}
    else:
        assert threading.get_ident() not in embedder.threads
    order = [item.entry_id for item in result.entries]
    assert order == sorted(order, key=lambda e: (shelf._live()[e].size or 0, e))
    assert [item.entry_id for item in result.accepted] == order
    assert _provider_threads() == []
    replay = _run(shelf, settings, embedder, caller, bound=bound, operation=operation)
    assert replay.replayed and dataclasses.replace(replay, replayed=False) == result


def test_a_failed_provider_call_settles_only_its_entry_and_is_never_repeated(shelf):
    """Failure mode: one entry's provider failure stops or corrupts its siblings' work,
    is resubmitted inside the call, or leaves a provider thread running."""
    shelf, settings, ids = shelf
    embedder = _Declared(4, fail="harbour 3 ")
    caller = _Owner()
    result = _run(shelf, settings, embedder, caller, bound=4)
    outcomes = {item.entry_id: (item.outcome, item.code) for item in result.entries}
    assert outcomes.pop(ids[3]) == ("uncertain", "callback-failed")
    assert set(outcomes.values()) == {("searchable", None)}
    assert any("harbour 3 " in text for call in embedder.calls for text in call)
    assert len(embedder.calls) == len(set(embedder.calls))  # no slice is ever sent twice
    assert caller.foreign == [] and _provider_threads() == []


def test_cancellation_waits_for_outstanding_calls_and_repeats_no_accepted_work(shelf):
    """Failure mode: a cancelled overlapped call returns while provider calls are still
    running, accepts or records partial work, or makes the next call embed again what
    an earlier call already accepted."""
    shelf, settings, ids = shelf
    embedder = _Declared(3)
    caller = _Owner()
    checks: list[int] = []

    def cancelled() -> bool:
        checks.append(1)
        return len(embedder.calls) >= 4

    with pytest.raises(ConsumerError, match="cancelled"):
        _run(shelf, settings, embedder, caller, bound=3, control=CallControl(cancelled=cancelled))
    assert _provider_threads() == [] and embedder.outstanding == 0
    from biblios.derived import DerivedStore

    store = DerivedStore(shelf.palace_store, read_only=True)
    accepted = {
        index
        for index, entry_id in enumerate(ids)
        if (record := store.record(entry_id)) is not None and record.accepted_stage_sha256
    }
    store.close()
    assert 0 < len(accepted) < len(ids)  # the call was cancelled part-way
    first = len(embedder.calls)
    again = _run(shelf, settings, embedder, caller, bound=3)
    assert {item.outcome for item in again.entries} == {"searchable"}
    resent = {
        index
        for call in embedder.calls[first:]
        for text in call
        for index in range(len(ids))
        if f"harbour {index} " in text or text.strip() == f"N{index}"
    }
    assert resent and not resent & accepted  # accepted work is never embedded again
    assert caller.foreign == [] and _provider_threads() == []


def test_yielding_starts_no_new_entry_and_finishes_every_started_one(shelf):
    """Failure mode: a yield abandons an entry whose provider work was dispatched, starts
    another entry, or loses the unreached entries for the next call."""
    shelf, settings, ids = shelf
    embedder = _Declared(3)
    caller = _Owner()
    asked: list[int] = []

    def should_yield() -> bool:
        asked.append(1)
        return len(asked) > 2

    converter = drawbridge_converter(settings)
    pipeline = ConsumerPipeline(
        converter=converter,
        renderer=envelope_renderer,
        input_view=None,
        policy=dataclasses.replace(default_policy(), max_concurrency=3),
        embedder=embedder,
    )
    with caller.owner:
        result = bring_current(
            BoundShelf(shelf.local_binding(), shelf),
            BringCurrentRequest(str(uuid.uuid4()), None),
            pipeline=pipeline,
            authority=caller,
            should_yield=should_yield,
        )
    assert result.yielded
    assert [item.outcome for item in result.entries] == ["searchable", "searchable"]
    assert _provider_threads() == []
    rest = _run(shelf, settings, embedder, caller, bound=3)
    assert {item.entry_id for item in rest.entries} == set(ids) - {
        item.entry_id for item in result.entries
    }


def test_outcomes_keep_entry_order_when_a_later_entry_settles_first(shelf):
    """Failure mode: a later entry settled at admission (here a stale caller intent)
    while an earlier one still waits on its provider is reported ahead of it, so the
    outcome order, and the recorded result, depend on provider timing."""
    from biblios.core import ExpectedEntry

    shelf, settings, ids = shelf
    caller = _Owner()
    stale = {ids[1]: ExpectedEntry(None, "0" * 64, None)}
    result = _run(shelf, settings, _Declared(3), caller, bound=3, expected_entries=stale)
    order = [item.entry_id for item in result.entries]
    assert result.outcome(ids[1]).code == "intent-mismatch"
    assert order == sorted(order, key=lambda e: (shelf._live()[e].size or 0, e))
    assert caller.foreign == [] and _provider_threads() == []
