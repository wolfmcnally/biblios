"""A failure keeps its origin: the translated error carries a stable code, stage and
effect, and its cause chain reaches the original exception and the frame that raised it,
whether that was a caller callback or a dependency."""

from __future__ import annotations

import time
import traceback
import uuid
from pathlib import Path

import pytest

from biblios.consumer import BoundShelf
from biblios.control import ConsumerError
from biblios.core import BringCurrentRequest, bring_current
from biblios.curation import CreateDirectory, CurationRequest
from biblios.pipeline import ConsumerPipeline
from biblios.representation import default_policy, envelope_renderer
from biblios.testing import HashEmbedder, OverlapReranker
from tests.consumer_fixtures import consumer_fixture, identifier, indexed_fixture
from tests.support import make_shelf


class _Origin(RuntimeError):
    pass


def _chain(error: BaseException) -> list[BaseException]:
    found = []
    while error is not None:
        found.append(error)
        error = error.__cause__
    return found


def _raising_frame(error: BaseException) -> traceback.FrameSummary:
    return traceback.extract_tb(error.__traceback__)[-1]


def test_a_failing_caller_callback_is_the_cause_and_names_its_own_frame(tmp_path: Path):
    fixture = indexed_fixture(tmp_path)
    origin = _Origin("provider unreachable")
    embedder = HashEmbedder()

    def _embed_query_that_fails(texts):
        raise origin

    embedder.embed = _embed_query_that_fails  # type: ignore[method-assign]
    try:
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.search("tide", embedder=embedder, reranker=OverlapReranker())
    finally:
        fixture.close()
    assert (refused.value.code, refused.value.stage) == ("callback-failed", "embedding")
    chain = _chain(refused.value)
    assert chain[-1] is origin
    assert _raising_frame(origin).name == "_embed_query_that_fails"


def test_a_dependency_refusal_keeps_its_code_and_its_own_frame(tmp_path: Path):
    fixture = consumer_fixture(tmp_path)
    try:
        create = CreateDirectory(fixture.root_id, "same")
        fixture.bound.curate(CurationRequest(identifier(), fixture.bound.view.revision, create))
        with pytest.raises(ConsumerError) as refused:
            fixture.bound.curate(CurationRequest(identifier(), fixture.bound.view.revision, create))
    finally:
        fixture.close()
    cause = refused.value.__cause__
    assert cause is not None and type(cause).__module__.startswith("castle")
    assert refused.value.code == cause.code  # the dependency's own code is the contract
    assert "/castle/" in _raising_frame(cause).filename


def test_an_entry_outcome_carries_its_cause_for_this_call_but_never_replays_it(
    tmp_path: Path,
):
    shelf, _settings = make_shelf(tmp_path)
    origin = _Origin("converter crashed")

    def _convert_that_fails(request):
        raise origin

    with shelf:
        entry_id = shelf.add("a.md", b"# A\n\nbody\n").affected_entry_ids[0]
        pipeline = ConsumerPipeline(
            converter=_convert_that_fails,
            renderer=envelope_renderer,
            input_view=None,
            policy=default_policy(),
            embedder=HashEmbedder(),
        )
        request = BringCurrentRequest(str(uuid.uuid4()), (entry_id,))
        bound = BoundShelf(shelf.local_binding(), shelf)
        first = bring_current(bound, request, pipeline=pipeline)
        outcome = first.outcome(entry_id)
        assert (outcome.outcome, outcome.code) == ("uncertain", "callback-failed")
        assert outcome.cause is origin
        assert _raising_frame(origin).name == "_convert_that_fails"
        replayed = bring_current(bound, request, pipeline=pipeline)
        assert replayed.replayed and replayed.outcome(entry_id).cause is None


def _provider_error(kind, delivery, *, stop="attempts-exhausted", hint=None, history=()):
    from palace.index import ProviderCallError, ProviderThrottledError

    cls = ProviderThrottledError if kind == "throttled" else ProviderCallError
    return cls(
        "provider said no",
        provider="test:local",
        operation="embed",
        kind=kind,
        delivery=delivery,
        code="Code",
        attempts=1,
        stop=stop,
        history=history,
        retry_after=hint,
        retry_not_before=None if hint is None else time.monotonic() + hint,
    )


def test_provider_failures_are_definite_only_when_nothing_was_performed():
    """Failure mode: the class of the last failure, rather than the delivery merged over
    every attempt, decides certainty, so a throttle after a timeout is requeued as if
    nothing happened; a definite refusal other than a throttle or a stop is called
    definite; any one uncertain slice fails to make the unit uncertain; or the hint is
    lost, renewed or taken from the wrong failure."""
    from palace.index._errors import AttemptRecord  # not exported by palace.index

    from biblios.control import ProviderOutcome, provider_outcome

    def judged(*failures, stopped=False):
        outcome = provider_outcome(failures, stopped_by_throttle=stopped)
        return outcome.definite, outcome.throttled, outcome.code

    throttled = _provider_error("throttled", "rejected", hint=5.0)
    assert judged(throttled) == (True, True, "provider-throttled")
    assert judged(_provider_error("throttled", "not-sent")) == (True, True, "provider-throttled")
    for delivery in ("unknown", "processed"):
        assert judged(_provider_error("throttled", delivery)) == (False, True, "provider-throttled")
    # An earlier throttle in the history still names the code; a refusal after it is not
    # a throttle, a stop or a deadline, so the request stays uncertain.
    earlier = (AttemptRecord("throttled", "rejected", "T", None),)
    refused = _provider_error("rejected", "rejected", stop="not-retryable", history=earlier)
    assert judged(refused) == (False, True, "provider-throttled")
    stops = {
        ("deadline", "interrupted"): "deadline-exceeded",
        ("cancelled", "interrupted"): "cancelled",
        ("cancellation-failed", "interrupted"): "cancellation-failed",
        ("pacer-refused", "pacer-refused"): "pacer-refused",
        ("unavailable", "beyond-deadline"): "deadline-exceeded",
    }
    for (kind, stop), code in stops.items():
        assert judged(_provider_error(kind, "not-sent", stop=stop)) == (True, False, code)
        assert judged(_provider_error(kind, "unknown", stop=stop)) == (False, False, code)
    denied = _provider_error("rejected", "rejected", stop="not-retryable")
    assert judged(denied) == (False, False, "callback-failed")
    assert judged(RuntimeError("boom")) == (False, False, "callback-failed")
    checked = ConsumerError("deadline-exceeded", stage="embedding")
    invalid = ConsumerError("embedding-result-invalid", stage="embedding", effect="uncertain")
    assert judged(checked) == (True, False, "deadline-exceeded")
    assert judged(checked, invalid) == (False, False, "embedding-result-invalid")
    assert judged(throttled, _provider_error("timeout", "unknown")) == (
        False,
        True,
        "provider-throttled",
    )
    assert judged(stopped=True) == (True, True, "provider-throttled")
    assert judged(checked, stopped=True) == (True, True, "provider-throttled")
    # Only the exception as raised is read: a wrapper is an ordinary failure.
    wrapped = RuntimeError("wrapped")
    wrapped.__cause__ = throttled
    assert judged(wrapped) == (False, False, "callback-failed")
    longest = provider_outcome([throttled, _provider_error("throttled", "rejected", hint=9.0)])
    assert isinstance(longest, ProviderOutcome) and 8.0 < longest.retry_after <= 9.0
    with pytest.raises(ValueError):
        provider_outcome([])
