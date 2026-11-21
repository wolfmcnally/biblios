"""One result per source at the bound seam: eligibility before grouping, same inputs,
no refill, and every selected hit re-read at assembly."""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from biblios import VectorAcceleration
from biblios.consumer import open_bound_stack
from biblios.control import CallControl, ConsumerError
from biblios.curation import CurationRequest, DeleteEntry, RelocateEntry, ReplaceContent
from biblios.distinct import DistinctSources
from biblios.testing import HashEmbedder
from tests.consumer_fixtures import consumer_fixture, identifier

SCORE = re.compile(r"score-(\d+)")


class ScriptedReranker:
    """Scores each candidate by its `score-NN` marker; records every call's exact texts."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def score(self, query, texts):
        self.calls.append(tuple(sorted(texts)))
        return [float(m.group(1)) if (m := SCORE.search(t)) else 0.0 for t in texts]

    def probe(self) -> None:
        return None

    def close(self) -> None:
        return None


def _section(score: int, word: str, term: str = "lighthouse") -> str:
    return f"{term} score-{score} {word}"


def _shelf(directory: Path, files: dict[str, list[int]]):
    fixture = consumer_fixture(directory)
    for name, scores in files.items():
        parts = [_section(scores[0], name)]
        parts += [f"## Part {i}\n\n{_section(s, name)}" for i, s in enumerate(scores[1:], 1)]
        fixture.bound.curate(fixture.request(f"{name}.txt", "\n\n".join(parts).encode()))
    fixture.bound._shelf.reconcile(fixture.settings, embedder=HashEmbedder())
    return fixture


def _search(fixture, limit, reranker, **options):
    return fixture.bound.search(
        "lighthouse", embedder=HashEmbedder(), reranker=reranker, limit=limit, **options
    )


def _stack_search(fixtures, limit, reranker, **options):
    stack = open_bound_stack(tuple(fixture.bound for fixture in fixtures))
    return stack.search(
        "lighthouse", embedder=HashEmbedder(), reranker=reranker, limit=limit, **options
    )


def _names(result):
    return [hit.path.removesuffix(".txt") for hit in result.hits]


def _entry_id(fixture, name: str) -> str:
    return next(entry.entry_id for entry in fixture.bound.files() if entry.path == name)


def _key_firing_on(name: str, action):
    """A per-entry-path key that runs `action` once, when it is called for `name`."""
    fired: list[bool] = []

    def key(candidate):
        if candidate.entry.path == name and not fired:
            fired.append(True)
            action()
        return candidate.entry.path

    return key


def _tamper(fixture, score: int) -> None:
    connection = sqlite3.connect(fixture.bound._shelf.chunks_db)
    with connection:
        connection.execute(
            "UPDATE chunks SET body = body || ' tampered' WHERE body LIKE ?",
            (f"%score-{score} %",),
        )
    connection.close()


def test_without_distinct_the_chunk_results_are_unchanged(tmp_path: Path) -> None:
    """Failure mode: the option leaks into an ordinary search (results grouped or a
    report attached). The unchanged existing suite is the main oracle; this checks the
    absent report directly."""
    fixture = _shelf(tmp_path, {"lead": [99, 98], "other": [90]})
    try:
        result = _search(fixture, 3, ScriptedReranker())
        assert (_names(result), result.distinct) == (["lead", "lead", "other"], None)
    finally:
        fixture.close()


def test_three_early_duplicates_and_later_groups_fill_the_limit(tmp_path: Path) -> None:
    """Failure mode: early duplicates consume the limit, or filling it needs a second
    rerank call."""
    files = {"lead": [99, 98, 97, 96]} | {f"doc{i}": [80 - i] for i in range(9)}
    fixture = _shelf(tmp_path, files)
    try:
        reranker = ScriptedReranker()
        result = _search(fixture, 10, reranker, distinct=DistinctSources())
        report = result.distinct
        assert (
            _names(result),
            len(reranker.calls),
            (report.walked, report.duplicates, report.selected, report.returned),
            report.underfilled,
        ) == (["lead"] + [f"doc{i}" for i in range(9)], 1, (13, 3, 10, 10), False)
    finally:
        fixture.close()


def test_distinct_sends_the_reranker_exactly_what_an_ordinary_search_sends(
    tmp_path: Path,
) -> None:
    """Failure mode: the option widens the candidate input or changes which texts reach
    the reranker. The corpus exceeds the candidate depth, so a widened pool would send
    more or different texts."""
    fixture = _shelf(tmp_path, {f"doc{i:02d}": [99 - i] for i in range(30)})
    try:
        plain, distinct = ScriptedReranker(), ScriptedReranker()
        _search(fixture, 5, plain)
        _search(fixture, 5, distinct, distinct=DistinctSources())
        assert (plain.calls == distinct.calls, len(plain.calls), len(plain.calls[0])) == (
            True,
            1,
            20,
        )
    finally:
        fixture.close()


def test_a_pool_bounded_below_the_shelf_underfills_without_refilling(tmp_path: Path) -> None:
    """Failure mode: a source-poor pool is refilled from deeper in the shelf (a second
    retrieval or a wider pool) instead of returning fewer results."""
    fixture = consumer_fixture(tmp_path)
    try:
        # Only the lead entry carries the query term, so its 25 sections fill the
        # 20-candidate pool; ten other entries exist outside it.
        lead = "\n\n".join(
            [_section(99, "lead")]
            + [f"## Part {i}\n\n{_section(99 - i, 'lead')}" for i in range(1, 25)]
        )
        fixture.bound.curate(fixture.request("lead.txt", lead.encode()))
        for i in range(10):
            text = _section(50 - i, f"other{i}", "harbour")
            fixture.bound.curate(fixture.request(f"other{i}.txt", text.encode()))
        fixture.bound._shelf.reconcile(fixture.settings, embedder=HashEmbedder())
        reranker, embedder = ScriptedReranker(), HashEmbedder()
        result = fixture.bound.search(
            "lighthouse",
            embedder=embedder,
            reranker=reranker,
            limit=5,
            distinct=DistinctSources(),
        )
        report = result.distinct
        assert (
            _names(result),
            report.underfilled,
            report.pool,
            report.walked == report.pool,
            len(reranker.calls),
            len(embedder.calls),
        ) == (["lead"], True, 20, True, 1, 1)
    finally:
        fixture.close()


def test_a_pool_short_of_distinct_sources_underfills(tmp_path: Path) -> None:
    """Failure mode: fewer distinct sources than the limit is hidden (no underfill
    report) or answered with repeats."""
    fixture = _shelf(tmp_path, {f"doc{i}": [80 - i, 60 - i] for i in range(6)})
    try:
        reranker = ScriptedReranker()
        result = _search(fixture, 10, reranker, distinct=DistinctSources())
        report = result.distinct
        assert (
            len(result.hits),
            report.underfilled,
            report.walked == report.pool,
            len(reranker.calls),
        ) == (6, True, True, 1)
    finally:
        fixture.close()


def test_a_stale_head_does_not_suppress_its_valid_sibling(tmp_path: Path) -> None:
    """Failure mode: grouping runs before Biblios's authority, so a stale top chunk
    claims its source and hides a valid lower-ranked chunk of the same entry."""
    fixture = _shelf(tmp_path, {"lead": [99, 95], "other": [90]})
    try:
        _tamper(fixture, 99)
        result = _search(fixture, 2, ScriptedReranker(), distinct=DistinctSources())
        assert [(hit.path, SCORE.search(hit.chunk).group(1)) for hit in result.hits] == [
            ("lead.txt", "95"),
            ("other.txt", "90"),
        ]
        assert (result.distinct.ineligible, [w.category for w in result.withheld]) == (
            1,
            ["body-mismatch"],
        )
    finally:
        fixture.close()


def test_a_consumer_key_rejects_a_version_and_its_sibling_fills(tmp_path: Path) -> None:
    """Failure mode: a version the consumer rejects still claims its source."""
    fixture = _shelf(tmp_path, {"old": [99], "new": [95], "other": [90]})
    try:

        def key(candidate):
            name = candidate.entry.path.removesuffix(".txt")
            return None if name == "old" else ("source-a" if name == "new" else name)

        result = _search(fixture, 2, ScriptedReranker(), distinct=DistinctSources(key))
        assert _names(result) == ["new", "other"]
    finally:
        fixture.close()


def test_a_stack_keeps_one_result_per_source_with_each_shelfs_authority(tmp_path: Path) -> None:
    """Failure mode: the default key merges shelves, or a consumer key spanning shelves
    keeps more than the best-ranked member or calls the reranker again."""
    one = _shelf(tmp_path / "one", {"lead": [99, 98], "doc": [90]})
    two = _shelf(tmp_path / "two", {"doc": [95], "tail": [80]})
    try:
        default = _stack_search([one, two], 4, ScriptedReranker(), distinct=DistinctSources())
        shelves = {one.binding.shelf_id: "one", two.binding.shelf_id: "two"}
        assert [(shelves[hit.shelf_id], hit.path) for hit in default.hits] == [
            ("one", "lead.txt"),
            ("two", "doc.txt"),
            ("one", "doc.txt"),
            ("two", "tail.txt"),
        ], "the default key keeps each shelf's entries separate"
        merging = ScriptedReranker()
        merged = _stack_search(
            [one, two],
            4,
            merging,
            distinct=DistinctSources(lambda candidate: candidate.entry.path),
        )
        assert [(shelves[hit.shelf_id], hit.path) for hit in merged.hits] == [
            ("one", "lead.txt"),
            ("two", "doc.txt"),
            ("two", "tail.txt"),
        ], "a consumer key spanning shelves keeps the best-ranked member only"
        report = merged.distinct
        assert (report.returned, report.underfilled, report.walked, len(merging.calls)) == (
            3,
            True,
            report.pool,
            1,
        )
    finally:
        one.close()
        two.close()


def test_a_stale_head_on_one_shelf_yields_to_its_sibling_on_another(tmp_path: Path) -> None:
    """Failure mode: a stale head on one member claims a cross-shelf source and hides
    the valid member on another shelf."""
    one = _shelf(tmp_path / "one", {"doc": [99]})
    two = _shelf(tmp_path / "two", {"doc": [95], "tail": [80]})
    try:
        _tamper(one, 99)
        result = _stack_search(
            [one, two],
            2,
            ScriptedReranker(),
            distinct=DistinctSources(lambda candidate: candidate.entry.path),
        )
        assert [(hit.shelf_id, hit.path) for hit in result.hits] == [
            (two.binding.shelf_id, "doc.txt"),
            (two.binding.shelf_id, "tail.txt"),
        ]
        assert (
            result.distinct.ineligible,
            [(w.shelf_id, w.category) for w in result.withheld],
        ) == (1, [(one.binding.shelf_id, "body-mismatch")])
    finally:
        one.close()
        two.close()


def test_a_stack_member_changed_during_the_walk_is_withheld_on_its_own_shelf(
    tmp_path: Path,
) -> None:
    """Failure mode: assembly through a stack skips the re-read, or keeps a withheld
    member's stage, so a member deleted mid-walk is served or listed."""
    one = _shelf(tmp_path / "one", {"a": [99]})
    two = _shelf(tmp_path / "two", {"b": [95], "c": [90]})
    try:
        entry_a = _entry_id(one, "a.txt")

        def delete_a() -> None:
            one.bound.curate(
                CurationRequest(identifier(), one.bound.view.revision, DeleteEntry(entry_a))
            )

        result = _stack_search(
            [one, two],
            2,
            ScriptedReranker(),
            distinct=DistinctSources(_key_firing_on("b.txt", delete_a)),
        )
        assert (
            [(hit.shelf_id, hit.path) for hit in result.hits],
            [(w.shelf_id, w.entry_id, w.category) for w in result.withheld],
            [(stage.shelf_id, stage.entry_id) for stage in result.stages],
            (result.distinct.selected, result.distinct.returned),
        ) == (
            [(two.binding.shelf_id, "b.txt")],
            [(one.binding.shelf_id, entry_a, "not-live")],
            [(two.binding.shelf_id, _entry_id(two, "b.txt"))],
            (2, 1),
        )
    finally:
        one.close()
        two.close()


def test_an_all_unbuilt_stack_reports_an_empty_distinct_search(tmp_path: Path) -> None:
    """Failure mode: a requested report is missing when no member is built."""
    one, two = consumer_fixture(tmp_path / "one"), consumer_fixture(tmp_path / "two")
    try:
        for fixture in (one, two):
            fixture.bound.curate(fixture.request(data=b"pending unbuilt source"))
        result = _stack_search([one, two], 3, ScriptedReranker(), distinct=DistinctSources())
        assert (result.hits, result.distinct.returned, result.distinct.underfilled) == (
            (),
            0,
            True,
        )
    finally:
        one.close()
        two.close()


def test_accelerated_distinct_results_equal_exact_distinct_results(tmp_path: Path) -> None:
    """Failure mode: serving from memory changes which sources are selected. Legs must
    come from memory, so the comparison is never the exact search with itself."""
    files = {"lead": [99, 98, 97], "doc": [90], "tail": [80, 79]}
    one, two = _shelf(tmp_path / "one", files), _shelf(tmp_path / "two", files)
    acceleration = VectorAcceleration(budget_bytes=256 * 2**20, on_unavailable="refuse")
    try:
        for fixture in (one, two):
            fixture.bound.enable_vector_feed()
        exact = _search(one, 3, ScriptedReranker(), distinct=DistinctSources())
        fast = _search(
            one, 3, ScriptedReranker(), distinct=DistinctSources(), acceleration=acceleration
        )
        stack_exact = _stack_search([one, two], 4, ScriptedReranker(), distinct=DistinctSources())
        stack_fast = _stack_search(
            [one, two],
            4,
            ScriptedReranker(),
            distinct=DistinctSources(),
            acceleration=acceleration,
        )
        assert (fast.hits, fast.distinct) == (exact.hits, exact.distinct)
        assert (stack_fast.hits, stack_fast.distinct) == (stack_exact.hits, stack_exact.distinct)
        routes = fast.vector_routes + stack_fast.vector_routes
        assert routes and all(
            (r.cached_legs >= 1, r.oracle_legs, r.fallback_legs) == (True, 0, 0) for r in routes
        ), "the accelerated distinct search was not served from memory"
    finally:
        one.close()
        two.close()


@pytest.mark.parametrize(("bump", "category"), ((2, "commit-during-read"), (1, "commit-in-flight")))
def test_a_commit_between_selection_and_assembly_is_withheld_not_refilled(
    tmp_path: Path, bump: int, category: str
) -> None:
    """Failure mode: assembly trusts the selection-time check, so a hit whose entry was
    re-indexed (or is being re-indexed) after selection is served, listed, or refilled."""
    fixture = _shelf(tmp_path, {"a": [99], "b": [95], "c": [90]})
    try:
        entry_a = _entry_id(fixture, "a.txt")

        def commit_a() -> None:
            connection = sqlite3.connect(fixture.binding.palace_root / "biblios.sqlite")
            with connection:
                connection.execute(
                    "UPDATE entries SET served_seq = served_seq + ? WHERE entry_id = ?",
                    (bump, entry_a),
                )
            connection.close()

        reranker = ScriptedReranker()
        result = _search(
            fixture, 2, reranker, distinct=DistinctSources(_key_firing_on("b.txt", commit_a))
        )
        report = result.distinct
        assert (
            _names(result),
            [(w.entry_id, w.category, w.count) for w in result.withheld],
            [stage.entry_id for stage in result.stages],
            (report.selected, report.returned, report.underfilled),
            len(reranker.calls),
        ) == (
            ["b"],
            [(entry_a, category, 1)],
            [_entry_id(fixture, "b.txt")],
            (2, 1, True),
            1,
        )
    finally:
        fixture.close()


@pytest.mark.parametrize("mutation", ("rename", "replace", "delete", "mirror"))
def test_a_selected_entry_changed_during_the_walk_is_judged_at_assembly(
    tmp_path: Path, mutation: str
) -> None:
    """Failure mode: a selected entry renamed, replaced, deleted or whose user-owned
    mirror was edited after selection is served from its old state."""
    fixture = _shelf(tmp_path, {"a": [99], "b": [95], "c": [90]})
    try:
        entry_a = _entry_id(fixture, "a.txt")

        def change_a() -> None:
            if mutation == "mirror":
                mirror = fixture.bound._shelf.mirror_path(entry_a)
                mirror.write_bytes(mirror.read_bytes() + b"\nedited by the user\n")
                return
            action = {
                "rename": RelocateEntry(entry_a, fixture.root_id, "renamed.txt"),
                "replace": ReplaceContent(entry_a, b"different body"),
                "delete": DeleteEntry(entry_a),
            }[mutation]
            fixture.bound.curate(CurationRequest(identifier(), fixture.bound.view.revision, action))

        result = _search(
            fixture,
            2,
            ScriptedReranker(),
            distinct=DistinctSources(_key_firing_on("b.txt", change_a)),
        )
        served = [hit.path for hit in result.hits]
        withheld = {w.entry_id: w.category for w in result.withheld}
        expected = {
            "rename": (["renamed.txt", "b.txt"], {}),
            "replace": (["b.txt"], {entry_a: "not-accepted"}),
            "delete": (["b.txt"], {entry_a: "not-live"}),
            "mirror": (["b.txt"], {entry_a: "mirror-changed"}),
        }[mutation]
        assert (served, withheld) == expected
    finally:
        fixture.close()


@pytest.mark.parametrize("bad", ("raises", "not-a-string"))
def test_a_failing_consumer_key_is_a_typed_callback_failure(tmp_path: Path, bad: str) -> None:
    """Failure mode: a broken consumer key is skipped silently or surfaces as an untyped
    search failure."""
    fixture = _shelf(tmp_path, {"lead": [99], "other": [90]})
    try:

        def key(candidate):
            if bad == "raises":
                raise RuntimeError("consumer index unavailable")
            return ("not", "a", "string")

        with pytest.raises(ConsumerError) as refused:
            _search(fixture, 2, ScriptedReranker(), distinct=DistinctSources(key))
        assert (refused.value.code, refused.value.stage) == ("callback-failed", "search")
    finally:
        fixture.close()


@pytest.mark.parametrize("key", ("consumer", "default"))
def test_the_walk_honours_the_calls_cancellation(tmp_path: Path, key: str, monkeypatch) -> None:
    """Failure mode: the walk runs to the end after the call was cancelled, with the
    consumer's key or the default one; only the bound seam's final check notices."""
    import biblios.search as search_module

    fixture = _shelf(tmp_path, {"lead": [99], "other": [90], "tail": [80]})
    seen: list[str] = []
    verified: list[str] = []
    original = search_module._ServingCheck._verify

    def counting(self, entry_id):
        verified.append(entry_id)
        return original(self, entry_id)

    monkeypatch.setattr(search_module._ServingCheck, "_verify", counting)
    try:

        def consumer(candidate):
            seen.append(candidate.entry.path)
            return candidate.entry.path

        reranker = ScriptedReranker()

        def cancelled() -> bool:
            # Cancelled from the walk's first entry verification on, after the rerank's
            # own fence has passed, so only the walk's fence can observe it in time.
            return bool(verified)

        with pytest.raises(ConsumerError) as refused:
            fixture.bound.search(
                "lighthouse",
                embedder=HashEmbedder(),
                reranker=reranker,
                limit=3,
                distinct=DistinctSources(consumer if key == "consumer" else None),
                control=CallControl(cancelled=cancelled),
            )
        # The fence stops the walk at its first candidate: one entry verified, the
        # consumer's key never called.
        assert (refused.value.code, len(seen), len(verified)) == ("cancelled", 0, 1)
    finally:
        fixture.close()


def test_an_unbuilt_shelf_reports_an_empty_distinct_search(tmp_path: Path) -> None:
    """Failure mode: a requested report is missing on an unbuilt shelf."""
    fixture = consumer_fixture(tmp_path)
    try:
        fixture.bound.curate(fixture.request(data=b"pending unbuilt source"))
        result = _search(fixture, 5, ScriptedReranker(), distinct=DistinctSources())
        assert (result.hits, result.distinct.returned, result.distinct.underfilled) == (
            (),
            0,
            True,
        )
    finally:
        fixture.close()


def test_an_entry_refused_before_the_walk_never_heads_its_source(tmp_path: Path) -> None:
    """Failure mode: an entry whose mirror no longer matches its accepted stage when the
    walk reaches it is selected, or refused without being reported."""
    fixture = _shelf(tmp_path, {"lead": [99, 98], "other": [90], "tail": [80]})
    try:
        mirror = fixture.bound._shelf.mirror_path(_entry_id(fixture, "lead.txt"))
        mirror.write_bytes(mirror.read_bytes() + b"\nedited by the user\n")
        result = _search(fixture, 2, ScriptedReranker(), distinct=DistinctSources())
        assert (
            _names(result),
            [(w.category, w.count) for w in result.withheld],
            result.distinct.ineligible,
        ) == (["other", "tail"], [("mirror-changed", 2)], 2)
    finally:
        fixture.close()


def test_an_assembly_withholding_names_its_reason_after_a_walk_withholding(
    tmp_path: Path,
) -> None:
    """Failure mode: an entry withheld once during the walk (a stale chunk) and again at
    assembly (deleted after its sibling was selected) reports only the walk's reason,
    hiding why a selected result is missing."""
    fixture = _shelf(tmp_path, {"lead": [99, 95], "other": [90], "tail": [80]})
    try:
        _tamper(fixture, 99)
        lead = _entry_id(fixture, "lead.txt")

        def delete_lead() -> None:
            fixture.bound.curate(
                CurationRequest(identifier(), fixture.bound.view.revision, DeleteEntry(lead))
            )

        result = _search(
            fixture,
            2,
            ScriptedReranker(),
            distinct=DistinctSources(_key_firing_on("other.txt", delete_lead)),
        )
        assert (
            _names(result),
            [(w.entry_id, w.category, w.count) for w in result.withheld],
            (result.distinct.selected, result.distinct.returned),
        ) == (["other"], [(lead, "not-live", 2)], (2, 1))
    finally:
        fixture.close()


@pytest.mark.parametrize("given", ("falsy-callable", "bare-function"))
def test_the_distinct_request_is_taken_as_given(tmp_path: Path, given: str) -> None:
    """Failure mode: a valid but falsy key is replaced by the default grouping, or a
    request of the wrong type fails as an anonymous search failure after provider work."""
    fixture = _shelf(tmp_path, {"old": [99], "new": [95], "other": [90]})
    try:

        class SourceKeys(dict):
            def __call__(self, candidate):
                name = candidate.entry.path.removesuffix(".txt")
                return "source-a" if name in ("old", "new") else name

        reranker = ScriptedReranker()
        if given == "falsy-callable":
            result = _search(fixture, 3, reranker, distinct=DistinctSources(SourceKeys()))
            assert _names(result) == ["old", "other"]
        else:
            with pytest.raises(ConsumerError) as refused:
                _search(fixture, 3, reranker, distinct=lambda candidate: "x")
            assert (refused.value.code, reranker.calls) == ("distinct-invalid", [])
    finally:
        fixture.close()
