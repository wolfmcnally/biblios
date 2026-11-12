"""Bounded stage queries: a keyed status for one stage digest that can answer
``absent``, paged pending stages, a keyed entry status, and a resume-only call that
finishes persisted stages and never starts preparation or provider work."""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest

from biblios import (
    BringCurrentRequest,
    StageRefused,
    bring_current,
    entry_status,
    pending_stages,
    resume_stages,
    stage_status,
)
from biblios.consumer import BoundShelf
from biblios.control import ConsumerError
from biblios.pipeline import ConsumerPipeline
from biblios.representation import default_policy, drawbridge_converter, envelope_renderer
from biblios.testing import HashEmbedder
from tests.support import make_shelf
from tests.test_core import _Holding


class _Caller(_Holding):
    """Records each stage it promotes as accepted; holds promotion until released."""

    def __init__(self) -> None:
        super().__init__()
        self.head = self.promoted


@pytest.fixture
def shelf(tmp_path: Path):
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        shelf.mkdir("notes")
        ids = [
            shelf.add(f"notes/n{i}.md", f"# N{i}\n\nharbour {i}\n".encode()).affected_entry_ids[0]
            for i in range(5)
        ]
        yield shelf, settings, ids


def _pipeline(settings, embedder):
    return ConsumerPipeline(
        converter=drawbridge_converter(settings),
        renderer=envelope_renderer,
        input_view=None,
        policy=default_policy(),
        embedder=embedder,
    )


def _held(shelf, settings, embedder, caller, entry_ids=None):
    bound = BoundShelf(shelf.local_binding(), shelf)
    bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), entry_ids),
        pipeline=_pipeline(settings, embedder),
        authority=caller,
    )
    return bound


def test_a_stage_digest_is_answered_by_keyed_reads_including_absent_and_cancelled(shelf):
    """Failure mode: an unknown digest is reported as some stage's, a persisted stage
    is not found by its digest, or an accepted one is reported as pending (or the
    reverse), so a caller acts on the wrong state."""
    shelf, settings, ids = shelf
    embedder, caller = HashEmbedder(), _Caller()
    bound = _held(shelf, settings, embedder, caller, (ids[0],))
    [pending] = pending_stages(bound).stages
    assert stage_status(bound, pending.stage_sha256) == pending
    assert pending.state == "authorized" and pending.entry_id == ids[0]
    assert stage_status(bound, "0" * 64).state == "absent"
    assert stage_status(bound, "0" * 64).entry_id is None
    caller.release = True
    bring_current(
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (ids[0],)),
        pipeline=_pipeline(settings, embedder),
        authority=caller,
    )
    bring_current(  # acknowledged: the retained stage is cleaned up
        bound,
        BringCurrentRequest(str(uuid.uuid4()), (ids[0],)),
        pipeline=_pipeline(settings, embedder),
        authority=caller,
    )
    recorded = stage_status(bound, pending.stage_sha256)
    assert (recorded.state, recorded.entry_id, recorded.serving) == ("recorded", ids[0], False)
    assert pending_stages(bound).stages == ()
    with pytest.raises(ConsumerError, match="request-invalid"):
        stage_status(bound, "not-a-digest")


def test_pending_stages_page_by_entry_without_repeats_and_answer_named_entries(shelf):
    """Failure mode: a traversal repeats or skips an entry whose stage stays persisted,
    a page exceeds its bound, the named form scans the whole shelf or accepts an
    unbounded name list, or a malformed page request is answered."""
    shelf, settings, ids = shelf
    bound = _held(shelf, settings, HashEmbedder(), _Caller())
    seen, after, pages = [], None, 0
    for _ in range(len(ids) + 1):  # a traversal that never ends is itself the failure
        page = pending_stages(bound, after=after, limit=2)
        assert len(page.stages) <= 2
        seen.extend(item.entry_id for item in page.stages)
        pages += 1
        if page.next_after is None:
            break
        after = page.next_after
    else:
        pytest.fail(f"the traversal did not end after {pages} pages: {seen}")
    assert seen == sorted(ids) and pages == 3
    named = pending_stages(bound, (ids[3], ids[1], "missing"), limit=3)
    assert [item.entry_id for item in named.stages] == sorted([ids[1], ids[3]])
    assert named.next_after is None
    for bad in (
        {"limit": 0},
        {"limit": 1001},
        {"limit": True},
        {"entry_ids": "notes/a"},
        {"entry_ids": tuple(ids), "limit": 2},
    ):
        with pytest.raises(ConsumerError, match="page-invalid"):
            pending_stages(bound, **bad)


def test_entry_status_reports_the_record_the_pending_stage_and_marks(shelf):
    """Failure mode: an entry's pending stage, record or mark is misreported, or an
    entry Biblios has never recorded is given a record."""
    shelf, settings, ids = shelf
    caller = _Caller()
    caller.authorize = lambda stage: (_ for _ in ()).throw(StageRefused("not-yet"))
    bound = _held(shelf, settings, HashEmbedder(), caller, (ids[0],))
    status = entry_status(bound, ids[0])
    assert (status.state, status.pending, status.refusal) == (None, None, "not-yet")
    assert status.dirty  # a pending outcome stays marked for the next call
    assert entry_status(bound, "no-such-entry").state is None
    with pytest.raises(ConsumerError, match="request-invalid"):
        entry_status(bound, "")


def test_a_resume_only_call_finishes_persisted_stages_and_starts_no_preparation(shelf):
    """Failure mode: the resume-only call converts, embeds or seals anything new,
    prepares an entry that has no persisted stage (or marks it), or prepares an entry
    whose stage settled while it still has later work; or it is not replayed."""
    shelf, settings, ids = shelf
    embedder, caller = HashEmbedder(), _Caller()
    bound = _held(shelf, settings, embedder, caller, (ids[0], ids[1]))
    path = shelf.mirror_path(ids[1])
    embedded, sealed = len(embedder.calls), len(caller.sealed)
    caller.release = True
    operation = str(uuid.uuid4())
    result = resume_stages(
        bound,
        operation,
        (ids[0], ids[2]),
        pipeline=_pipeline(settings, embedder),
        authority=caller,
    )
    outcomes = {item.entry_id: (item.outcome, item.code) for item in result.entries}
    assert outcomes == {ids[0]: ("searchable", None), ids[2]: ("pending", "no-stage")}
    assert len(embedder.calls) == embedded and len(caller.sealed) == sealed
    untouched = entry_status(bound, ids[2])
    assert (untouched.state, untouched.pending) == (None, None)
    # The resumed stage is kept, accepted, until a later call sees the caller's head
    # record it; the unnamed entry's stage is untouched.
    states = {item.entry_id: item.state for item in pending_stages(bound).stages}
    assert states == {ids[0]: "accepted", ids[1]: "authorized"}
    assert path.exists()
    replay = resume_stages(
        bound,
        operation,
        (ids[0], ids[2]),
        pipeline=_pipeline(settings, embedder),
        authority=caller,
    )
    assert replay.replayed and replay.entries == result.entries
    with pytest.raises(ConsumerError, match="request-invalid"):
        resume_stages(bound, str(uuid.uuid4()), (), pipeline=_pipeline(settings, embedder))
    with pytest.raises(ConsumerError, match="request-invalid"):
        bring_current(
            bound,
            BringCurrentRequest(str(uuid.uuid4()), (ids[0],), resume_only=True, sweep=True),
            pipeline=_pipeline(settings, embedder),
        )


def test_a_resume_only_call_leaves_an_entry_s_later_work_for_an_ordinary_call(shelf):
    """Failure mode: a stage that settles while its entry has since changed (here, a
    user edit of the accepted mirror) makes the resume-only call prepare or embed the
    edit; or a stage of a since-replaced source is acted on rather than left with the
    resume's own outcome."""
    shelf, settings, ids = shelf
    embedder, caller = HashEmbedder(), _Caller()
    caller.release = True
    bound = _held(shelf, settings, embedder, caller, (ids[0],))  # accepted, retained
    path = shelf.mirror_path(ids[0])
    path.write_bytes(path.read_bytes() + b"\nA user's own line.\n")
    embedded, sealed = len(embedder.calls), len(caller.sealed)
    edited = resume_stages(
        bound,
        str(uuid.uuid4()),
        (ids[0],),
        pipeline=_pipeline(settings, embedder),
        authority=caller,
    )
    assert [(item.outcome, item.code) for item in edited.entries] == [
        ("pending", "work-after-resume")
    ]
    assert len(embedder.calls) == embedded and len(caller.sealed) == sealed
    assert entry_status(bound, ids[0]).dirty  # kept for the ordinary call
    held = _Caller()
    _held(shelf, settings, embedder, held, (ids[1],))  # authorized, not promoted
    shelf.update("notes/n1.md", b"# N1\n\nharbour changed\n")
    embedded, sealed = len(embedder.calls), len(held.sealed)
    replaced = resume_stages(
        bound, str(uuid.uuid4()), (ids[1],), pipeline=_pipeline(settings, embedder), authority=held
    )
    assert [(item.outcome, item.code) for item in replaced.entries] == [
        ("pending", "source-replaced")
    ]
    assert len(embedder.calls) == embedded and len(held.sealed) == sealed
