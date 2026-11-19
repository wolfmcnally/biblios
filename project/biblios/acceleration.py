"""Opt-in exact vector acceleration and memory observability for bound shelves.

A long-lived consumer may build one :class:`VectorAcceleration` per process and pass it
to :meth:`biblios.consumer.BoundShelf.search` or :meth:`biblios.consumer.BoundStack.search`.
For a shelf whose Palace change feed is enabled, Palace's exact in-memory vector cache
then serves the vector leg with the released answer. A search without one runs exactly
the released path, and this module imports none of Palace's cache or feed modules until
an acceleration object exists or a feed is read.

Figures come from three scopes, never mixed: :class:`StoredVectors` is one shelf's
persistent store, read from its feed state; :class:`AccelerationStats` is this process's
cache, from Palace's own allocation ledger (an estimate of the cache's memory, not of the
process); ``process_peak_rss_bytes`` is the operating system's lifetime peak for the
process. None of them carries text, chunk ids or queries.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import closing, contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from biblios.control import ConsumerError, ShelfView

if TYPE_CHECKING:
    from palace.vector_cache import VectorCache, VectorCacheOutcome

    from biblios.consumer import BoundShelf

OnUnavailable = Literal["refuse", "fallback"]
Feed = Literal["enabled", "disabled", "absent"]

#: Palace's operational unavailability reasons; only these honour ``on_unavailable``.
UNAVAILABLE_REASONS = ("feed-disabled", "budget", "queue-full", "snapshot")


@dataclass(frozen=True, slots=True)
class StoredVectors:
    """One shelf's stored vectors, read in constant work from its Palace feed state.

    ``feed`` is ``enabled``, ``disabled`` (the index exists without a feed) or ``absent``
    (no index database). Every figure is ``None`` unless the feed is enabled: without a
    feed the store keeps no constant-work count, and an unknown count is never zero.
    ``vector_rows`` counts stored vectors; ``live_chunks`` counts stored chunks;
    ``chunks_without_vector`` is their difference. ``diverged`` means some writer changed
    the chunks without the feed, so no cache may serve the shelf until a new lineage.
    """

    shelf_id: str
    feed: Feed
    epoch: str | None = None
    last_seq: int | None = None
    floor_seq: int | None = None
    feed_entries: int | None = None
    live_chunks: int | None = None
    vector_rows: int | None = None
    chunks_without_vector: int | None = None
    diverged: bool | None = None


@dataclass(frozen=True, slots=True)
class VectorRoute:
    """How one shelf's vector legs were answered in one accelerated search.

    Every route returns the exact released answer; they differ in what served it.
    ``cached_legs`` came from memory; ``oracle_legs`` from the released SQL because the
    cache could not certify parity (still exact, not a fallback); ``fallback_legs`` from
    the released SQL because the cache was unavailable under ``on_unavailable="fallback"``,
    with ``reasons`` naming why. ``bypassed`` means the shelf had no sqlite-vec vector leg
    (keyword-only search, a remote vector backend, or no index). ``snapshot_seq`` is the
    feed position the legs read.
    """

    shelf_id: str
    bypassed: bool
    cached_legs: int
    oracle_legs: int
    fallback_legs: int
    reasons: tuple[str, ...]
    snapshot_seq: int | None


@dataclass(frozen=True, slots=True)
class ResidentShelf:
    """One shelf resident in this process's cache: Palace's figures for its copy.

    ``applied_seq``, ``live_rows`` and ``dead_rows`` describe one committed feed position
    and are published together; ``live_rows`` equals the store's ``vector_rows`` only at
    that position. ``owned_bytes`` is the cache's accounting of this shelf's blocks.
    """

    shelf_id: str | None
    epoch: str
    base_seq: int
    applied_seq: int
    live_rows: int
    dead_rows: int
    loader_route: str
    load_seconds: float
    owned_bytes: int
    leases: int


@dataclass(frozen=True, slots=True)
class AccelerationStats:
    """This process's cache from one Palace ledger snapshot, plus the process's peak.

    ``owned_bytes`` is ``live_bytes + retired_bytes + pooled_bytes + load_bytes +
    scratch_bytes``; ``headroom_bytes`` is what remains under ``budget_bytes``. These are
    the cache's own accounting, an estimate; the process's footprint as the operating
    system sees it differs. ``counters`` are cumulative and keyed by event and reason
    (for example ``fallbacks.budget``, ``refusals.feed-disabled``, ``evictions.authorization``).
    ``process_peak_rss_bytes`` is the operating system's lifetime peak resident set for
    the whole process, or ``None`` when unavailable.
    """

    budget_bytes: int
    owned_bytes: int
    headroom_bytes: int
    live_bytes: int
    retired_bytes: int
    pooled_bytes: int
    load_bytes: int
    scratch_bytes: int
    leases: int
    counters: Mapping[str, int]
    shelves: tuple[ResidentShelf, ...]
    process_peak_rss_bytes: int | None


class _Unauthorized(Exception):
    """The cache was asked to serve a store this call did not admit, or the consumer's
    hook refused or failed. Palace evicts the store and re-raises this unchanged."""


_admitted: ContextVar[tuple[object, Mapping[Path, ShelfView]] | None] = ContextVar(
    "biblios_vector_admitted", default=None
)


def _key(store: Path) -> Path:
    # Palace keys every store by this same canonical directory.
    return Path(store).expanduser().resolve()


class VectorAcceleration:
    """One process-wide, budgeted, exact vector cache for bound shelves.

    ``on_unavailable`` has no default. ``"refuse"`` makes a search fail with
    ``vector-cache-<reason>`` when the cache cannot serve a shelf for an operational
    reason (``feed-disabled``, ``budget``, ``queue-full``, ``snapshot``); ``"fallback"``
    answers that shelf from the released SQL and reports it in the route. Integrity and
    access failures fail the search under either setting and are never a fallback.

    ``authorize`` receives the admitted :class:`~biblios.control.ShelfView` of each shelf
    on every accelerated search (hits included) and on :meth:`warm`, and must return true.
    A false result, an exception or a store the call did not admit fails the search with
    ``vector-cache-unauthorized`` and evicts the shelf. The object is shared by threads.
    """

    def __init__(
        self,
        *,
        budget_bytes: int,
        on_unavailable: OnUnavailable,
        authorize: Callable[[ShelfView], bool] | None = None,
        max_concurrent_loads: int = 1,
        max_waiting_loads: int = 8,
    ) -> None:
        from palace.vector_cache import VectorCache

        self._hook = authorize
        self._names: dict[Path, str] = {}
        self._lock = threading.Lock()
        self._cache: VectorCache = VectorCache(
            budget_bytes=budget_bytes,
            on_unavailable=on_unavailable,
            authorize=self._authorize,
            max_concurrent_loads=max_concurrent_loads,
            max_waiting_loads=max_waiting_loads,
        )

    @contextmanager
    def _admitting(self, views: Mapping[Path, ShelfView]) -> Iterator[VectorCache]:
        """Bind the stores of the shelves this call admitted, for this call's context."""
        keyed = {_key(store): view for store, view in views.items()}
        with self._lock:
            self._names.update((store, view.shelf_id) for store, view in keyed.items())
        token = _admitted.set((self, keyed))
        try:
            yield self._cache
        finally:
            _admitted.reset(token)

    def _authorize(self, store: Path) -> bool:
        current = _admitted.get()
        if current is None or current[0] is not self or store not in current[1]:
            raise _Unauthorized("store not admitted by this call")
        if self._hook is None:
            return True
        try:
            allowed = self._hook(current[1][store])
        except Exception as error:
            raise _Unauthorized("authorization hook failed") from error
        if not allowed:
            raise _Unauthorized("authorization refused")
        return True

    def warm(self, shelf: BoundShelf) -> bool:
        """Authorize and load one shelf ahead of its first accelerated search.

        Returns whether the shelf is now resident; an unbuilt shelf, or one without a
        sqlite-vec vector leg, is not loaded. Unavailability follows ``on_unavailable``."""
        return shelf._warm_vectors(self)

    def evict(self, shelf: BoundShelf) -> bool:
        """Unpublish one shelf's resident copy now; returns whether it was resident.

        When its last search ends, Palace zero-fills its blocks and keeps them pooled for
        reuse: ``live_bytes`` falls and ``pooled_bytes`` rises, while ``owned_bytes``,
        ``headroom_bytes`` and process memory fall only when Palace drops pooled blocks
        under later budget pressure. Eviction needs no admission and works on a closed
        binding, including the one an excision retired: an excised entry is never served
        (each search applies the feed first), but its vector stays in this process's
        memory until its block is compacted or the shelf is evicted, and is zeroed by the
        eviction. Closing a binding leaves its shelf resident."""
        return self._cache.evict(shelf._shelf.palace_store)

    def stats(self) -> AccelerationStats:
        from palace.vector_cache import process_peak_rss_bytes

        snapshot = self._cache.stats()
        with self._lock:
            names = dict(self._names)
        return AccelerationStats(
            budget_bytes=snapshot.budget_bytes,
            owned_bytes=snapshot.owned_bytes,
            headroom_bytes=snapshot.headroom_bytes,
            live_bytes=snapshot.live_bytes,
            retired_bytes=snapshot.retired_bytes,
            pooled_bytes=snapshot.pooled_bytes,
            load_bytes=snapshot.load_bytes,
            scratch_bytes=snapshot.scratch_bytes,
            leases=snapshot.leases,
            counters=dict(snapshot.counters),
            shelves=tuple(
                ResidentShelf(
                    shelf_id=names.get(_key(item.store)),
                    epoch=item.epoch,
                    base_seq=item.base_seq,
                    applied_seq=item.applied_seq,
                    live_rows=item.live_rows,
                    dead_rows=item.dead_rows,
                    loader_route=item.loader_route,
                    load_seconds=item.load_seconds,
                    owned_bytes=item.owned_bytes,
                    leases=item.leases,
                )
                for item in snapshot.stores
            ),
            process_peak_rss_bytes=process_peak_rss_bytes(),
        )


def stored_vectors(shelf_id: str, database: Path) -> StoredVectors:
    """Read one shelf's feed state in constant work, from one read-only snapshot."""
    if not database.is_file():
        return StoredVectors(shelf_id, "absent")
    from palace.index._errors import IndexError as PalaceIndexError
    from palace.index.vector_feed import read_vector_feed_state

    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.execute("BEGIN")
        try:
            state = read_vector_feed_state(conn)
        except PalaceIndexError as error:
            # Palace raises its plain IndexError only for a malformed state row.
            raise ConsumerError("vector-feed-malformed", stage="read") from error
        finally:
            conn.rollback()
    if state is None:
        return StoredVectors(shelf_id, "disabled")

    return StoredVectors(
        shelf_id,
        "enabled",
        epoch=state.epoch,
        last_seq=state.last_seq,
        floor_seq=state.floor_seq,
        feed_entries=state.feed_entries,
        live_chunks=state.live_chunks,
        vector_rows=state.vector_rows,
        chunks_without_vector=state.chunks_without_vector,
        diverged=state.diverged,
    )


def vector_error(error: BaseException) -> ConsumerError | None:
    """The consumer code for a typed cache or feed failure, or ``None`` for any other.

    Imports nothing: a Palace cache or feed error can only exist once its module is loaded.
    An unavailability reason outside Palace's closed set is not typed here; it then takes
    the fail-closed integrity route.
    """
    if isinstance(error, _Unauthorized):
        return ConsumerError("vector-cache-unauthorized", stage="search")
    cache = sys.modules.get("palace.vector_cache")
    if (
        cache is not None
        and isinstance(error, cache.VectorCacheUnavailable)
        and error.reason in UNAVAILABLE_REASONS
    ):
        return ConsumerError(f"vector-cache-{error.reason}", stage="search")
    feed = sys.modules.get("palace.index.vector_feed")
    if feed is not None and isinstance(error, feed.VectorFeedDiverged):
        return ConsumerError("vector-feed-diverged", stage="search")
    return None


#: Palace's cache and feed sources: an error raised in one of them was raised by the cache,
#: never by the exact released search.
_CACHE_SOURCES = ("palace/vector_cache.py", "palace/index/vector_feed.py")


def _chain(error: BaseException) -> list[BaseException]:
    """The error and its causes: Palace's admission wrappers chain a store read error
    raised inside a cached leg, so the original must be inspected too."""
    seen: list[BaseException] = []
    while error is not None and all(error is not item for item in seen):
        seen.append(error)
        error = error.__cause__ or error.__context__
    return seen


def _innermost_file(error: BaseException) -> str:
    frame = error.__traceback__
    while frame is not None and frame.tb_next is not None:
        frame = frame.tb_next
    return "" if frame is None else frame.tb_frame.f_code.co_filename.replace("\\", "/")


def _raised_by_cache(error: BaseException) -> bool:
    return any(_innermost_file(item).endswith(_CACHE_SOURCES) for item in _chain(error))


def _contention(error: BaseException) -> bool:
    """A store lock or busy timeout: an operational wait, not an integrity failure."""
    return any(
        isinstance(item, sqlite3.OperationalError)
        and ("locked" in str(item) or "busy" in str(item))
        for item in _chain(error)
    )


def accelerated_error(error: BaseException, databases: Sequence[Path]) -> ConsumerError | None:
    """The consumer code for a failed accelerated search, or ``None`` when the failure
    did not come from the cache or its feed (the caller's ordinary mapping applies).

    Authorization, unavailability and divergence are typed. Every other cache failure
    (malformed feed state, a vector of the wrong width, a load or refresh that does not
    reconcile with the stored count, a store read error inside the cache) reaches here as
    Palace's plain ``IndexError``, raised in the cache's own code or chaining an error that
    was; the searched shelves' feed state is then read once, so a divergence seen only by a
    concurrent loader is still reported as one. The integrity code means the cache could
    not serve a consistent copy; it is not by itself proof of corruption.
    """
    typed = vector_error(error)
    if typed is not None or _contention(error) or not _raised_by_cache(error):
        # A lock wait during a load stays the ordinary search failure, as it would be
        # for the exact search under the same contention.
        return typed
    for database in databases:
        try:
            if stored_vectors("", database).diverged:
                return ConsumerError("vector-feed-diverged", stage="search")
        except ConsumerError as malformed:
            if malformed.code == "vector-feed-malformed":
                return ConsumerError("vector-feed-malformed", stage="search")
        except Exception:
            # An unreadable store: nothing more specific is known, and the search still
            # fails closed with the integrity code below.
            pass
    return ConsumerError("vector-cache-integrity", stage="search")


def routes(
    outcomes: Sequence[VectorCacheOutcome] | None, shelf_of: Callable[[Path], str]
) -> tuple[VectorRoute, ...] | None:
    """Palace's per-store outcomes as shelf routes, in Palace's order."""
    if outcomes is None:
        return None
    return tuple(
        VectorRoute(
            shelf_id=shelf_of(_key(outcome.store)),
            bypassed=outcome.bypassed,
            cached_legs=outcome.cached_legs,
            oracle_legs=outcome.oracle_legs,
            fallback_legs=outcome.fallback_legs,
            reasons=tuple(outcome.reasons),
            snapshot_seq=outcome.snapshot_seq,
        )
        for outcome in outcomes
    )
