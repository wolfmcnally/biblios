"""Bounded concurrency (ONE-03): independent entries' provider calls overlap up to the
smaller of the policy's bound and the embedder's declared ``max_concurrency``, while
everything else, every caller callback, store write and commit, stays on the calling
thread in entry order."""

from __future__ import annotations

import dataclasses
import math
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
from biblios.testing import HashEmbedder, OverlapReranker
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


# ---- provider throttling --------------------------------------------------------------


class _Scripted(HashEmbedder):
    """A provider that answers like :class:`HashEmbedder` except where a rule's marker
    appears in a slice: the rule runs first and may wait or raise, and a callable it
    returns rewrites the answer. ``started`` lists every slice the call actually
    submitted, in start order."""

    def __init__(self, capability: int, rules=None) -> None:
        super().__init__()
        self.max_concurrency = capability
        self.rules = rules or {}
        self.lock = threading.Lock()
        self.started: list[tuple[str, ...]] = []

    def embed(self, texts):
        with self.lock:
            self.started.append(tuple(texts))
        rewrites = [
            rule() for marker, rule in self.rules.items() if any(marker in t for t in texts)
        ]
        with self.lock:
            answer = super().embed(texts)
        for rewrite in rewrites:
            if callable(rewrite):
                answer = rewrite(answer)
        return answer

    def sent(self, marker: str) -> int:
        return sum(1 for call in self.started if any(marker in text for text in call))


def _sections(name: str, count: int, pad: int = 0, word: str = "tide") -> bytes:
    """One heading row per section, so ``count`` fixes the slice count; ``pad`` only
    grows the bytes, which order the entries."""
    filler = "x" * pad
    return (
        f"# {name}\n\n{filler}\n\n"
        + "".join(f"## {name} {j}\n\nmark {name}-s{j:02d} {word}\n\n" for j in range(count))
    ).encode()


def _throttle(*, delivery: str = "rejected", hint: float | None = None):
    from palace.index import ProviderThrottledError

    return ProviderThrottledError(
        "provider throttled",
        provider="test:local",
        operation="embed",
        kind="throttled",
        delivery=delivery,
        code="ThrottlingException",
        attempts=4,
        stop="attempts-exhausted",
        retry_after=hint,
        retry_not_before=None if hint is None else time.monotonic() + hint,
    )


def _poison(value: float):
    """A rule result that puts ``value`` into the first vector of the answer."""

    def rewrite(answer):
        answer[0][0] = value
        return answer

    return rewrite


def _raises(error: BaseException, before=lambda: None):
    def rule() -> None:
        before()
        raise error

    return rule


@pytest.mark.parametrize("capability", [1, 2], ids=["serial", "overlapped"])
def test_a_definite_throttle_requeues_its_entry_and_stops_all_fresh_work(tmp_path, capability):
    """Failure mode: a throttle that provably performed nothing still charges the entry
    as uncertain; the call keeps sending slices or visiting entries after the throttle
    is observable (here while the head's earlier slice is still outstanding); the
    accepted stage is disturbed; the entry is lost for the next call; or the recorded
    result changes shape, or a replay renews the retry hint."""
    import json

    from biblios.core import _result_document, entry_status

    shelf, settings = make_shelf(tmp_path)
    caller = _Owner()
    with shelf:
        bound = BoundShelf(shelf.local_binding(), shelf)
        b = shelf.add("b.md", _sections("b", 40)).affected_entry_ids[0]
        c = shelf.add("c.md", _sections("c", 2, pad=1500)).affected_entry_ids[0]
        first = _run(shelf, settings, _Scripted(capability), caller, bound=4)
        caller.head.update(item.stage_sha256 for item in first.accepted)
        before = entry_status(bound, b).accepted_stage_sha256
        thrown = threading.Event()
        rules = {"b-s24": _raises(_throttle(hint=30.0), thrown.set)}
        if capability > 1:
            # The head's first slice is still outstanding when the throttle lands.
            rules["b-s05"] = lambda: (thrown.wait(2), time.sleep(0.05))
        embedder = _Scripted(capability, rules)
        operation = str(uuid.uuid4())
        reembed = (b, c)
        result = _run(
            shelf, settings, embedder, caller, bound=4, operation=operation, reembed=reembed
        )
        outcomes = {item.entry_id: (item.outcome, item.code) for item in result.entries}
        assert outcomes.pop(b) == ("pending", "provider-throttled")
        # Serial: no further entry is visited. Overlapped: c is either never admitted or
        # admitted and stopped, depending on when the throttle lands; never submitted.
        assert outcomes in ({}, {c: ("pending", "provider-throttled")})
        assert capability > 1 or outcomes == {}
        assert embedder.sent("b-s38") == 0 and embedder.sent("mark c-s") == 0
        assert result.throttled and result.accepted == ()
        assert result.provider_calls == 1  # the first slice returned and was checked
        local = result.local
        assert (local.invocations_failed, local.answers_discarded, local.inputs_discarded) == (
            1,
            1,
            16,
        )
        assert 0 < local.retry_after <= 30.0 and local.usage is None
        assert entry_status(bound, b).accepted_stage_sha256 == before
        hits = shelf.search("tide", embedder=HashEmbedder(), reranker=OverlapReranker()).hits
        assert b in {hit.entry_id for hit in hits}  # the accepted stage is still served
        assert entry_status(bound, b).dirty and entry_status(bound, c).dirty
        stored = json.loads(_result_document(result))
        assert set(stored) == {
            "operation_id",
            "revision",
            "entries",
            "accepted",
            "yielded",
            "replayed",
            "provider_calls",
            "swept",
            "redacted",
        }
        assert {key for item in stored["entries"] for key in item} == {
            "entry_id",
            "outcome",
            "code",
            "stage_sha256",
            "embedded",
            "reused",
        }
        replay = _run(
            shelf, settings, embedder, caller, bound=4, operation=operation, reembed=reembed
        )
        assert replay.replayed and replay.entries == result.entries
        assert replay.throttled and replay.local is None
        retry = _Scripted(capability)
        again = _run(shelf, settings, retry, caller, bound=4, reembed=reembed)
        assert {item.entry_id: item.outcome for item in again.entries} == {
            b: "searchable",
            c: "searchable",
        }
        assert retry.sent("b-s38") == 1 and not again.throttled
    assert caller.foreign == [] and _provider_threads() == []


@pytest.mark.parametrize("throttle_first", [True, False], ids=["throttle-first", "timeout-first"])
def test_each_entry_is_classified_once_after_its_slices_drain(tmp_path, throttle_first):
    """Failure mode: the first failure to complete decides the entry, so a definite
    throttle hides another slice's timeout (or the reverse) depending on timing; a
    throttle and a genuine failure in one call are conflated; or a sibling whose slices
    were all submitted before the throttle is not committed."""
    from palace.index import ProviderCallError

    timeout = ProviderCallError(
        "read timed out",
        provider="test:local",
        operation="embed",
        kind="timeout",
        delivery="unknown",
        code="ReadTimeout",
        attempts=1,
        stop="not-retryable",
    )
    barrier = threading.Barrier(7, timeout=2)
    first = threading.Event()

    def leading() -> None:
        barrier.wait()
        first.set()

    def following() -> None:
        barrier.wait()
        first.wait(2)
        time.sleep(0.05)

    throttle = _throttle()
    rules = {
        "m-s03": _raises(throttle, leading if throttle_first else following),
        "m-s18": _raises(timeout, following if throttle_first else leading),
        "n-s01": _raises(RuntimeError("provider failed"), barrier.wait),
        "o-s01": _raises(_throttle(), barrier.wait),
        "p-s01": barrier.wait,
        # A definite throttle on the first slice closes q before its second slice is
        # taken; the second slice's invalid answer must still make q uncertain.
        "q-s03": _raises(_throttle(), barrier.wait),
        "q-s18": lambda: (barrier.wait(), _poison(math.nan))[1],
    }
    shelf, settings = make_shelf(tmp_path)
    caller = _Owner()
    with shelf:
        ids = {
            name: shelf.add(f"{name}.md", _sections(name, count)).affected_entry_ids[0]
            for name, count in (("m", 20), ("n", 2), ("o", 2), ("p", 2), ("q", 20))
        }
        embedder = _Scripted(7, rules)
        result = _run(shelf, settings, embedder, caller, bound=7)
    outcomes = {item.entry_id: (item.outcome, item.code) for item in result.entries}
    assert len(result.entries) == len(outcomes) == 5
    assert outcomes == {
        ids["m"]: ("uncertain", "provider-throttled"),
        ids["n"]: ("uncertain", "callback-failed"),
        ids["o"]: ("pending", "provider-throttled"),
        ids["p"]: ("searchable", None),
        ids["q"]: ("uncertain", "provider-throttled"),
    }
    assert result.outcome(ids["m"]).cause.__cause__ is throttle  # slice order, not timing
    q_second = sum(len(call) for call in embedder.started if any("q-s18" in t for t in call))
    local = result.local
    assert (local.invocations_failed, result.provider_calls) == (5, 2)
    assert (local.answers_discarded, local.inputs_discarded) == (1, q_second)
    assert len(embedder.started) == 7 and result.accepted[0].entry_id == ids["p"]
    assert caller.foreign == [] and _provider_threads() == []


@pytest.mark.parametrize("bound", [1, 4], ids=["serial", "overlapped"])
def test_a_providers_own_consumer_error_stays_an_uncertain_callback_failure(shelf, bound):
    """Failure mode: an error the supplied embedder raises in Biblios's own shape (effect
    ``none``) is taken for a definite Biblios check, so a genuine provider failure is
    requeued as ``pending`` instead of charged as uncertain."""
    shelf, settings, ids = shelf
    shaped = ConsumerError("deadline-exceeded", stage="embedding")
    embedder = _Scripted(4, {"harbour 3 ": _raises(shaped)})
    result = _run(shelf, settings, embedder, _Owner(), bound=bound)
    outcome = result.outcome(ids[3])
    assert (outcome.outcome, outcome.code) == ("uncertain", "callback-failed")
    assert outcome.cause.__cause__ is shaped
    assert result.local.invocations_failed == 1


@pytest.mark.parametrize("throttle_first", [True, False], ids=["throttle-first", "malformed-first"])
def test_a_malformed_answer_beside_a_throttle_is_contained(tmp_path, throttle_first):
    """Failure mode: an answer of the wrong shape (none at all, or a component that is
    not a number) raises out of the whole call instead of failing its entry, whether
    it is taken in order or drained after a throttle closed the entry; or it hides the
    throttle, is published, or escapes the accounting."""
    barrier = threading.Barrier(8, timeout=2)

    def throttling() -> None:
        barrier.wait()
        if not throttle_first:
            time.sleep(0.1)

    def malformed(rewrite):
        def rule():
            barrier.wait()
            if throttle_first:
                time.sleep(0.1)
            return rewrite

        return rule

    nothing = lambda answer: None  # noqa: E731
    rules = {
        # A throttled first slice closes r and s; their malformed answers are drained.
        "r-s03": _raises(_throttle(), throttling),
        "r-s18": malformed(nothing),
        "s-s03": _raises(_throttle(), throttling),
        "s-s18": malformed(_poison("tide")),
        # A malformed first slice is taken in order, before t's and u's throttle.
        "t-s03": malformed(nothing),
        "t-s18": _raises(_throttle(), throttling),
        "u-s03": malformed(_poison("tide")),
        "u-s18": _raises(_throttle(), throttling),
    }
    shelf, settings = make_shelf(tmp_path)
    caller = _Owner()
    with shelf:
        ids = [
            shelf.add(f"{name}.md", _sections(name, 20)).affected_entry_ids[0] for name in "rstu"
        ]
        embedder = _Scripted(8, rules)
        result = _run(shelf, settings, embedder, caller, bound=8)
    assert {item.entry_id: (item.outcome, item.code) for item in result.entries} == {
        entry_id: ("uncertain", "provider-throttled") for entry_id in ids
    }
    assert len(result.entries) == 4 and result.accepted == ()
    malformed_inputs = sum(
        len(call)
        for call in embedder.started
        for marker in ("r-s18", "s-s18", "t-s03", "u-s03")
        if any(marker in text for text in call)
    )
    local = result.local
    assert (local.invocations_failed, result.provider_calls) == (4, 4)
    assert (local.answers_discarded, local.inputs_discarded) == (4, malformed_inputs)
    assert caller.foreign == [] and _provider_threads() == []


@pytest.mark.parametrize("case", ["unstorable-answer", "assembly-fails"])
@pytest.mark.parametrize("capability", [1, 2], ids=["serial", "overlapped"])
def test_an_answer_that_cannot_be_assembled_is_reported_once_and_never_published(
    tmp_path, monkeypatch, capability, case
):
    """Failure mode: a finite answer that float32 cannot hold, or any failure while the
    entry's rows are assembled, leaves the entry looking complete, so it is reported
    twice, handed to the authority and published, or its answers are not accounted."""
    import biblios.core
    from biblios.core import entry_status

    shelf, settings = make_shelf(tmp_path)
    caller = _Owner()
    with shelf:
        bound = BoundShelf(shelf.local_binding(), shelf)
        b = shelf.add("b.md", _sections("b", 20)).affected_entry_ids[0]
        first = _run(shelf, settings, _Scripted(capability), caller, bound=4)
        caller.head.update(item.stage_sha256 for item in first.accepted)
        before = entry_status(bound, b).accepted_stage_sha256
        authorized = list(caller.authorized)
        rules = {}
        if case == "unstorable-answer":
            rules["b-s18"] = lambda: _poison(1e100)
        else:

            def broken(vector):
                raise RuntimeError("assembly failed")

            monkeypatch.setattr(biblios.core, "_float32", broken)
        embedder = _Scripted(capability, rules)
        result = _run(shelf, settings, embedder, caller, bound=4, reembed=(b,))
        monkeypatch.undo()
        expected = (
            ("uncertain", "embedding-result-invalid")
            if case == "unstorable-answer"
            else ("pending", "prepare-failed")
        )
        assert [(item.entry_id, item.outcome, item.code) for item in result.entries] == [
            (b, *expected)
        ]
        assert caller.authorized == authorized and result.accepted == ()
        assert entry_status(bound, b).accepted_stage_sha256 == before
        sent = [len(call) for call in embedder.started]
        assert len(sent) == 2
        assert (result.local.answers_discarded, result.local.inputs_discarded) == (2, sum(sent))
    assert caller.foreign == [] and _provider_threads() == []


@pytest.mark.parametrize("refusal", ["authorize", "promote"])
@pytest.mark.parametrize("capability", [1, 2], ids=["serial", "overlapped"])
def test_answers_that_never_reach_a_persisted_stage_are_counted_discarded(
    tmp_path, capability, refusal
):
    """Failure mode: answers assembled into an entry that the authority refuses before
    its stage is persisted are not counted as discarded, or answers in a persisted,
    resumable stage are."""
    from biblios.core import StageRefused

    class _Refusing(_Owner):
        armed = False

        def authorize(self, stage):
            if self.armed:
                raise StageRefused("refused-by-caller")
            return super().authorize(stage)

    shelf, settings = make_shelf(tmp_path)
    caller = _Refusing()
    with shelf:
        b = shelf.add("b.md", _sections("b", 20)).affected_entry_ids[0]
        first = _run(shelf, settings, _Scripted(capability), caller, bound=4)
        caller.head.update(item.stage_sha256 for item in first.accepted)
        if refusal == "authorize":
            caller.armed = True
        else:
            caller.release = False  # promotion is held after the stage is persisted
        embedder = _Scripted(capability)
        result = _run(shelf, settings, embedder, caller, bound=4, reembed=(b,))
    outcome = result.outcome(b)
    sent = [len(call) for call in embedder.started]
    assert len(sent) == 2 and result.provider_calls == 2
    if refusal == "authorize":
        assert (outcome.outcome, outcome.code) == ("pending", "refused-by-caller")
        assert (result.local.answers_discarded, result.local.inputs_discarded) == (2, sum(sent))
    else:
        assert (outcome.outcome, outcome.code) == ("busy", "held")
        assert (result.local.answers_discarded, result.local.inputs_discarded) == (0, 0)


def test_pooled_calls_carry_the_callers_context_and_count_only_their_own_usage(tmp_path):
    """Failure mode: a pooled provider call loses the caller's context (Palace's usage
    collector, call scope, or the caller's own variables); one copied context is entered
    by two calls; or two simultaneous calls count each other's physical requests, or an
    enclosing collector counts a request twice or not at all."""
    import contextvars

    from palace.index.egress import EmbeddingUsage, current_usage, usage_scope
    from palace.index.pacing import current_scope

    mark: contextvars.ContextVar[str | None] = contextvars.ContextVar("mark", default=None)
    entered: contextvars.ContextVar[bool] = contextvars.ContextVar("entered", default=False)
    meet = threading.Barrier(2, timeout=2)

    class _Recording(_Scripted):
        def __init__(self) -> None:
            super().__init__(2)
            self.seen: list[tuple[str | None, float | None, bool]] = []
            self.met = False

        def embed(self, texts):
            with self.lock:
                self.seen.append((mark.get(), current_scope().deadline, entered.get()))
                first, self.met = not self.met, True
            entered.set(True)
            if first:
                meet.wait()  # both shelves' calls are in flight together
            usage = current_usage()
            usage.record(ok=False, prompt_tokens=None, cost_usd=None, throttled=True)
            usage.record(ok=True, prompt_tokens=len(texts), cost_usd=None)
            return super().embed(texts)

    results: dict[str, tuple] = {}

    def run(name: str, entries: int, deadline: float) -> None:
        shelf, settings = make_shelf(tmp_path / name)
        with shelf:
            for i in range(entries):
                shelf.add(f"{name}{i}.md", _sections(f"{name}{i}", 2, pad=i))
            embedder = _Recording()
            outer = EmbeddingUsage()
            outer.record(ok=True, prompt_tokens=None, cost_usd=None)  # before this call
            mark.set(name)
            with usage_scope(outer):
                result = _run(
                    shelf,
                    settings,
                    embedder,
                    _Owner(),
                    bound=2,
                    control=CallControl(deadline=deadline),
                )
            results[name] = (result, embedder, outer.summary())

    deadline = time.monotonic() + 120
    threads = [
        threading.Thread(target=run, args=("x", 3, deadline)),
        threading.Thread(target=run, args=("y", 2, deadline + 1)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    for name, entries, own in (("x", 3, deadline), ("y", 2, deadline + 1)):
        result, embedder, outer = results[name]
        assert {item.outcome for item in result.entries} == {"searchable"}
        assert embedder.seen == [(name, own, False)] * entries
        usage = result.local.usage
        assert (usage.requests, usage.failed_requests, usage.throttled_requests) == (
            2 * entries,
            entries,
            entries,
        )
        # The enclosing collector counts each of this call's requests once, beside its own.
        assert (outer.requests, outer.throttled_requests) == (usage.requests + 1, entries)
    assert _provider_threads() == []


@pytest.mark.parametrize(
    ("delivery", "outcome"), [("not-sent", "pending"), ("unknown", "uncertain")]
)
def test_a_deadline_reached_during_an_outstanding_call_waits_for_it(tmp_path, delivery, outcome):
    """Failure mode: the call returns, or reports, while a provider call it made is still
    running; or a provider stopped by the deadline is charged as uncertain when nothing
    was sent, or as definite when an earlier attempt may have been processed."""
    from palace.index import ProviderCallError

    # Long enough for conversion and planning to finish before the call is sent.
    deadline = time.monotonic() + 2.5
    finished: list[float] = []

    def late() -> None:
        assert time.monotonic() < deadline, "the provider call started after the deadline"
        time.sleep(max(0.0, deadline - time.monotonic()) + 0.1)
        finished.append(time.monotonic())

    stopped = ProviderCallError(
        "stopped by deadline",
        provider="test:local",
        operation="embed",
        kind="deadline",
        delivery=delivery,
        code="deadline",
        attempts=0 if delivery == "not-sent" else 1,
        stop="interrupted",
    )
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        entry_id = shelf.add("d.md", _sections("d", 2)).affected_entry_ids[0]
        pipeline = ConsumerPipeline(
            converter=drawbridge_converter(settings),
            renderer=envelope_renderer,
            input_view=None,
            policy=dataclasses.replace(default_policy(), max_concurrency=2),
            embedder=_Scripted(2, {"d-s01": _raises(stopped, late)}),
        )
        caller = _Owner()
        with caller.owner:
            result = bring_current(
                BoundShelf(shelf.local_binding(), shelf),
                BringCurrentRequest(str(uuid.uuid4()), None),
                pipeline=pipeline,
                authority=caller,
                control=CallControl(deadline=deadline),
            )
        returned = time.monotonic()
    assert finished and returned >= finished[0] > deadline
    assert (result.outcome(entry_id).outcome, result.outcome(entry_id).code) == (
        outcome,
        "deadline-exceeded",
    )
    assert _provider_threads() == []
