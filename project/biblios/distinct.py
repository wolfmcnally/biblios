"""Opt-in distinct-source results: at most one result per caller-supplied source key.

Palace walks its bounded, already reranked pool after every snapshot has closed and
asks Biblios for each candidate's source key. A candidate is eligible only after
Biblios's own authority serves it and the caller's key accepts it, so a stale or
rejected head never fills its group and the next valid sibling can. Palace makes the
same calls with the same inputs as without it; a short pool returns fewer results.
Only the strict reranked route of the bound seam is supported.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING

from biblios.control import CallControl, ConsumerError, controlled

if TYPE_CHECKING:
    from palace.prepared import DistinctOutcome

    from biblios.entries import Entry
    from biblios.search import Hit, ServedStage, _ServingCheck


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    """A candidate whose entry Biblios has served, as the caller's key sees it: its
    shelf, its entry and the accepted stage it would be served from. Its chunk is
    checked only after the key returns, and only when it would head a new source."""

    shelf_id: str
    entry: Entry
    stage: ServedStage


@dataclass(frozen=True, slots=True)
class DistinctSources:
    """One result per source: the search's ``limit`` then counts sources, not chunks.

    ``key`` maps a candidate to its source key, or ``None`` to reject it (for example a
    version the caller does not accept). It is called once per walked candidate of a
    served entry, duplicates included, after Palace's snapshot has closed, and should be
    fast and free of side effects. The default is one source per entry,
    ``"<shelf_id>/<entry_id>"``.
    """

    key: Callable[[SourceCandidate], str | None] | None = None


@dataclass(frozen=True, slots=True)
class DistinctReport:
    """How a distinct-source search went.

    Palace walked ``walked`` of the ``pool`` ranked candidates and selected ``selected``
    sources; ``ineligible`` candidates were refused (by Biblios's authority or the
    caller's key) and ``duplicates`` repeated a selected source. ``returned`` is the
    number of results in the answer: lower than ``selected`` when a selected hit was
    withheld at assembly (see ``withheld``). ``underfilled`` is true when ``returned``
    is below the requested limit.
    """

    pool: int
    walked: int
    ineligible: int
    duplicates: int
    selected: int
    returned: int
    underfilled: bool


def _per_entry(candidate: SourceCandidate) -> str:
    return f"{candidate.shelf_id}/{candidate.entry.entry_id}"


def fenced(distinct: DistinctSources | None, control: CallControl) -> DistinctSources | None:
    """The key, the caller's or the default, behind the same fence as every supplied
    callback: the call's deadline and cancellation are checked around each call, and a
    failure is ``callback-failed``."""
    if distinct is None:
        return None
    if not isinstance(distinct, DistinctSources):
        raise ConsumerError("distinct-invalid", stage="search")
    key = _per_entry if distinct.key is None else distinct.key
    return DistinctSources(lambda candidate: controlled(lambda: key(candidate), control, "search"))


def report(outcome: DistinctOutcome | None, returned: int, limit: int) -> DistinctReport:
    if outcome is None:  # nothing was searched
        return DistinctReport(0, 0, 0, 0, 0, 0, True)
    return DistinctReport(
        outcome.pool,
        outcome.walked,
        outcome.ineligible,
        outcome.duplicates,
        outcome.returned,
        returned,
        returned < limit,
    )


class _Selection:
    """Palace's key callback: Biblios's per-entry authority (memoized per entry), the
    caller's key, a duplicate shortcut, then the per-hit authority.

    ``locate(hit)`` returns the hit's shelf, its raw Palace search hit and a stable
    identity; serving checks open lazily, after Palace's snapshot has closed.
    """

    def __init__(
        self,
        distinct: DistinctSources,
        locate: Callable[[object], tuple[object, object, Hashable]],
    ) -> None:
        self.distinct = distinct
        self.locate = locate
        self.checks: dict[str, _ServingCheck] = {}
        self.kept: set[str] = set()
        self.served: dict[Hashable, Hit] = {}

    def check(self, shelf) -> _ServingCheck:
        from biblios.search import _ServingCheck

        if shelf.shelf_id not in self.checks:
            self.checks[shelf.shelf_id] = _ServingCheck(shelf)
        return self.checks[shelf.shelf_id]

    def close(self) -> None:
        for check in self.checks.values():
            check.close()

    def key(self, hit, evidence) -> str | None:
        shelf, raw, ident = self.locate(hit)
        check = self.check(shelf)
        entry_id = Path(raw.path).stem
        if entry_id not in check._verified:
            check._verified[entry_id] = check._verify(entry_id)
        verified = check._verified[entry_id]
        if isinstance(verified, str):
            check._withhold(entry_id, verified)
            return None
        entry, _record, stage = verified
        from biblios.search import ServedStage

        # The bound seam always supplies a key (``fenced``): the caller's or ``_per_entry``.
        group = self.distinct.key(
            SourceCandidate(
                shelf.shelf_id,
                entry,
                ServedStage(
                    shelf.shelf_id,
                    entry_id,
                    stage.stage_sha256,
                    stage.entry.mirror_sha256,
                    str(stage.entry.content_sha256),
                    stage.canonical,
                ),
            )
        )
        if group is not None and not isinstance(group, str):
            raise ConsumerError("callback-failed", stage="search")
        if group is None:
            return None
        if group in self.kept:
            return group  # a duplicate of a selected source: no per-hit check is needed
        served = check.hit(raw, "applied", evidence)
        if served is None:
            return None  # a stale head never fills its group
        self.kept.add(group)
        self.served[ident] = served
        return group

    def assemble(self, hits, status) -> list[Hit]:
        """Each selected hit, re-judged at assembly; a withheld one is not refilled."""
        answered: list[Hit] = []
        for hit in hits:
            shelf, _raw, ident = self.locate(hit)
            served = self.checks[shelf.shelf_id].recheck(
                replace(self.served[ident], rerank_status=status)
            )
            if served is not None:
                answered.append(served)
        survivors = {(hit.shelf_id, hit.entry_id) for hit in answered}
        for shelf_id, check in self.checks.items():
            check._served = {
                entry_id: stage
                for entry_id, stage in check._served.items()
                if (shelf_id, entry_id) in survivors
            }
        return answered
