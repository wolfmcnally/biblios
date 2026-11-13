"""The one indexing core: bring a set of entries current, one authenticated stage each.

Every Shelf, CLI and bound-consumer entry point is a thin call into :func:`bring_current`.
It discovers only the directly affected entries, decides re-reads by stat identity,
prepares each entry (conversion only under a positive creation grant, otherwise the
current mirror bytes), and publishes it through its own stage: B0 prepared, B1 authorized,
B2 persisted with its rows, B3 promoted, B4 served by ``commit_plan``, B5 accepted. Only
accepted state is served. Nothing here writes Castle metadata or advances the namespace
revision; Castle is read only through the adapter below, using published calls.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from castle.namespace import canonical_bytes

from biblios.control import CallControl, ConsumerError, controlled
from biblios.derived import (
    ACCEPTED_MARKER,
    GRANULARITY_NS,
    DerivedRecord,
    DerivedStore,
    StatIdentity,
    VolatileStore,
    account,
    mark_accepted_ever,
    opened,
    transaction,
)
from biblios.errors import BibliosError
from biblios.mirrors import (
    EDIT_CONVERTER,
    POLICY_CLASSES,
    SEARCHABLE,
    MirrorInvalid,
    Provenance,
    digest,
    lane_for,
    read_mirror,
    read_mirror_stat,
    retire_mirror,
    validate_mirror,
    write_mirror,
)

if TYPE_CHECKING:
    from palace.index import FilePlan
    from palace.index.config import EmbeddingIdentity

    from biblios.consumer import BoundShelf
    from biblios.entries import Entry
    from biblios.pipeline import ConsumerPipeline, ConversionResult, ReuseDonor

EMBED_SLICE = 16
IMPORT_BATCH = 64
"""Legacy entries recorded per import transaction; cancellation is checked between."""
MAX_EVIDENCE = 16 * 1024

Outcome = Literal[
    "searchable",
    "unconverted",
    "pending",
    "busy",
    "unrecorded",
    "uncertain",
    "recovery-required",
    "removed",
]


# ---- request and result ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExpectedEntry:
    """What the caller last saw of one entry; a difference refuses that entry only."""

    predecessor_stage_sha256: str | None
    source_cas_id: str | None
    mirror_sha256: str | None
    """The accepted record's mirror digest the caller last saw, compared with the record,
    never with the bytes now at the path: a direct edit since then is still adopted."""


@dataclass(frozen=True, slots=True)
class UpdateSeam:
    """Replace one entry's mirror with caller bytes, over exactly the expected digest."""

    entry_id: str
    expected_mirror_sha256: str | None
    new_mirror: bytes


@dataclass(frozen=True, slots=True)
class ReadObservation:
    """A mismatch a reader saw while serving; returned to a write-grade call.

    ``served_seq`` is the entry's even serving sequence the reader verified against. A
    write-grade call passes it back in :attr:`BringCurrentRequest.observations`: an
    observation whose sequence is no longer the entry's is stale and dropped;
    ``mirror-changed`` is the ordinary mirror classification; ``rows-mismatch`` and
    ``missing-rows`` are checked under the writer lock against the entry's accepted
    stage (O(rows of that entry)): the rows Palace serves, and the accepted per-hit rows
    and membership nodes. An observation is a hint, never evidence: a report the check
    disproves is dropped however often it is replayed, and a second report at the same
    sequence is verified afresh like the first (it never isolates rows the check finds
    healthy). Only a proven mismatch changes anything:

    - Palace still serves exactly the accepted stage's rows (only the per-hit rows or
      nodes are damaged) and the call has a stage authority: the entry is re-committed
      in the same call through the ordinary path, from Palace's rows (no provider call)
      under a new stage the caller authorizes then. A mirror edit or rename since
      acceptance, or the caller refusing that stage, leaves it isolated.
    - Otherwise (Palace's rows changed, or no authority to re-verify): the entry is
      isolated ``served-rows-damaged`` until a request names it, which re-indexes it,
      reusing committed rows only where they still reproduce the accepted stage.

    Nothing else is repaired, re-signed or rewritten (never the user's mirror), and a
    reader keeps serving every other hit it proves individually."""

    entry_id: str
    category: Literal["mirror-changed", "rows-mismatch", "missing-rows"]
    served_seq: int


@dataclass(frozen=True, slots=True)
class VoidRecord:
    """The caller's evidence that it voided a stage it holds a record of."""

    entry_id: str
    stage_sha256: str
    reason: str
    evidence: bytes


@dataclass(frozen=True, slots=True)
class BringCurrentRequest:
    operation_id: str
    entry_ids: tuple[str, ...] | None = None
    """Entries to bring current; None discovers the directly affected set."""
    expected_revision: int | None = None
    retry_uncertain: bool = False
    update: UpdateSeam | None = None
    expected_entries: Mapping[str, ExpectedEntry] | None = None
    voided: tuple[VoidRecord, ...] = ()
    observations: tuple[ReadObservation, ...] = ()
    reembed: tuple[str, ...] = ()
    """Entries the caller explicitly selects for ordinary re-embedding; never a default."""
    sweep: bool = False
    """The one explicit whole-shelf form: stat every recorded and live entry (O(N))."""
    donors: Mapping[str, ReuseDonor] | None = None
    """Per destination entry, one caller-nominated donor whose converted body and
    identical ordered embedding inputs may be reused; never selected automatically."""
    resume_only: bool = False
    """Only finish the named entries' persisted stages (see :func:`resume_stages`)."""


@dataclass(frozen=True, slots=True)
class EntryOutcome:
    entry_id: str
    outcome: str
    """An :data:`Outcome`, or ``isolated:<class>``."""
    code: str | None = None
    stage_sha256: str | None = None
    embedded: int = 0
    reused: int = 0
    """Rows whose vectors came from a nominated donor rather than a provider call."""
    cause: BaseException | None = field(default=None, compare=False, repr=False)
    """The exception behind this outcome, for diagnosis by this call's caller only; it
    is never recorded, so a replayed result carries none."""


@dataclass(frozen=True, slots=True)
class AcceptedStage:
    entry_id: str
    stage_sha256: str


@dataclass(frozen=True, slots=True)
class BringCurrentResult:
    """What one call did. A ``replayed`` result is the exact record of the original call
    with the same operation id, returned without any effect: its outcomes, accepted
    stages, revision and ``provider_calls`` describe that original call, not the shelf
    now and not calls made now (none are). It is history, never current state and never
    authority: an entry it calls searchable may since have changed, been removed or been
    excised, and a caller decides nothing from it without its own current records."""

    operation_id: str
    revision: int
    entries: tuple[EntryOutcome, ...]
    accepted: tuple[AcceptedStage, ...]
    """Reported only after B5 is recorded."""
    yielded: bool = False
    replayed: bool = False
    """True when this is the recorded result of an earlier identical request."""
    provider_calls: int = 0
    """Provider calls the call this result records made (for a replay: the original's)."""
    swept: bool = False
    redacted: bool = False
    """True for a replayed result from which an excision removed the selected entries."""

    def outcome(self, entry_id: str) -> EntryOutcome | None:
        return next((item for item in self.entries if item.entry_id == entry_id), None)


# ---- the stage seam -------------------------------------------------------------------


class StageRefused(Exception):  # noqa: N818 - a caller's answer, not a fault
    """Raised by a stage authority to refuse authorization or promotion."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DerivationIdentity:
    converter: str
    converter_version: str
    policy_sha256: str
    renderer_label: str
    input_view_sha256: str
    body_sha256: str | None


@dataclass(frozen=True, slots=True)
class EmbedderIdentity:
    palace_store_identity: str
    model: str
    dimension: int


@dataclass(frozen=True, slots=True)
class DonorIdentity:
    entry_id: str
    stage_sha256: str
    mirror_sha256: str
    body_sha256: str
    content_sha256: str
    checkpoint_sha256: str | None = None
    """When the donor's converted body is reused under a stage authority: the checkpoint
    the donor's accepted stage signs, whose stored document recomputed to it and whose
    seal the caller verified. None when only the donor's vectors are reused, without an
    authority, and in a ``biblios.stage/1`` stage."""


@dataclass(frozen=True, slots=True)
class CreationIdentity:
    """The grant that authorized a first mirror; the caller checks it against its own
    curation records."""

    operation_id: str | None
    role: str
    kind: str | None
    cas_id: str
    request_fingerprint: str | None
    revision_after: int | None


@dataclass(frozen=True, slots=True)
class StageEntry:
    entry_id: str
    path: str
    source_cas_id: str | None
    action: Literal["publish", "remove", "unconverted"]
    prior_mirror_sha256: str | None
    mirror_sha256: str | None
    mirror_provenance: Literal["converted", "direct-edit", "update-seam", "copy", "import"]
    content_sha256: str | None
    chunk_manifest_sha256: str | None
    row_count: int
    projection: Literal["exact", "replaced", "appended"] | None = None
    """How the indexed bytes derive from the mirror bytes (publish only): ``exact`` (the
    mirror itself), ``replaced`` (the renderer's own recorded name section swapped, in
    memory, for the current filename's) or ``appended`` (every mirror byte kept, the
    current filename's section added in memory). The mirror on disk is never changed."""
    indexed_sha256: str | None = None
    """The digest of exactly the bytes Palace indexed (publish only)."""
    chunk_root_sha256: str | None = None
    """The membership root over the ordered rows (publish with rows only; see
    :mod:`biblios.membership`). Absent from a ``biblios.stage/1`` stage, whose hits carry
    no membership proof and are withheld ``proof-missing``."""
    checkpoint: StageCheckpoint | None = None
    """Under a stage authority, the conversion checkpoint whose content this stage was
    derived from (converted mirrors, unconverted outcomes and name projections that use
    the recorded rendering), so the caller can require its own seal. None without an
    authority, when no checkpoint content was used, and in a ``biblios.stage/1`` stage."""


@dataclass(frozen=True, slots=True)
class Stage:
    stage_id: str
    stage_sha256: str
    canonical: bytes
    namespace_id: str
    epoch: str
    store_id: str
    lineage_id: str
    database_generation: str
    """With ``namespace_id`` and ``epoch``: the shelf generation the stage was persisted
    under (Castle's binding), signed, so a later disposition names it exactly."""
    observed_revision: int
    view_sha256: str
    operation_id: str
    predecessor_sha256: str | None
    entry: StageEntry
    derivation: DerivationIdentity
    embedder: EmbedderIdentity
    donor: DonorIdentity | None
    reused_from_sha256: str | None
    """The accepted stage whose committed rows this stage reuses, or None. For an edit
    it is the predecessor; for a replaced source (no predecessor: a new ingestion) it is
    the old source's accepted stage. The caller checks it against its own accepted
    record before signing, so rows substituted in the stores are never laundered."""
    creation: CreationIdentity | None
    voids: tuple[VoidRecord, ...]
    resume_sha256: str
    """The digest of the stage's resume document (conversion, grant, the exchange B4
    must displace, the indexed projection), so a restart or a cancellation never acts on
    an unsigned decision; a mismatch is ``stage-tampered``."""
    authenticated: bool

    def generation(self) -> GenerationIdentity:
        return GenerationIdentity(
            self.namespace_id, self.store_id, self.lineage_id, self.database_generation, self.epoch
        )


@dataclass(frozen=True, slots=True)
class GenerationIdentity:
    """One shelf generation as Castle's binding reports it."""

    namespace_id: str
    store_id: str
    lineage_id: str
    database_generation: str
    epoch: str


@dataclass(frozen=True, slots=True)
class StageVerification:
    stage_sha256: str
    staged_content_sha256: str | None
    row_count: int


CHECKPOINT_SCHEMA = "biblios.checkpoint/1"


@dataclass(frozen=True, slots=True)
class CheckpointIdentity:
    """One conversion checkpoint exactly as it is persisted for reuse.

    ``canonical`` is the exact ``biblios.checkpoint/1`` byte string the caller seals and
    verifies (never its own serialization) and ``sha256`` its digest. It covers every
    field here plus the recorded ``ConversionResult`` and, at ``complete``, the rendered
    ``MirrorDocument`` and the name it was rendered for, and the donor checkpoint a
    reused body came from. At ``complete`` it also names ``conversion_checkpoint_sha256``:
    the ``converted`` checkpoint (sealed in this call, or verified) whose conversion it
    renders, unchanged; a caller that keeps the conversions it sealed or verified can
    refuse a ``complete`` request whose conversion differs from the one so named. Biblios
    rebuilds the identity from the stored row on every reread; a stored digest is never
    trusted. Without an authority the link is empty."""

    entry_id: str
    source_cas_id: str
    media_type: str
    policy_sha256: str
    """The digest of the derivation policy the conversion ran under."""
    generation: GenerationIdentity
    """The shelf generation the checkpoint was sealed under."""
    state: Literal["converted", "complete"]
    """``converted``: the converter's result only; ``complete``: also its rendering."""
    canonical: bytes
    sha256: str


@dataclass(frozen=True, slots=True)
class StageCheckpoint:
    sha256: str
    """The :class:`CheckpointIdentity` digest the stage's content was derived from."""
    reused: bool
    """False when this call produced the checkpoint content and the caller sealed it in
    this call; True when it was read back from storage and the caller verified it."""


@dataclass(frozen=True, slots=True)
class StageCancellation:
    """A caller's request to cancel one serving stage of one entry.

    ``canonical`` is the exact byte string the caller signs (never its own serialization)
    and ``sha256`` its digest; a restart and a replay are verified against the bytes
    persisted at the first call, never against a fresh serialization."""

    operation_id: str
    entry_id: str
    stage_sha256: str
    predecessor_sha256: str | None
    """The entry's accepted stage the cancelled stage would have replaced (None: none)."""
    stage_generation: GenerationIdentity
    """Exactly the generation the OLD stage records."""
    current_generation: GenerationIdentity
    """Exactly the shelf's generation now; it shares the stage's namespace, store and
    lineage, and may differ only in epoch and database generation."""
    reason: str
    supersedes_sha256: str | None = None
    """The digest of the pending request this one replaces, when that one names a
    generation the shelf no longer has; None for the first request for a stage."""

    @property
    def canonical(self) -> bytes:
        return canonical_bytes({"schema": "biblios.stage-cancellation/2", **asdict(self)})

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.canonical).hexdigest()


@dataclass(frozen=True, slots=True)
class CancellationObservation:
    """What Biblios's own stores show for the entry when a cancellation is rechecked:
    information for the caller's decision, never evidence of completion."""

    stage_present: bool
    stage_state: str | None
    palace_file_sha256: str | None
    """Palace's committed file hash at the entry's path, or None when there is none."""
    served_seq_even: bool
    mirror_sha256: str | None
    """The digest of the bytes at the entry's mirror path, or None when there are none."""


@dataclass(frozen=True, slots=True)
class CancellationResult:
    state: Literal["cancelled", "completed-replay"]
    """``cancelled`` by this call (or this call finished an interrupted one), or the
    ``completed-replay`` of an earlier identical request, which has no effect."""
    operation_id: str
    stage_sha256: str
    retained: tuple[str, ...]
    """Mirror files the cancellation moved to retained names (never deleted)."""


class StageAuthority(Protocol):
    """A caller's own evidence for each stage, supplied on every call that takes one.

    Biblios has two publication grades and never chooses between them. Called with an
    authority, every effect waits for it: ``authorize_creation`` precedes any download,
    provider call or write at a user's path; ``authorize`` precedes persisting a stage;
    ``promote`` precedes serving it; ``recheck`` precedes every effect on a persisted
    stage that is read again. Called without one, the shelf publishes on Biblios's own
    self-checked records, which are no caller's evidence.

    Requiring authentication is the caller's discipline: a caller that authenticates
    passes its authority on every authority-taking call and refuses omission itself.
    Biblios never infers that requirement from store state, and it cannot prevent other
    code that holds the shelf from calling it without an authority or from writing its
    stores directly. Such a caller therefore accepts an entry's state only when its own
    signed records bind it: every outcome and :func:`stage_status` row carries the stage
    digest, and a digest its signed head does not record (for instance one published
    without the authority) is to be refused, not trusted because Biblios reports it.
    """

    def authorize_creation(self, creation: CreationIdentity, entry_id: str) -> bytes:
        """Admit a first conversion before any download, provider call or mirror write,
        from the caller's own write-grade curation record; raise StageRefused to refuse.
        Volatile grants and namespace presence are never this evidence. (Shape provisional
        pending the consumer reconciliation.)"""
        ...

    def authorize(self, stage: Stage) -> bytes: ...

    def promote(self, stage: Stage, verified: StageVerification) -> bytes: ...

    def recheck(
        self,
        stage: Stage,
        verified: StageVerification,
        authorization: bytes,
        promotion: bytes | None,
    ) -> Literal["pending", "accepted"]:
        """Verify, on every reread of a persisted stage and before any effect (including
        the cleanup of an accepted one), that the exact persisted authorization (and
        promotion, when present) is the caller's own evidence for exactly this stage;
        raise StageRefused otherwise. It never signs anything.

        The caller binds the evidence to the stage's entry, predecessor, namespace,
        database generation and epoch and to its own current signed head. It returns
        ``accepted`` only when that signed head already records exactly this stage, and
        ``pending`` otherwise. Biblios's own records never stand in for that answer: a
        derived record naming the stage without the caller's ``accepted`` is disputed and
        nothing is cleaned up. Biblios's volatile flags and self-checked digests are never
        evidence. A caller that authenticates passes its authority on every call; Biblios
        never infers that requirement from store state. (Shape provisional pending the
        consumer reconciliation.)"""
        ...

    def authorize_cancellation(self, stage: Stage, request: StageCancellation) -> bytes:
        """Sign the caller's own durable write-grade decision to cancel exactly this serving
        stage, over exactly ``request.canonical`` (whose digest is ``request.sha256``),
        before any cancellation effect; raise StageRefused to refuse. The caller binds the
        decision to the stage, its predecessor, epoch and operation and to its current
        signed head. Called once per cancellation: a restart never asks again."""
        ...

    def seal_checkpoint(self, checkpoint: CheckpointIdentity) -> bytes:
        """Seal a conversion checkpoint before Biblios persists it for reuse, over exactly
        ``checkpoint.canonical``; raise StageRefused to refuse. Called on the invoking
        thread after the converter (``converted``) and after the renderer (``complete``),
        and only for content this call produced: a reread is never sealed again.

        Refused, nothing reusable is persisted and the entry is pending
        ``checkpoint-seal-refused`` until its source or the policy changes (a new
        checkpoint key); Biblios never re-runs the converter by itself. Raising anything
        else leaves the dispatch uncertain (``retry_uncertain`` alone runs it again). A
        seal returned before a crash that persisted nothing matches no stored checkpoint."""
        ...

    def verify_checkpoint(self, checkpoint: CheckpointIdentity, seal: bytes) -> bool:
        """Whether ``seal`` is the caller's own seal over exactly ``checkpoint.canonical``.
        It never seals. Called on the invoking thread before any persisted checkpoint
        content is reused (the converter's result, its rendering, a donor's body or the
        recorded name section a projection replaces). ``False`` (or StageRefused) leaves
        the entry pending ``checkpoint-not-ours`` (a projection falls back to
        ``appended``); raising anything else leaves it uncertain. A checkpoint persisted
        without a seal (by an earlier release, or by a call without an authority) is
        pending ``checkpoint-unsealed``; neither outcome converts or embeds anything.

        Limitation: a checkpoint is never sealed after the fact, so an entry accepted
        without an authority (or in the ``biblios.stage/1`` form, which names no
        checkpoint) stays pending under an authority until a request re-embeds it
        (``reembed``), which embeds its current mirror bytes with the name section appended."""
        ...

    def recheck_cancellation(
        self,
        stage: Stage,
        request: StageCancellation,
        evidence: bytes,
        observed: CancellationObservation,
    ) -> Literal["pending", "completed"]:
        """Verify, before any effect of a resumed or replayed cancellation, that the
        persisted ``evidence`` is the caller's own signature over exactly the persisted
        ``request`` bytes for exactly this stage; raise StageRefused otherwise. It never
        signs. Answer ``completed`` only when the caller's own authenticated records
        already recognise this exact cancellation as completed (including after a
        successor head), and ``pending`` otherwise; a Biblios receipt or ``observed``
        state is never that recognition."""
        ...


def _stage_document(stage_fields: dict) -> bytes:
    def plain(value):
        if isinstance(value, bytes):
            return value.hex()
        if isinstance(value, tuple | list):
            return [plain(item) for item in value]
        if isinstance(value, dict):
            return {key: plain(item) for key, item in value.items()}
        return value

    return canonical_bytes(plain(stage_fields))


STAGE_SCHEMA = "biblios.stage/2"
"""Stages this release builds. ``biblios.stage/1`` (no membership root) still parses
exactly as it was signed; it is never rewritten."""


def build_stage(*, schema: str = STAGE_SCHEMA, **fields) -> Stage:
    """A stage whose canonical bytes cover every field; ``stage_sha256`` hashes them."""
    document = {
        key: (asdict(value) if hasattr(value, "__dataclass_fields__") else value)
        for key, value in fields.items()
    }
    document["voids"] = [asdict(void) for void in fields["voids"]]
    if schema == "biblios.stage/1":
        unsigned = [document["entry"].pop("chunk_root_sha256"), document["entry"].pop("checkpoint")]
        if document["donor"] is not None:
            unsigned.append(document["donor"].pop("checkpoint_sha256"))
        if any(value is not None for value in unsigned):
            raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
    elif schema != STAGE_SCHEMA:
        raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
    canonical = _stage_document({"schema": schema, **document})
    return Stage(stage_sha256=hashlib.sha256(canonical).hexdigest(), canonical=canonical, **fields)


def _check_evidence(evidence: object) -> None:
    """A caller's authorization or promotion is opaque bytes within the evidence bound."""
    if type(evidence) is not bytes or len(evidence) > MAX_EVIDENCE:
        raise ConsumerError("evidence-invalid", stage="index")


def _snapshot_rows(snapshot) -> list[tuple[str, str, str, bytes]]:
    """A Palace file snapshot's rows in the form stage digests are computed over."""
    return [
        (item.chunk.chunk_id, item.chunk.body, item.input_sha256, _float32(item.vector))
        for item in snapshot.chunks
    ]


def _float32(vector: Sequence[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def content_digest(rows: Sequence[tuple[str, str, str, bytes]]) -> tuple[str, str]:
    """``(content_sha256, chunk_manifest_sha256)`` of ordered (chunk id, body, input
    digest, float32 vector) rows; Palace timestamps are never part of either.

    Each row is encoded canonically on its own and framed by its length, so a file of any
    admitted size digests without one whole-file encoding (whose size is bounded)."""
    content, manifest = hashlib.sha256(), hashlib.sha256()
    for chunk_id, body, inputs, vector in rows:
        for hasher, row in (
            (content, [chunk_id, body, inputs, vector.hex()]),
            (manifest, [chunk_id, _text_digest(body), hashlib.sha256(vector).hexdigest()]),
        ):
            encoded = canonical_bytes(row)
            hasher.update(len(encoded).to_bytes(8, "big"))
            hasher.update(encoded)
    return content.hexdigest(), manifest.hexdigest()


def chunk_leaves(rows: Sequence[tuple[str, str, str, bytes]]) -> list[bytes]:
    """The membership leaves of ordered (chunk id, body, input digest, float32 vector)
    rows: each row at its position, by its body, input and vector digests."""
    from biblios.membership import leaf_hash

    return [
        leaf_hash(
            position, chunk_id, _text_digest(body), inputs, hashlib.sha256(vector).hexdigest()
        )
        for position, (chunk_id, body, inputs, vector) in enumerate(rows)
    ]


def chunk_root(rows: Sequence[tuple[str, str, str, bytes]]) -> str | None:
    """The membership root of ordered rows; ``None`` for no rows."""
    from biblios.membership import root_of, tree_nodes

    if not rows:
        return None
    return root_of(tree_nodes(chunk_leaves(rows)), len(rows)).hex()


def _text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---- the Castle adapter ---------------------------------------------------------------


class _Castle:
    """The only Castle access on ordinary core paths, through Castle's published reads.

    Castle validates exactly the records a read returns, with their ancestry, so every
    entry used here was validated against current bytes when it was read; damage in an
    untouched record is found when it is touched or by Castle's explicit diagnosis. Each
    read is one admission of constant cost: the revision probe, one ``get-entry`` per
    named entry (plus its not yet read ancestors) and change discovery in pages. ``calls``
    counts reads by kind and ``rows`` the records they returned, so the per-entry cost is
    measured, never hidden.
    """

    def __init__(self, bound: BoundShelf) -> None:
        self._shelf = bound._shelf
        self.calls: dict[str, int] = {}
        self.rows = 0
        self.generation: str | None = None
        self._ancestors: dict[str, object] = {}
        self._ancestors_at: int | None = None

    def _count(self, name: str) -> None:
        self.calls[name] = self.calls.get(name, 0) + 1

    def _read(self, kind: str, args: dict, at_revision: int | None = None):
        self._count(kind)
        return self._shelf._read_result(kind, args, at_revision)

    def observe_revision(self) -> int:
        """The committed revision from Castle's constant-cost probe; also records the
        database generation that change discovery is keyed to."""
        probe = self._read("probe", {}).value
        self.generation = probe.database_generation
        return probe.revision

    def _changes(self, from_revision: int):
        if self.generation is None:
            self.observe_revision()
        for change in self._shelf._changes(from_revision, self.generation, read=self._read):
            self.rows += 1
            yield change

    def snapshot(self) -> dict:
        """Every live entry record, each validated as it is read: the whole-sequence
        read, used only by the explicit sweep, the one-time preservation import and a
        resynchronization."""
        return {change.entry.entry_id: change.entry for change in self._changes(0) if change.live}

    def read_entries(self, entry_ids: Sequence[str]) -> dict[str, Entry]:
        """The live file entries among ``entry_ids``; absent ids are not live.

        Every read after the first is pinned to the first one's revision, so the answer
        is one committed state; a commit between them refuses the read and the set is
        read again (bounded), never mixed."""
        from castle.namespace import NamespaceError

        for attempt in range(3):
            try:
                return self._read_entries_at(entry_ids)
            except NamespaceError as error:
                if error.code != "namespace-revision-conflict" or attempt == 2:
                    raise
        raise AssertionError("unreachable")

    def _read_entries_at(self, entry_ids: Sequence[str]) -> dict[str, Entry]:
        from castle.namespace import NamespaceError

        from biblios.entries import view
        from biblios.shelf import _ENTRY_ID

        root = self._shelf._root_id
        revision: int | None = None
        found: dict[str, Entry] = {}

        def ancestor(entry_id: str):
            # Each directory is read once per revision; the root ends the chain.
            if self._ancestors_at != revision:
                self._ancestors, self._ancestors_at = {}, revision
            if entry_id not in self._ancestors:
                self._ancestors[entry_id] = self._read(
                    "get-entry", {"entry_id": entry_id}, revision
                ).value
                self.rows += 1
            return self._ancestors[entry_id]

        for entry_id in dict.fromkeys(entry_ids):
            if not isinstance(entry_id, str) or _ENTRY_ID.fullmatch(entry_id) is None:
                continue
            try:
                result = self._read("get-entry", {"entry_id": entry_id}, revision)
            except NamespaceError as error:
                if error.code == "namespace-entry-missing":
                    continue
                raise
            revision = result.revision
            record = result.value
            self.rows += 1
            if record.kind == "file":
                found[entry_id] = view(record, self._shelf._chain(record, ancestor), root)
        return found

    def changed_since(self, revision: int) -> set[str]:
        """File entries whose records changed after ``revision`` in the current database
        generation, including removals, from Castle's change discovery."""
        return {
            change.entry.entry_id
            for change in self._changes(revision + 1)
            if change.entry.kind == "file"
        }

    def operation(self, operation_id: str):
        from castle.namespace import namespace_operation

        self._count("operation")
        return namespace_operation(self._shelf._handle, operation_id)


# ---- marking --------------------------------------------------------------------------


def mark_dirty(bound: BoundShelf, entry_ids: Sequence[str], *, source: str) -> int:
    """Record that these entries may have changed; the one marking interface.

    Writes volatile rows only, never takes the writer lock or the namespace mutex, and
    returns the highest mark sequence written.
    """
    bound._admit(write=True)
    if not entry_ids:
        return 0
    if not isinstance(source, str) or not source or len(source) > 64:
        raise ConsumerError("request-invalid", stage="index")
    volatile = VolatileStore(bound.binding.palace_root)
    with closing(volatile.conn):
        return volatile.mark_dirty(list(entry_ids), source=source)


def grant_first_ingestion(
    bound: BoundShelf,
    entry_id: str,
    *,
    expected_cas_id: str,
    operation_id: str,
    expected_mirror_sha256: str | None = None,
) -> bool:
    """Grant one named entry its first mirror: the explicit, write-grade first-ingestion
    route (root ruling). Never invoked automatically and adds no approval gate.

    It grants only an entry that was never converted: live, with no accepted or
    converted record (an imported legacy entry that was never converted qualifies), and
    no file at its mirror path; a converted entry whose mirror is missing is refused
    ``converted-before`` (its repair is the update seam), a file at the path is
    ``mirror-user-owned``, and it refuses ``import-pending`` until the preservation
    import has run, and ``cas-changed`` when the entry's content is no longer
    ``expected_cas_id``. The grant is role ``explicit``, bound to that content address,
    this operation and the observed revision; a later
    :func:`bring_current` naming the entry materializes the mirror exactly as for any
    creation grant, and a caller's ``authorize_creation`` sees that provenance. Returns
    False when an identical grant already exists (idempotent).

    With ``expected_mirror_sha256`` the grant instead adopts the file already at the
    mirror path, for rebuilding a lost derived store: it is granted only when that
    file's bytes hash to exactly the caller's vouched digest (any other bytes refuse
    ``mirror-user-owned``, an absent file ``mirror-missing``), and the next
    :func:`bring_current` naming the entry indexes those bytes as they are, converting
    nothing and writing nothing at the path."""
    if not isinstance(operation_id, str) or not operation_id:
        raise ConsumerError("operation-invalid", stage="index")
    with bound._writer("biblios grant-first-ingestion"):
        with opened(bound.binding.palace_root, read_only=False) as (derived, volatile):
            assert volatile is not None
            if not derived.counter("legacy-import"):
                raise ConsumerError("import-pending", stage="index")
            shelf = bound._shelf
            castle = _Castle(bound)
            with shelf._handle.mutex:
                revision = castle.observe_revision()
                entry = castle.read_entries([entry_id]).get(entry_id)
            if entry is None:
                raise ConsumerError("entry-not-live", stage="index")
            if entry.cas_id != expected_cas_id:
                raise ConsumerError("cas-changed", stage="index")
            record = derived.record(entry_id)
            if record is not None and (record.converted or record.state == "accepted"):
                raise ConsumerError("converted-before", stage="index")
            path = shelf.mirror_path(entry_id)
            kind = "first-ingestion"
            if expected_mirror_sha256 is not None:
                # The same contained, no-follow read the indexing pass uses.
                try:
                    existing = read_mirror(path, shelf.mirror_root)
                except MirrorInvalid:
                    raise ConsumerError("mirror-user-owned", stage="index") from None
                except OSError as error:
                    raise ConsumerError("mirror-unreadable", stage="index") from error
                if existing is None:
                    raise ConsumerError("mirror-missing", stage="index")
                if digest(existing) != expected_mirror_sha256:
                    raise ConsumerError("mirror-user-owned", stage="index")
                kind = "adopt-existing"
            elif path.exists() or path.is_symlink():
                raise ConsumerError("mirror-user-owned", stage="index")
            identity = ["explicit", entry_id, entry.cas_id, operation_id]
            if expected_mirror_sha256 is not None:
                identity.append(expected_mirror_sha256)
            fingerprint = hashlib.sha256(canonical_bytes(identity)).hexdigest()
            row = volatile.conn.execute(
                "SELECT role, request_fingerprint, state, kind, intended_mirror_sha256 "
                "FROM grants WHERE entry_id=? AND cas_id=?",
                (entry_id, entry.cas_id),
            ).fetchone()
            if row is not None and row[2] == "granted":
                same = (row[3], row[4]) == (kind, expected_mirror_sha256)
                if row[0] != "explicit" or same or expected_mirror_sha256 is None:
                    return False  # this grant, or a creation grant for the same content
                # An explicit grant re-vouched for the bytes now at the path (the user
                # edited the mirror after the first vouch): the new digest replaces the
                # old one, so a stale vouch can never leave the entry unindexable.
            elif row is not None:
                raise ConsumerError("grant-consumed", stage="index")
            with transaction(volatile.conn):
                volatile.conn.execute(
                    "INSERT INTO grants VALUES (?, ?, 'explicit', ?, ?, "
                    "?, ?, 'granted', ?) ON CONFLICT (entry_id, cas_id) DO UPDATE SET "
                    "role=excluded.role, operation_id=excluded.operation_id, "
                    "kind=excluded.kind, request_fingerprint=excluded.request_fingerprint, "
                    "revision_after=excluded.revision_after, state='granted', "
                    "intended_mirror_sha256=excluded.intended_mirror_sha256",
                    (
                        entry_id,
                        entry.cas_id,
                        operation_id,
                        kind,
                        fingerprint,
                        revision,
                        expected_mirror_sha256,
                    ),
                )
            return True


@dataclass(frozen=True, slots=True)
class StageStatus:
    """What Biblios's own stores show for one stage digest: information for the caller,
    never authority. A cancellation, discard or acceptance re-verifies its own evidence;
    none of them takes a status as evidence, and ``cancelled`` is a historical receipt."""

    entry_id: str | None
    """None only for ``absent``."""
    stage_sha256: str
    state: Literal[
        "authorized",
        "promoted",
        "serving",
        "cancelling",
        "accepted",
        "recorded",
        "cancelled",
        "absent",
    ]
    """A persisted stage's own state: ``authorized`` (B2), ``promoted`` (B3), ``serving``
    (B4 begun), ``cancelling`` (a caller-signed cancellation began) or ``accepted`` (B5
    recorded, kept until the caller's signed head records it). Otherwise ``recorded``
    (Biblios's accepted record names it; the caller's head decides whether it is
    accepted), ``cancelled`` (a cancellation of it completed) or ``absent`` (no store
    names it)."""
    epoch: str | None
    serving: bool
    """B4 began and B5 has not finished: such a stage is never discarded; it is finished,
    or cancelled with :func:`cancel_stage`."""


@dataclass(frozen=True, slots=True)
class StagePage:
    stages: tuple[StageStatus, ...]
    """Persisted stages in entry order."""
    next_after: str | None
    """The ``after`` that continues the traversal, or None when it is complete."""


@dataclass(frozen=True, slots=True)
class EntryStatus:
    """One entry as Biblios's stores show it; information, never authority."""

    entry_id: str
    state: str | None
    """The derived record's state (``accepted``, ``unconverted``, ...), or None when the
    entry has no record."""
    accepted_stage_sha256: str | None
    mirror_sha256: str | None
    """The recorded mirror digest (not a fresh hash of the bytes at the path)."""
    pending: StageStatus | None
    """The entry's persisted, not yet settled stage, if any."""
    dirty: bool
    """The entry is marked for the next call."""
    refusal: str | None
    """The code of the caller's recorded refusal of the entry's current inputs, if any."""


MAX_PAGE = 1000


def _stage_row(derived, volatile, row) -> StageStatus:
    entry_id, sha, canonical, state = row
    try:
        epoch = str(json.loads(bytes(canonical))["epoch"])
    except (ValueError, KeyError, TypeError):
        epoch = None
    return StageStatus(entry_id, sha, state, epoch, _serving(derived.conn, volatile.conn, entry_id))


def stage_status(bound: BoundShelf, stage_sha256: str) -> StageStatus:
    """What the stores show for one stage digest, by keyed reads only.

    One snapshot under the writer lock, which a concurrent writer can change the moment
    it is released; a busy writer raises ``writer-busy`` (chained to the lock timeout),
    never a guessed status. A malformed digest is ``request-invalid``."""
    if not _is_digest(stage_sha256):
        raise ConsumerError("request-invalid", stage="index")
    with bound._writer("biblios stage-status"):
        with opened(bound.binding.palace_root, read_only=False) as (derived, volatile):
            assert volatile is not None
            row = volatile.conn.execute(
                "SELECT entry_id, stage_sha256, canonical, state FROM stages WHERE stage_sha256=?",
                (stage_sha256,),
            ).fetchone()
            if row is not None:
                return _stage_row(derived, volatile, row)
            row = derived.conn.execute(
                "SELECT canonical FROM accepted_stages WHERE stage_sha256=?", (stage_sha256,)
            ).fetchone()
            state = "recorded"
            if row is None:
                row = derived.conn.execute(
                    "SELECT stage_canonical FROM cancellations WHERE stage_sha256=?",
                    (stage_sha256,),
                ).fetchone()
                state = "cancelled"
            if row is None:
                return StageStatus(None, stage_sha256, "absent", None, False)
            try:
                document = json.loads(bytes(row[0]))
                entry_id, epoch = str(document["entry"]["entry_id"]), str(document["epoch"])
            except (ValueError, KeyError, TypeError) as error:
                raise ConsumerError(
                    "stage-tampered", stage="index", effect="recovery-required"
                ) from error
            return StageStatus(entry_id, stage_sha256, state, epoch, False)


def pending_stages(
    bound: BoundShelf,
    entry_ids: Sequence[str] | None = None,
    *,
    after: str | None = None,
    limit: int = 100,
) -> StagePage:
    """Persisted, not yet settled stages in entry order, at most ``limit`` (1 to
    :data:`MAX_PAGE`) per call: of the named entries (at most ``limit`` of them, by keyed
    reads) or, with None, of the whole shelf from just after ``after``.

    Each page is one snapshot under the writer lock (a busy writer raises
    ``writer-busy``, chained). Pages are keyed by entry id, so a traversal never repeats
    an entry and returns every entry whose stage stayed persisted throughout; a stage
    persisted or settled between pages may be missed or appear. Information only."""
    if (
        type(limit) is not int
        or not 0 < limit <= MAX_PAGE
        or not (after is None or type(after) is str)
        or (
            entry_ids is not None
            and (
                isinstance(entry_ids, str)
                or len(entry_ids) > limit
                or any(type(item) is not str for item in entry_ids)
            )
        )
    ):
        raise ConsumerError("page-invalid", stage="index")
    with bound._writer("biblios pending-stages"):
        with opened(bound.binding.palace_root, read_only=False) as (derived, volatile):
            assert volatile is not None
            query = "SELECT entry_id, stage_sha256, canonical, state FROM stages"
            clauses, values = [], []
            if entry_ids is not None:
                names = sorted(set(entry_ids))
                if not names:
                    return StagePage((), None)
                clauses.append(f"entry_id IN ({', '.join('?' * len(names))})")
                values.extend(names)
            if after is not None:
                clauses.append("entry_id > ?")
                values.append(after)
            if clauses:
                query += " WHERE " + " AND ".join(clauses)
            rows = volatile.conn.execute(
                query + " ORDER BY entry_id LIMIT ?", (*values, limit + 1)
            ).fetchall()
            more = len(rows) > limit
            stages = tuple(_stage_row(derived, volatile, row) for row in rows[:limit])
            return StagePage(stages, stages[-1].entry_id if more else None)


def entry_status(bound: BoundShelf, entry_id: str) -> EntryStatus:
    """One entry's record, pending stage, mark and recorded refusal, by keyed reads only;
    one snapshot under the writer lock (a busy writer raises ``writer-busy``, chained).
    Information only: never the caller's acceptance, and never a reason to act."""
    if type(entry_id) is not str or not entry_id:
        raise ConsumerError("request-invalid", stage="index")
    with bound._writer("biblios entry-status"):
        with opened(bound.binding.palace_root, read_only=False) as (derived, volatile):
            assert volatile is not None
            record = derived.record(entry_id)
            row = volatile.conn.execute(
                "SELECT entry_id, stage_sha256, canonical, state FROM stages WHERE entry_id=?",
                (entry_id,),
            ).fetchone()
            dirty = volatile.conn.execute(
                "SELECT 1 FROM dirty WHERE entry_id=? LIMIT 1", (entry_id,)
            ).fetchone()
            refusal = volatile.conn.execute(
                "SELECT code FROM refusals WHERE entry_id=? LIMIT 1", (entry_id,)
            ).fetchone()
            return EntryStatus(
                entry_id,
                None if record is None else record.state,
                None if record is None else record.accepted_stage_sha256,
                None if record is None else record.mirror_sha256,
                None if row is None else _stage_row(derived, volatile, row),
                dirty is not None,
                None if refusal is None else str(refusal[0]),
            )


def capture_donor(bound: BoundShelf, entry_id: str) -> ReuseDonor:
    """Capture an entry's actual accepted view as a reuse nominee for a later request.

    Refuses ``donor-not-complete`` unless the entry is live, accepted, and its mirror is
    exactly its accepted record. Retaining the nominee reserves nothing."""
    from biblios.binding import validate_handle
    from biblios.pipeline import ReuseDonor

    with bound._writer("biblios donor capture"):
        shelf = bound._shelf
        with shelf._handle.mutex:
            epoch = validate_handle(bound.binding, shelf._handle).epoch
            entry = _Castle(bound).read_entries([entry_id]).get(entry_id)
        with opened(bound.binding.palace_root, read_only=False) as (derived, _volatile):
            record = derived.record(entry_id)
        try:
            present = read_mirror(shelf.mirror_path(entry_id), shelf.mirror_root)
        except MirrorInvalid:
            present = None
        if (
            entry is None
            or record is None
            or record.state != "accepted"
            or record.accepted_stage_sha256 is None
            or record.source_cas_id != entry.cas_id
            or present is None
            or digest(present) != record.mirror_sha256
        ):
            raise ConsumerError("donor-not-complete", stage="index")
        return ReuseDonor(
            entry_id,
            bound.binding.namespace_id,
            epoch,
            entry.cas_id,
            record.mirror_sha256,
            record.accepted_stage_sha256,
        )


def discard_stage(bound: BoundShelf, entry_id: str, stage_sha256: str) -> bool:
    """Cancel one persisted stage at the caller's request: it is never served or
    re-presented. Idempotent: False when no such stage remains. A stage whose B4 began is
    refused ``stage-serving`` (recovery-required): it is finished, or cancelled with
    :func:`cancel_stage`. The decision to cancel, and any successor work, belong to the
    caller.

    This is a generic utility, not a decision. An authenticating caller invokes it only
    after its own durable write-grade decision bound to exactly this stage, its
    predecessor, epoch and current signed head (a signed cancellation or void, or an
    already-accepted cleanup), never from :func:`stage_status`, a mutable state flag or
    an absent record. Until then the stage and its evidence stay; discarding a retained
    accepted stage before its acknowledgment loses its exact re-presentation.

    A stage whose entry was removed, or whose source was replaced, after it was
    authorized is never finished: every later call reports it (``entry-removed`` or
    ``source-replaced``; ``recovery-required`` once B4 began) with no effect and reads
    withhold the entry. Biblios never cancels it automatically, with or without an
    authority. Disposing of an unserved one is this call, made by the caller; until it is
    made the entry's successor work waits. A serving one cannot be discarded here; its
    disposition is :func:`cancel_stage`."""
    with bound._writer("biblios discard-stage"):
        with opened(bound.binding.palace_root, read_only=False) as (derived, volatile):
            assert volatile is not None
            row = volatile.conn.execute(
                "SELECT stage_sha256 FROM stages WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if row is None:
                return False
            if row[0] != stage_sha256:
                raise ConsumerError("stage-mismatch", stage="index")
            if _serving(derived.conn, volatile.conn, entry_id) or _cancellation_begun(
                derived.conn, stage_sha256
            ):
                raise ConsumerError("stage-serving", stage="index", effect="recovery-required")
            with transaction(volatile.conn):
                _drop_stage(volatile.conn, entry_id)
            return True


_SHA256 = frozenset("0123456789abcdef")


def _is_digest(value) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _SHA256


def _is_generation(value) -> bool:
    return type(value) is GenerationIdentity and all(
        type(getattr(value, name)) is str and getattr(value, name)
        for name in ("namespace_id", "store_id", "lineage_id", "database_generation", "epoch")
    )


def _validate_cancellation(request: StageCancellation) -> None:
    if type(request) is not StageCancellation:
        raise ConsumerError("request-invalid", stage="index")
    try:
        if str(uuid.UUID(request.operation_id)) != request.operation_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ConsumerError("operation-invalid", stage="index") from None
    if (
        type(request.entry_id) is not str
        or not request.entry_id
        or not _is_digest(request.stage_sha256)
        or not (request.predecessor_sha256 is None or _is_digest(request.predecessor_sha256))
        or not _is_generation(request.stage_generation)
        or not _is_generation(request.current_generation)
        or type(request.reason) is not str
        or not 0 < len(request.reason) <= 64
        or not (request.supersedes_sha256 is None or _is_digest(request.supersedes_sha256))
    ):
        raise ConsumerError("request-invalid", stage="index")


def _parse_cancellation(canonical: bytes, request_sha256: str) -> StageCancellation:
    """Rebuild persisted cancellation bytes and prove they round-trip to their digest."""
    if hashlib.sha256(canonical).hexdigest() != request_sha256:
        raise ConsumerError("cancellation-tampered", stage="index", effect="recovery-required")
    try:
        document = json.loads(canonical)
        if document.pop("schema") != "biblios.stage-cancellation/2":
            raise ValueError
        request = StageCancellation(
            **{
                **document,
                "stage_generation": GenerationIdentity(**document["stage_generation"]),
                "current_generation": GenerationIdentity(**document["current_generation"]),
            }
        )
    except (ValueError, KeyError, TypeError) as error:
        raise ConsumerError(
            "cancellation-tampered", stage="index", effect="recovery-required"
        ) from error
    if request.sha256 != request_sha256:
        raise ConsumerError("cancellation-tampered", stage="index", effect="recovery-required")
    return request


def _cancellation_begun(derived: sqlite3.Connection, stage_sha256: str) -> bool:
    """Whether a caller-signed cancellation link exists for this stage: from C2's first
    write on, no ordinary call may finish, re-present or discard it."""
    row = derived.execute(
        "SELECT 1 FROM cancellation_chain WHERE stage_sha256=? LIMIT 1", (stage_sha256,)
    ).fetchone()
    return row is not None


def cancel_stage(
    bound: BoundShelf,
    request: StageCancellation,
    *,
    authority: StageAuthority | None,
    control: CallControl = CallControl(),
) -> CancellationResult:
    """Cancel one serving stage (B4 begun, B5 not finished) on the caller's signed decision.

    A serving stage is one marked ``serving`` before any write (W1), one whose entry's
    ``served_seq`` is odd (W2), one whose B4 wrote its mirror (W3) and one whose Palace
    commit landed (W4); in W1 and W2 nothing has moved and nothing moves here. An unserved
    stage (``authorized`` or ``promoted``) is refused ``stage-not-serving``: its
    disposition is :func:`discard_stage`. Never automatic, and never without an
    authority: a shelf without one has no serving cancellation.

    A shelf that has never had a stage authority (standalone) therefore has no
    disposition at all for a serving stage; nothing here implies that an authority will
    arrive. What happens to such a stage, exactly:

    - Recovered by an ordinary later call, with no new embedding: the entry is still
      live under the same name and source and the shelf generation is unchanged, so the
      stage resumes from its persisted rows and finishes. That covers a failed Palace
      commit, a crash or failure at any serving window, and a lost result.
    - Retried but not finishing while the cause persists: an update seam or exchange
      whose expected bytes are no longer at the path (the user saved the mirror after
      the stage was prepared) fails ``mirror-changed-concurrently`` on each call, and
      the user's bytes are never overwritten.
    - Fenced for good: the entry was removed, renamed or its source replaced after the
      stage was authorized, or the shelf generation changed. Every call reports
      ``recovery-required`` with the stage digest, reads withhold the entry, and the
      stage, its rows and the user's bytes are kept. Nothing disposes of it: not this
      call, not :func:`discard_stage`, and not excision, which refuses ``stage-pending``
      for an entry with a persisted stage. That entry stays withheld; the rest of the
      shelf is unaffected.

    The request binds the OLD stage exactly (its digest, predecessor and the generation
    it was persisted under) and the shelf's CURRENT generation exactly. The two must
    share namespace, store and lineage; only the epoch and database generation may
    differ, so a stage fenced by a changed epoch has this one disposition, by the
    caller's current authority, and never by inference from the mismatch.

    Order: (C0) the stage, its signed resume document and the request are verified;
    the request's generations, the current generation and the entry's accepted stage must
    match, else ``cancellation-conflict`` with no effect; the stage must be serving.
    (C1) the caller signs exactly ``request.canonical``
    (:meth:`StageAuthority.authorize_cancellation`). (C2) the request and signature are
    appended, insert-only, as the next link of the stage's cancellation chain, then the
    stage is marked ``cancelling``; from the link on, no ordinary call touches the stage.
    (C3) only what B4 itself wrote for this entry is undone, idempotently: Palace's file
    is deleted when it holds exactly the stage's indexed bytes (see below for bytes equal
    to the predecessor's); a mirror B4 wrote is moved, digest-bound, to a retained name
    fixed by the stage and its mirror digest (reason ``cancelled``), never deleted;
    ``served_seq`` is made even; only the creation grant the stage consumed is dropped.
    (C4) an insert-only receipt of the completing link and the stage bytes is written,
    then the stage is dropped. Every link of the chain stays.

    Nothing is restored, finished, embedded or re-served: the predecessor record is
    never changed, and reads withhold the entry wherever its accepted rows or mirror are
    no longer in place (the predecessor's displaced mirror stays retained, a first
    mirror written before staging stays where it is, and a removal's retirement and
    Palace deletion stay done). When the stage indexed exactly the predecessor's bytes,
    Palace's rows are left only when they reproduce the signed predecessor stage; any
    difference deletes them, so the entry is withheld, never served with the cancelled
    stage's vectors.

    A restart of the chain's tip request never asks for a new signature: it verifies the
    persisted link and asks :meth:`StageAuthority.recheck_cancellation`, which must answer
    ``pending``; the current generation must still be the one the tip names, else
    ``stage-fenced`` (recovery-required). Such a tip is continued only by a new request
    naming it in ``supersedes_sha256`` and the generation the shelf now has: a fresh
    caller decision, appended as the next link (one linear chain, never a fork). The
    authority for every current effect is the tip alone: its signature, made by the
    caller's current authority (C1) or rechecked by it on resume, covers bytes naming the
    exact digest of the request it supersedes, and so transitively every earlier link.
    Earlier links' signatures were made under generations that may no longer exist; they
    are kept exactly as history and are never authority. Biblios verifies every link's
    bytes, position and linkage, but it neither verifies nor asks the caller to recheck
    an earlier link's opaque evidence; a caller that requires that does so when it
    authorizes or rechecks the tip, from its own records. A request
    the chain already superseded is ``cancellation-superseded``. After completion the
    tip's repeat is replayed from the persisted bytes, with no current-state guard, and
    only when the caller answers ``completed``; Biblios's receipt is history, never
    authority. A different request for the same stage, or the same operation id for a
    different request, is ``cancellation-conflict`` with no effect. A StageRefused from
    the caller passes through unchanged and has no effect. The writer lock is held and
    no other work of this lifetime may be active (``work-active``)."""
    _validate_cancellation(request)
    if authority is None:
        raise ConsumerError("stage-authority-required", stage="index")
    with bound._writer("biblios cancel-stage", control=control):
        if bound.active_work != 1:
            raise ConsumerError("work-active", stage="index")
        with opened(bound.binding.palace_root, read_only=False) as (derived, volatile):
            assert volatile is not None
            return _Cancellation(bound, request, authority, control, derived, volatile).run()


class _Cancellation:
    def __init__(self, bound, request, authority, control, derived, volatile) -> None:
        self.bound, self.request, self.authority, self.control = bound, request, authority, control
        self.derived, self.volatile = derived.conn, volatile.conn
        self.shelf = bound._shelf
        self.root = bound.binding.palace_root
        self.entry_id = request.entry_id
        self.path = self.shelf.mirror_path(request.entry_id)

    def _conflict(self, effect: str = "none") -> ConsumerError:
        return ConsumerError("cancellation-conflict", stage="index", effect=effect)

    def _chain(self, stage: Stage) -> list[tuple]:
        """The stage's whole cancellation chain, every link verified before use: positions
        contiguous from 0, each link's canonical bytes round-tripping to its digest and
        naming this stage and entry, the first superseding nothing and each later one
        superseding exactly its predecessor's digest, and each evidence within bounds.
        The evidence itself is the caller's to verify; Biblios's chain is never
        authority."""
        links = self.derived.execute(
            "SELECT position, operation_id, request_sha256, request_canonical, evidence"
            " FROM cancellation_chain WHERE stage_sha256=? ORDER BY position",
            (stage.stage_sha256,),
        ).fetchall()
        previous = None
        for index, (position, operation_id, request_sha, canonical, evidence) in enumerate(links):
            persisted = _parse_cancellation(bytes(canonical), request_sha)
            if (
                position != index
                or persisted.operation_id != operation_id
                or persisted.stage_sha256 != stage.stage_sha256
                or persisted.entry_id != stage.entry.entry_id
                or persisted.stage_generation != stage.generation()
                or persisted.supersedes_sha256 != previous
                or len(bytes(evidence)) > MAX_EVIDENCE
            ):
                raise ConsumerError(
                    "cancellation-tampered", stage="index", effect="recovery-required"
                )
            previous = request_sha
        return links

    def run(self) -> CancellationResult:
        request = self.request
        link = self.derived.execute(
            "SELECT stage_sha256, request_sha256 FROM cancellation_chain WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()
        if link is not None and (link[0], link[1]) != (request.stage_sha256, request.sha256):
            raise self._conflict()
        receipt = self.derived.execute(
            "SELECT operation_id, request_sha256, request_canonical, stage_sha256,"
            " stage_canonical, evidence, retained FROM cancellations WHERE stage_sha256=?",
            (request.stage_sha256,),
        ).fetchone()
        row = self.volatile.execute(
            "SELECT stage_sha256, canonical FROM stages WHERE entry_id=?", (self.entry_id,)
        ).fetchone()
        present = row is not None and row[0] == request.stage_sha256
        if receipt is not None:
            if receipt[0] != request.operation_id:
                raise ConsumerError(
                    "cancellation-superseded" if link is not None else "cancellation-conflict",
                    stage="index",
                )
            if receipt[1] != request.sha256:
                raise self._conflict()
            # Completed: answered from the persisted bytes before any current-state guard.
            return self._replay(receipt, present)
        if row is None:
            raise ConsumerError("stage-absent", stage="index")
        if not present:
            raise ConsumerError("stage-mismatch", stage="index")
        stage = _parse_stage(bytes(row[1]), row[0])
        self._document(stage)
        chain = self._chain(stage)
        tip = chain[-1] if chain else None
        if tip is not None and tip[1] == request.operation_id:
            return self._resume(stage, tip)
        if link is not None:
            raise ConsumerError("cancellation-superseded", stage="index")
        if tip is None:
            if request.supersedes_sha256 is not None:
                raise self._conflict()
        else:
            persisted = _parse_cancellation(bytes(tip[3]), tip[2])
            if request.supersedes_sha256 != persisted.sha256:
                raise self._conflict()
            if persisted.current_generation == self._current():
                # The tip can still continue under its own signature: no fork.
                raise self._conflict()
        self._bound_to(stage, request, resuming=False)
        if not _serving(self.derived, self.volatile, self.entry_id):
            raise ConsumerError("stage-not-serving", stage="index")
        # C1: the caller's current-authority signature over exactly these bytes.
        evidence = _answer(
            lambda: self.authority.authorize_cancellation(stage, request), self.control, "index"
        )
        if type(evidence) is not bytes or len(evidence) > MAX_EVIDENCE:
            raise ConsumerError("evidence-invalid", stage="index")
        # C2: the link first (durable history, and the fence for ordinary calls), then
        # the flag; a restart of this request verifies the link and never re-signs.
        with transaction(self.derived):
            self.derived.execute(
                "INSERT INTO cancellation_chain VALUES (?, ?, ?, ?, ?, ?)",
                (
                    stage.stage_sha256,
                    len(chain),
                    request.operation_id,
                    request.sha256,
                    request.canonical,
                    evidence,
                ),
            )
        self._flag(stage)
        return self._finish(stage, request, evidence)

    def _current(self) -> GenerationIdentity:
        from biblios.binding import validate_handle

        with self.shelf._handle.mutex:
            view = validate_handle(self.bound.binding, self.shelf._handle)
        return GenerationIdentity(
            view.namespace_id, view.store_id, view.lineage_id, view.database_generation, view.epoch
        )

    def _flag(self, stage: Stage) -> None:
        with transaction(self.volatile):
            self.volatile.execute(
                "UPDATE stages SET state='cancelling' WHERE entry_id=? AND stage_sha256=?",
                (self.entry_id, stage.stage_sha256),
            )

    def _bound_to(self, stage: Stage, request: StageCancellation, *, resuming: bool) -> None:
        """Before any effect, in this order: (1) the request names exactly this stage, its
        predecessor and the generation it was persisted under, and its two generations
        share namespace, store and lineage, else ``cancellation-conflict``; (2) the
        shelf's current generation is the one the request names, else
        ``cancellation-conflict`` for a new request and ``stage-fenced``
        (recovery-required) for a resumed one, which only a superseding request can
        continue; (3) the entry's accepted stage is still the request's predecessor, else
        ``cancellation-conflict``, recovery-required once begun (nothing accepts for an
        entry while its stage persists, so a difference is an out-of-band change). The
        generations are the shelf's (Castle's binding); the caller's own key epoch is the
        caller's to bind in its evidence and recheck."""
        old, new = request.stage_generation, request.current_generation
        if (
            stage.entry.entry_id != request.entry_id
            or stage.stage_sha256 != request.stage_sha256
            or stage.predecessor_sha256 != request.predecessor_sha256
            or stage.generation() != old
            or (old.namespace_id, old.store_id, old.lineage_id)
            != (new.namespace_id, new.store_id, new.lineage_id)
        ):
            raise self._conflict()
        if self._current() != new:
            if resuming:
                raise ConsumerError("stage-fenced", stage="index", effect="recovery-required")
            raise self._conflict()
        record = self.derived.execute(
            "SELECT accepted_stage_sha256 FROM entries WHERE entry_id=?", (self.entry_id,)
        ).fetchone()
        if (None if record is None else record[0]) != request.predecessor_sha256:
            raise self._conflict("recovery-required" if resuming else "none")

    def _resume(self, stage: Stage, tip: tuple) -> CancellationResult:
        """The chain's tip request again: verified from its persisted link, never
        re-signed, and only while the caller still holds it pending."""
        persisted = _parse_cancellation(bytes(tip[3]), tip[2])
        if persisted.sha256 != self.request.sha256:
            raise self._conflict()
        self._bound_to(stage, persisted, resuming=True)
        evidence = bytes(tip[4])
        if self._recheck(stage, persisted, evidence) != "pending":
            # Effects may be incomplete; completion claimed before the receipt exists is
            # contradictory, and nothing moves.
            raise ConsumerError(
                "cancellation-unverified", stage="index", effect="recovery-required"
            )
        self._flag(stage)
        return self._finish(stage, persisted, evidence)

    def _replay(self, receipt, present: bool) -> CancellationResult:
        """A completed cancellation's repeat: rebuilt only from the receipt's bytes, with no
        current-state guard (the generation and predecessor may since have changed). The
        stage may still be present after an interruption between the receipt and its drop;
        every effect is already done, so only the drop remains."""
        op, request_sha, request_canonical, stage_sha, stage_canonical, evidence, retained = receipt
        persisted = _parse_cancellation(bytes(request_canonical), request_sha)
        stage = _parse_stage(bytes(stage_canonical), stage_sha)
        answer = self._recheck(stage, persisted, bytes(evidence))
        if present:
            with transaction(self.volatile):
                _drop_stage(self.volatile, self.entry_id)
        elif answer != "completed":
            raise ConsumerError("cancellation-unverified", stage="index")
        return CancellationResult(
            "completed-replay" if answer == "completed" else "cancelled",
            persisted.operation_id,
            stage.stage_sha256,
            tuple(json.loads(retained)),
        )

    def _finish(self, stage, request, evidence) -> CancellationResult:
        retained = self._undo(stage, request)
        # C4: the receipt only after every effect, then the stage drop, so an
        # interruption between them leaves only the drop to resume.
        with transaction(self.derived):
            self.derived.execute(
                "INSERT INTO cancellations VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    request.operation_id,
                    request.sha256,
                    request.canonical,
                    stage.stage_sha256,
                    stage.canonical,
                    evidence,
                    json.dumps(list(retained)),
                ),
            )
        with transaction(self.volatile):
            _drop_stage(self.volatile, self.entry_id)
        return CancellationResult("cancelled", request.operation_id, stage.stage_sha256, retained)

    def _document(self, stage: Stage) -> dict:
        """The stage's resume document, verified against the digest the signed stage
        carries: every C3 decision comes from signed bytes, never an unsigned row."""
        resume = self.volatile.execute(
            "SELECT document FROM stage_resume WHERE entry_id=?", (self.entry_id,)
        ).fetchone()
        if resume is None or _text_digest(resume[0]) != stage.resume_sha256:
            raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
        return json.loads(resume[0])

    def _observe(self) -> CancellationObservation:
        row = self.volatile.execute(
            "SELECT state FROM stages WHERE entry_id=?", (self.entry_id,)
        ).fetchone()
        seq = self.derived.execute(
            "SELECT served_seq FROM entries WHERE entry_id=?", (self.entry_id,)
        ).fetchone()
        try:
            present = read_mirror(self.path, self.shelf.mirror_root)
        except MirrorInvalid:
            present = None
        return CancellationObservation(
            row is not None,
            None if row is None else row[0],
            self._palace_hash(),
            seq is None or int(seq[0]) % 2 == 0,
            None if present is None else digest(present),
        )

    def _palace_hash(self) -> str | None:
        from biblios.progress import read_only_index, store_built

        if not store_built(self.shelf):
            return None
        with read_only_index(self.shelf.chunks_db) as conn:
            row = conn.execute(
                "SELECT file_hash FROM files WHERE path=?", (self.entry_id + ".md",)
            ).fetchone()
        return None if row is None else row[0]

    def _recheck(self, stage, request, evidence) -> str:
        observed = self._observe()
        answer = _answer(
            lambda: self.authority.recheck_cancellation(stage, request, evidence, observed),
            self.control,
            "index",
        )
        if answer not in ("pending", "completed"):
            raise ConsumerError("evidence-invalid", stage="index")
        return answer

    def _undo(self, stage: Stage, request: StageCancellation) -> tuple[str, ...]:
        """C3: undo what B4 itself wrote for this entry; idempotent, so a restart repeats
        it safely. Returns the name of the file this cancellation retained, if any: the
        retirement uses a fixed name chosen from the stage's digest and its mirror digest
        (never the operation, so every request of the chain finds it). After a crash at
        any point, including between the rename and its provenance record, one keyed
        lookup finds it and its record is completed."""
        planned = stage.entry
        document = self._document(stage)
        if planned.action == "publish":
            # A digest equal to the predecessor's proves nothing about the committed
            # vectors: Palace's rows stay only when they are exactly the rows the signed
            # predecessor stage records, and are otherwise deleted, so reads withhold the
            # entry rather than serve the cancelled stage's vectors.
            palace = self._palace_hash()
            if palace == planned.indexed_sha256 and not self._predecessor_serves(request, palace):
                self._delete_indexed()
            if (
                (
                    document["expected_exchange"] is not None
                    or planned.mirror_provenance == "update-seam"
                )
                and planned.mirror_sha256 != planned.prior_mirror_sha256
                and not document["preexisting"]
            ):
                self._retire_written(planned, request)
        with transaction(self.derived):
            seq = self.derived.execute(
                "SELECT served_seq FROM entries WHERE entry_id=?", (self.entry_id,)
            ).fetchone()
            if seq is not None and int(seq[0]) % 2 == 1:
                self.derived.execute(
                    "UPDATE entries SET served_seq=? WHERE entry_id=?",
                    (int(seq[0]) + 1, self.entry_id),
                )
        grant = document["grant"]
        if grant is not None and planned.action != "remove" and grant[0] != "removed":
            # Only the exact creation grant this stage consumed (named by its signed resume
            # document: role and curation operation), as B5 would have consumed it; any
            # other grant row for the entry, including a removal's, stays.
            with transaction(self.volatile):
                self.volatile.execute(
                    "DELETE FROM grants WHERE entry_id=? AND cas_id=? AND role=?"
                    " AND operation_id IS ? AND role!='removed'",
                    (self.entry_id, planned.source_cas_id, grant[0], grant[1]),
                )
        return self._retained_names(planned, request)

    def _retention(self, planned: StageEntry, request: StageCancellation) -> Provenance:
        with self.shelf._handle.mutex:
            revision = self.shelf.revision
        return Provenance(
            self.root,
            "cancelled",
            planned.source_cas_id,
            planned.mirror_sha256,
            request.operation_id,
            revision,
        )

    def _retained_names(self, planned: StageEntry, request: StageCancellation) -> tuple[str, ...]:
        from biblios.mirrors import record_retention_once, retained_path

        if planned.action != "publish" or planned.mirror_sha256 is None:
            return ()
        target = retained_path(
            self.path.parent,
            self.entry_id,
            "cancelled-" + self.request.stage_sha256,
            planned.mirror_sha256,
        )
        if target.is_symlink():
            raise ConsumerError("artifact-outside-domain", stage="index")
        if not target.is_file():
            return ()
        if digest(target.read_bytes()) != planned.mirror_sha256:
            # Not the bytes this cancellation retired: left exactly as found, unlisted and
            # unrecorded, and the cancellation does not complete.
            raise ConsumerError(
                "mirror-retention-conflict", stage="index", effect="recovery-required"
            )
        record_retention_once(target, self.entry_id, self._retention(planned, request))
        return (target.name,)

    def _predecessor_serves(self, request: StageCancellation, palace: str | None) -> bool:
        """Whether Palace's committed file is exactly the predecessor stage the caller
        signed (``request.predecessor_sha256``): that stage's canonical bytes, re-verified
        against the digest, record the indexed digest, row count and content digests the
        committed rows must reproduce. The mutable derived rows (entries, accepted
        chunks) are never consulted. Any doubt, including a Palace refusal to read the
        rows (identity, malformed, over its published default limits), is ``False``."""
        from palace.index import IndexError as PalaceIndexError
        from palace.index import read_file_vectors
        from palace.index.embedder_config import identity_for, load_embedder_config

        if request.predecessor_sha256 is None:
            return False
        row = self.derived.execute(
            "SELECT canonical FROM accepted_stages WHERE entry_id=? AND stage_sha256=?",
            (self.entry_id, request.predecessor_sha256),
        ).fetchone()
        if row is None:
            return False
        try:
            predecessor = _parse_stage(bytes(row[0]), request.predecessor_sha256).entry
        except ConsumerError:
            return False
        if predecessor.action != "publish" or predecessor.indexed_sha256 != palace:
            return False
        try:
            identity = identity_for(load_embedder_config(self.root))
            with _open_index(self.shelf, self.root, identity) as conn:
                snapshot = read_file_vectors(
                    conn=conn,
                    watch_root=self.shelf.mirror_root.resolve(),
                    path=self.entry_id + ".md",
                    expected_identity=identity,
                )
        except PalaceIndexError:
            return False
        if snapshot is None or snapshot.file.file_hash != palace:
            return False
        served = _snapshot_rows(snapshot)
        return len(served) == predecessor.row_count and content_digest(served) == (
            predecessor.content_sha256,
            predecessor.chunk_manifest_sha256,
        )

    def _delete_indexed(self) -> None:
        from palace.index import IndexError as PalaceIndexError
        from palace.index.core import delete_path
        from palace.index.embedder_config import identity_for, load_embedder_config

        try:
            identity = identity_for(load_embedder_config(self.root))
            with _open_index(self.shelf, self.root, identity) as conn:
                delete_path(
                    conn=conn,
                    watch_root=str(self.shelf.mirror_root.resolve()),
                    path_str=self.entry_id + ".md",
                )
        except PalaceIndexError as error:
            raise ConsumerError(
                "index-identity-mismatch", stage="index", effect="recovery-required"
            ) from error

    def _retire_written(self, planned: StageEntry, request: StageCancellation) -> None:
        try:
            present = read_mirror(self.path, self.shelf.mirror_root)
        except MirrorInvalid:
            return
        if present is None or digest(present) != planned.mirror_sha256:
            return
        retire_mirror(
            self.path,
            provenance=self._retention(planned, request),
            expected=planned.mirror_sha256,
            label="cancelled-" + self.request.stage_sha256,
        )


# ---- the core -------------------------------------------------------------------------


def _request_digest(request: BringCurrentRequest) -> str:
    update = request.update
    document = {
        "operation": request.operation_id,
        "entries": None if request.entry_ids is None else sorted(request.entry_ids),
        "expected_revision": request.expected_revision,
        "retry_uncertain": request.retry_uncertain,
        "update": None
        if update is None
        else [update.entry_id, update.expected_mirror_sha256, digest(update.new_mirror)],
        "expected_entries": None
        if request.expected_entries is None
        else {key: asdict(value) for key, value in sorted(request.expected_entries.items())},
        "voided": [
            [v.entry_id, v.stage_sha256, v.reason, v.evidence.hex()] for v in request.voided
        ],
        "observations": [asdict(item) for item in request.observations],
        "reembed": sorted(request.reembed),
        "sweep": request.sweep,
        "resume_only": request.resume_only,
        "donors": None
        if request.donors is None
        else {key: asdict(value) for key, value in sorted(request.donors.items())},
    }
    return hashlib.sha256(canonical_bytes(document)).hexdigest()


def _validate(request: BringCurrentRequest) -> None:
    from biblios.pipeline import ReuseDonor

    try:
        parsed = uuid.UUID(request.operation_id)
        if str(parsed) != request.operation_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ConsumerError("operation-invalid", stage="index") from None
    if any(
        type(item) is not ReadObservation
        or item.category not in ("mirror-changed", "rows-mismatch", "missing-rows")
        or type(item.entry_id) is not str
        or type(item.served_seq) is not int
        or item.served_seq < 0
        for item in request.observations
    ):
        raise ConsumerError("request-invalid", stage="index")
    if request.resume_only is not False and (
        request.resume_only is not True
        or not request.entry_ids
        or request.sweep
        or request.reembed
        or request.update is not None
        or request.donors
        or request.expected_entries
        or request.observations
        or request.voided
    ):
        raise ConsumerError("request-invalid", stage="index")
    if (
        type(request.retry_uncertain) is not bool
        or type(request.sweep) is not bool
        or (request.entry_ids is not None and type(request.entry_ids) is not tuple)
        or type(request.reembed) is not tuple
        or any(len(void.evidence) > MAX_EVIDENCE for void in request.voided)
        or (
            request.donors is not None
            and any(
                type(key) is not str or type(value) is not ReuseDonor
                for key, value in request.donors.items()
            )
        )
    ):
        raise ConsumerError("request-invalid", stage="index")


def resume_stages(
    bound: BoundShelf,
    operation_id: str,
    entry_ids: Sequence[str],
    *,
    pipeline: ConsumerPipeline,
    authority: StageAuthority | None = None,
    control: CallControl = CallControl(),
) -> BringCurrentResult:
    """Finish only the named entries' persisted stages, through :func:`bring_current`'s
    own resume (every reread verified, the authority's ``recheck`` and ``promote`` as
    there), and nothing else: no discovery, sweep, legacy import, conversion,
    preparation, new embedding or checkpoint seal.

    An entry with no persisted stage is ``pending``/``no-stage`` and left exactly as it
    was (unmarked). A stage that settles while its entry still has later work (a new
    source, an update, a rename) is ``pending``/``work-after-resume`` and stays marked
    for an ordinary call. Every other outcome is the resume's own. The result is
    recorded under ``operation_id`` and replayed like any other call's."""
    if isinstance(entry_ids, str):
        raise ConsumerError("request-invalid", stage="index")
    return bring_current(
        bound,
        BringCurrentRequest(operation_id, tuple(entry_ids), resume_only=True),
        pipeline=pipeline,
        authority=authority,
        control=control,
    )


def _resume_document(prepared: _Prepared) -> str:
    """What finishing a persisted stage needs beyond its canonical bytes and rows."""
    return json.dumps(
        {
            "conversion": prepared.conversion,
            "grant": None if prepared.grant is None else list(prepared.grant),
            "expected_exchange": prepared.expected_exchange,
            "indexed": [prepared.indexed_cut, prepared.indexed_suffix],
            "preexisting": prepared.preexisting,
        },
        sort_keys=True,
    )


def _name_section(markdown: str, heading: str) -> str | None:
    """The renderer's name section: from the last heading line naming ``heading`` to the
    end, or None when the rendering has no such final section."""
    import re

    found = None
    for match in re.finditer(r"(?m)^#{1,6}[ \t]+" + re.escape(heading) + r"[ \t]*$", markdown):
        found = match
    return None if found is None else markdown[found.start() :]


def _drop_stage(conn: sqlite3.Connection, entry_id: str) -> None:
    for table in ("stage_rows", "stage_resume", "stages"):
        conn.execute(f"DELETE FROM {table} WHERE entry_id=?", (entry_id,))


def _serving(derived: sqlite3.Connection, volatile: sqlite3.Connection, entry_id: str) -> bool:
    """Whether B4 began for this entry's stage and B5 has not finished: the stage marked
    ``serving`` (set before any mirror or Palace write, so a first ingestion is covered)
    or ``cancelling``, or an odd ``served_seq``. A discard refuses on it and a cancellation
    requires it, so a forged flag can only block a discard or admit a caller-signed
    cancellation, never an effect without the caller's signature."""
    stage = volatile.execute("SELECT state FROM stages WHERE entry_id=?", (entry_id,)).fetchone()
    if stage is not None and stage[0] in ("serving", "cancelling"):
        return True
    row = derived.execute("SELECT served_seq FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
    return row is not None and int(row[0]) % 2 == 1


def _stage_entry(fields: dict) -> StageEntry:
    checkpoint = fields.get("checkpoint")
    if checkpoint is not None:
        fields = {**fields, "checkpoint": StageCheckpoint(**checkpoint)}
    return StageEntry(**fields)


def _parse_stage(canonical: bytes, stage_sha256: str) -> Stage:
    """Rebuild a persisted stage and prove it round-trips to the same digest."""
    if hashlib.sha256(canonical).hexdigest() != stage_sha256:
        raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
    try:
        document = json.loads(canonical)
        voids = tuple(
            VoidRecord(
                item["entry_id"],
                item["stage_sha256"],
                item["reason"],
                bytes.fromhex(item["evidence"]),
            )
            for item in document["voids"]
        )
        stage = build_stage(
            schema=document["schema"],
            stage_id=document["stage_id"],
            namespace_id=document["namespace_id"],
            epoch=document["epoch"],
            store_id=document["store_id"],
            lineage_id=document["lineage_id"],
            database_generation=document["database_generation"],
            observed_revision=document["observed_revision"],
            view_sha256=document["view_sha256"],
            operation_id=document["operation_id"],
            predecessor_sha256=document["predecessor_sha256"],
            entry=_stage_entry(document["entry"]),
            derivation=DerivationIdentity(**document["derivation"]),
            embedder=EmbedderIdentity(**document["embedder"]),
            donor=None if document["donor"] is None else DonorIdentity(**document["donor"]),
            reused_from_sha256=document["reused_from_sha256"],
            creation=(
                None if document["creation"] is None else CreationIdentity(**document["creation"])
            ),
            voids=voids,
            resume_sha256=document["resume_sha256"],
            authenticated=document["authenticated"],
        )
    except (ValueError, KeyError, TypeError) as error:
        raise ConsumerError("stage-tampered", stage="index", effect="recovery-required") from error
    if stage.stage_sha256 != stage_sha256:
        raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
    return stage


def _index_hashes(shelf) -> dict[str, str]:
    """Palace's per-entry file hashes, read once for a sweep; empty for a missing or
    unusable store, whose every accepted entry is then re-presented."""
    from biblios.progress import indexed_files, store_built

    try:
        return indexed_files(shelf) if store_built(shelf) else {}
    except (sqlite3.Error, BibliosError):
        return {}


def _result_document(result: BringCurrentResult) -> str:
    stripped = replace(result, entries=tuple(replace(item, cause=None) for item in result.entries))
    document = asdict(stripped)
    for item in document["entries"]:
        del item["cause"]
    return json.dumps(document, sort_keys=True)


def _result_from(document: str) -> BringCurrentResult:
    value = json.loads(document)
    return BringCurrentResult(
        operation_id=value["operation_id"],
        revision=value["revision"],
        entries=tuple(EntryOutcome(**item) for item in value["entries"]),
        accepted=tuple(AcceptedStage(**item) for item in value["accepted"]),
        yielded=value["yielded"],
        replayed=True,
        provider_calls=value["provider_calls"],
        swept=value["swept"],
        redacted=value["redacted"],
    )


def bring_current(
    bound: BoundShelf,
    request: BringCurrentRequest,
    *,
    pipeline: ConsumerPipeline,
    authority: StageAuthority | None = None,
    control: CallControl = CallControl(),
    should_yield: Callable[[], bool] | None = None,
) -> BringCurrentResult:
    """Bring the named or discovered entries current; the only orchestration.

    Holds the Palace writer lock for the call and the namespace mutex only for Castle
    reads. Embedding and every caller callback run outside the namespace mutex, and no
    derived write transaction is open across one.
    """
    _validate(request)
    with bound._writer("biblios bring-current", control=control, recover=request.sweep):
        with opened(bound.binding.palace_root, read_only=False) as (derived, volatile):
            assert volatile is not None
            run = _Run(
                bound, request, pipeline, authority, control, should_yield, derived, volatile
            )
            return run.execute()


@dataclass(slots=True)
class _Prepared:
    entry: Entry
    record: DerivedRecord | None
    action: Literal["publish", "remove", "unconverted"]
    provenance: str
    data: bytes | None = None
    plan: FilePlan | None = None
    rows: list[tuple[str, str, str, bytes]] = field(default_factory=list)
    converter: str = ""
    converter_version: str = ""
    conversion: str = "converted"
    body_sha256: str | None = None
    grant: tuple | None = None
    expected_exchange: str | None = None
    """The digest an exchange must displace (update seam or replaced source)."""
    embedded: int = 0
    reused: int = 0
    donor: DonorIdentity | None = None
    donor_rows: tuple[tuple[str, tuple[float, ...], bool], ...] | None = None
    """The nominated donor's committed (input digest, vector, is-name-row) rows, in order."""
    mirror_written: bool = False
    """A resumed stage whose exact mirror is already at the path: B4 writes nothing."""
    prior_mirror: str | None = None
    """A removal before any acceptance: the digest of the first mirror it retires."""
    indexed: bytes | None = None
    """The bytes Palace indexes: the mirror, or its current-filename projection."""
    projection: str | None = None
    indexed_cut: int = 0
    indexed_suffix: str = ""
    read_stat: StatIdentity | None = None
    """The stat identity of the descriptor ``data`` was read from, when B1 read it."""

    reused_from: str | None = None
    """The accepted stage whose committed rows were reused (signed in the stage)."""
    preexisting: bool = False
    """The path already held the stage's exact mirror bytes when it was prepared, so B4
    writes none and a cancellation has no mirror of its own to retire."""
    pending: _Pending | None = None
    """Planned (B0) but not yet embedded: the provider inputs still to be answered."""
    migrating: bool = False
    """A named, unchanged entry whose accepted stage signs no membership root: it is
    re-staged from exactly its committed rows, with no embedding, or not at all."""
    checkpoint: StageCheckpoint | None = None
    """The sealed or verified conversion checkpoint this entry's content derives from."""


@dataclass(frozen=True, slots=True)
class _Pending:
    """What one entry still asks of the provider, and how its answer is assembled."""

    inputs: tuple[str, ...]
    missing: tuple[int, ...]
    donated: Mapping[int, tuple[float, ...]]
    reusable: Mapping[tuple[str, str], Sequence[float]]
    source: str | None


class _Job:
    """One entry's provider work in an overlapped call; owned by the calling thread."""

    def __init__(self, prepared: _Prepared) -> None:
        self.prepared = prepared
        inputs = prepared.pending.inputs if prepared.pending is not None else ()
        self.batches = [
            tuple(inputs[start : start + EMBED_SLICE])
            for start in range(0, len(inputs), EMBED_SLICE)
        ]
        self.total = len(inputs)
        self.sent = 0
        self.collected = 0
        self.outstanding: deque[tuple[Future, tuple[str, ...]]] = deque()
        self.answers: list[list[float]] = []
        self.error: object | None = None
        self.settled = prepared.pending is None

    def unsent(self) -> bool:
        return self.error is None and self.sent < len(self.batches)

    def next_batch(self) -> tuple[str, ...]:
        batch = self.batches[self.sent]
        self.sent += 1
        return batch

    def fail(self, run: _Run, error: Exception) -> None:
        """Report the entry once, as the serial loop would; its later answers are dropped."""
        if self.error is None:
            self.error = error
            entry_id = self.prepared.entry.entry_id
            if isinstance(error, ConsumerError):
                run._refused(entry_id, error)
            else:
                run._report(entry_id, "pending", "prepare-failed", cause=error)
        self.settle_if_drained()

    def settle_if_drained(self) -> None:
        if self.error is not None and not self.outstanding:
            self.settled = True

    def complete(self) -> bool:
        return self.error is None and self.prepared.pending is None


class _Run:
    def __init__(
        self,
        bound: BoundShelf,
        request: BringCurrentRequest,
        pipeline: ConsumerPipeline,
        authority: StageAuthority | None,
        control: CallControl,
        should_yield: Callable[[], bool] | None,
        derived: DerivedStore,
        volatile: VolatileStore,
    ) -> None:
        self.bound, self.request, self.pipeline = bound, request, pipeline
        self.authority, self.control = authority, control
        self.should_yield = should_yield or (lambda: False)
        self.derived, self.volatile = derived, volatile
        self.castle = _Castle(bound)
        self.shelf = bound._shelf
        self.root = bound.binding.palace_root
        self.outcomes: list[EntryOutcome] = []
        self._checkpoints_verified: dict[str, bytes] = {}
        """Checkpoint digests the caller verified in this call, with the seal it accepted."""
        self.unverified: dict[str, Exception] = {}
        """Entries whose read observation could not be verified, with the cause."""
        self.refusal_causes: dict[str, Exception] = {}
        """This call's converter refusals, given as the unconverted outcome's cause."""
        self.repairs: set[str] = set()
        """Entries this call proved damaged only in the accepted per-hit rows, while
        Palace still serves exactly the accepted stage's rows: re-committed from those
        rows under a stage the caller authorizes now, with no provider call."""
        self.imported: set[str] = set()
        self.paths: dict[str, str] = {}
        self.swept_index: dict[str, str] | None = None
        self.accepted: list[AcceptedStage] = []
        self.provider_calls = 0

    # -- entry ---------------------------------------------------------------------------

    def execute(self) -> BringCurrentResult:
        request = self.request
        recorded = self.derived.conn.execute(
            "SELECT request_sha256, result FROM operations WHERE operation_id=?",
            (request.operation_id,),
        ).fetchone()
        wanted = _request_digest(request)
        if recorded is not None:
            if recorded[0] != wanted:
                raise ConsumerError("operation-conflict", stage="index")
            if recorded[1] is not None:
                return _result_from(recorded[1])
        with self.shelf._handle.mutex:
            revision = self.castle.observe_revision()
        if request.expected_revision is not None and request.expected_revision != revision:
            raise ConsumerError("stale-view", stage="index")
        self.revision = revision
        self.generation = self.castle.generation
        self._refuse_moved_index()
        from biblios.binding import validate_handle

        with self.shelf._handle.mutex:
            self.view = validate_handle(self.bound.binding, self.shelf._handle)
        self.epoch = self.view.epoch
        with transaction(self.derived.conn):
            self.derived.conn.execute(
                "INSERT OR IGNORE INTO operations VALUES (?, ?, NULL)",
                (request.operation_id, wanted),
            )
        self.identity = self._identity()
        self._verify_store_identity()
        self.lane = lane_for(self.root)
        self.shelf.mirror_root.mkdir(parents=True, exist_ok=True)
        self._observed()
        if not request.resume_only:
            self._import_legacy()
        ceiling, candidates = self._candidates()
        candidates = sorted(set(candidates) | self.imported)
        yielded = False
        live = self._live(candidates)
        ordered = sorted(candidates, key=lambda e: (live[e].size or 0, e) if e in live else (0, e))
        width = self._width()
        if width == 1:
            visited = 0
            for entry_id in ordered:
                if self.should_yield():
                    yielded = True
                    break
                self.control.check("index")
                self._bring(entry_id, live.get(entry_id), ceiling)
                visited += 1
            unreached = ordered[visited:]
        else:
            yielded, unreached = self._overlapped(ordered, live, ceiling, width)
        # Advancing the observed revision must never lose work: an entry left unsettled
        # or not reached stays marked, so the next discovery finds it again.
        unsettled = [
            item.entry_id
            for item in self.outcomes
            if item.outcome in ("uncertain", "recovery-required", "busy")
            or (
                item.outcome == "pending"
                and item.code not in ("intent-mismatch", "legacy-unmatched", "no-stage")
            )
        ]
        requeued = unsettled + unreached
        self.volatile.mark_dirty(requeued, source="requeue")
        self.unsettled = len(set(requeued))
        result = BringCurrentResult(
            request.operation_id,
            self.revision,
            tuple(self.outcomes),
            tuple(self.accepted),
            yielded,
            False,
            self.provider_calls,
            request.sweep,
        )
        if request.sweep:
            self._reconcile_marker()
        with transaction(self.derived.conn):
            self.derived.conn.execute(
                "UPDATE operations SET result=? WHERE operation_id=?",
                (_result_document(result), request.operation_id),
            )
            self.derived.set_counter("observed_revision", self.revision)
            self.derived.set_meta("observed_generation", self.generation)
            self.derived.set_counter("unsettled", self.unsettled)
        return result

    def _observed(self) -> None:
        """Act on the readers' observations (ONE-09) before any entry is brought.

        A reader's report is a hint, never evidence: under the writer lock each still-
        current one is checked against the entry's accepted stage, and only damage this
        call proves isolates the entry. Proven means the rows Palace serves are not the
        stage's (``read_file_vectors``), or the accepted per-hit rows and membership
        nodes do not rebuild the root the stage signs. A stale report (another serving
        sequence) and a disproved one are dropped; replaying one changes nothing. A check
        that cannot run (Palace refuses to read the entry's committed file) is neither:
        the entry is ``pending``/``observation-unverified`` with Palace's error as its
        cause, stays marked, and is otherwise left as it was."""
        from palace.index import IndexError as PalaceIndexError

        for item in self.request.observations:
            if item.category == "mirror-changed":
                continue  # the entry is a candidate; its mirror is classified as usual
            record = self.derived.record(item.entry_id)
            if (
                record is None
                or record.state != "accepted"
                or record.served_seq != item.served_seq
                or record.served_seq % 2
            ):
                continue  # stale: the entry changed since the read, or is not served
            stage = self._accepted_stage(record)
            if stage is None or stage.entry.chunk_root_sha256 is None:
                continue  # nothing this call can prove against; a reader withholds it
            try:
                served = self._served_as(item.entry_id, stage)
            except PalaceIndexError as error:
                # Not proof of damage, and not a disproof: the entry's outcome says the
                # check could not run, with Palace's refusal (and its trace) as the cause.
                self.unverified[item.entry_id] = error
                continue
            if served and self._accepted_rows_match(item.entry_id, stage):
                continue  # disproved: a read racing a commit
            damaged = replace(record, state="served-rows-damaged")
            with transaction(self.derived.conn):
                account(self.derived, record, damaged)
                self.derived.put(damaged)
            if served and self.authority is not None:
                # Only the derived per-hit rows are damaged and Palace still holds the
                # exact accepted rows: re-commit them through the ordinary path, under a
                # stage the caller's authority authorizes in this call. Without that
                # authority, or with Palace's rows changed, the entry stays isolated.
                self.repairs.add(item.entry_id)

    def _accepted_rows_match(self, entry_id: str, stage: Stage) -> bool:
        """Whether the per-hit rows and membership nodes the reader checks against are
        exactly the stage's: the accepted rows rebuild the signed root and every stored
        node is the rebuilt tree's (O(rows of the entry))."""
        from biblios.membership import leaf_hash, root_of, tree_nodes

        rows = self.derived.conn.execute(
            "SELECT position, chunk_id, body_sha256, input_sha256, vector_sha256 "
            "FROM accepted_chunks WHERE entry_id=? ORDER BY position",
            (entry_id,),
        ).fetchall()
        if len(rows) != stage.entry.row_count or [row[0] for row in rows] != list(range(len(rows))):
            return False
        try:
            nodes = tree_nodes([leaf_hash(*row) for row in rows])
        except (TypeError, ValueError):
            return False
        if root_of(nodes, len(rows)).hex() != stage.entry.chunk_root_sha256:
            return False
        stored = {
            (lo, hi): value
            for lo, hi, value in self.derived.conn.execute(
                "SELECT lo, hi, hash FROM accepted_nodes WHERE entry_id=?", (entry_id,)
            )
        }
        return stored == nodes

    def _width(self) -> int:
        """How many provider calls may be outstanding at once: the policy's bound and the
        embedder's declared ``max_concurrency``, whichever is smaller. A declaration that
        is not a positive int refuses the call before any provider call."""
        declared = getattr(self.pipeline.embedder, "max_concurrency", None)
        if type(declared) is not int or declared < 1:
            raise ConsumerError("embedder-capability-invalid", stage="embedding")
        return min(self.pipeline.policy.max_concurrency, declared)

    def _overlapped(
        self, ordered: list[str], live: Mapping[str, Entry], ceiling: int, width: int
    ) -> tuple[bool, list[str]]:
        """Bring entries with up to ``width`` provider calls outstanding (ONE-03).

        Everything except the provider call itself runs here, on the calling thread:
        yielding and cancellation, every control check, conversion, planning, progress,
        answer checks, assembly, the authority's calls, every store write and every
        commit. A provider call is detached only as ``embedder.embed`` over a fresh list
        of strings, whose return value is all that comes back. At most ``width`` entries
        are prepared at once and they finish strictly in entry order, so the outcomes,
        commits and replay are the serial loop's. A raised check stops new work, waits
        for every outstanding provider call to return, and propagates; no provider call
        is ever repeated, and none outlives this call."""
        position = {entry_id: index for index, entry_id in enumerate(ordered)}
        active: deque[_Job] = deque()
        admitted, yielded = 0, False
        with ThreadPoolExecutor(max_workers=width, thread_name_prefix="biblios-provider") as pool:
            while True:
                while admitted < len(ordered) and len(active) < width and not yielded:
                    if self.should_yield():
                        yielded = True
                        break
                    self.control.check("index")
                    entry_id = ordered[admitted]
                    admitted += 1
                    prepared = self._head(entry_id, live.get(entry_id), ceiling)
                    if prepared is not None:
                        active.append(_Job(prepared))
                    self._dispatch(active, pool, width)
                self._dispatch(active, pool, width)
                if not active:
                    break
                job = active[0]
                if job.settled:
                    active.popleft()
                    if job.complete():
                        self._finish_entry(job.prepared, ceiling)
                    continue
                self._collect(job)
        self.outcomes.sort(key=lambda item: position.get(item.entry_id, len(ordered)))
        return yielded, ordered[admitted:]

    def _dispatch(self, active: deque[_Job], pool: ThreadPoolExecutor, width: int) -> None:
        """Submit the next provider slices, in entry order, while fewer than ``width``
        are outstanding; the checks before each call run here, as the serial loop's do."""
        outstanding = sum(len(job.outstanding) for job in active)
        for job in active:
            while outstanding < width and job.unsent():
                try:
                    self.control.check("embedding")
                    self.control.check("embedding", effect="uncertain")
                except ConsumerError as error:
                    job.fail(self, error)
                    break
                if job.sent == 0:
                    try:
                        # The commit fence, run before any spend (as the serial loop does).
                        self._fence(job.prepared)
                    except ConsumerError as error:
                        job.fail(self, error)
                        break
                batch = job.next_batch()
                embed = self.pipeline.embedder.embed
                job.outstanding.append((pool.submit(embed, list(batch)), batch))
                outstanding += 1

    def _collect(self, job: _Job) -> None:
        """Take the oldest outstanding answer of the first entry, in order, here."""
        if not job.outstanding:
            # Every slice answered (the first entry is always dispatched before any later
            # one, so it never waits on capacity): complete its rows here.
            assert not job.unsent()
            job.settled = True
            if job.error is None:
                try:
                    self._assemble(job.prepared, job.answers)
                except Exception as error:
                    job.error = None
                    job.fail(self, error)
            return
        future, batch = job.outstanding.popleft()
        start = job.collected
        job.collected += len(batch)
        if job.error is not None:
            future.exception()  # an earlier failure: wait for it, never use it
            job.settle_if_drained()
            return
        try:
            try:
                returned = future.result()
            except Exception as error:
                raise ConsumerError(
                    "callback-failed", stage="embedding", effect="uncertain"
                ) from error
            self.control.check("embedding", effect="uncertain")
            self.provider_calls += 1
            job.answers.extend(
                self._answered(job.prepared.entry, batch, returned, start, job.total)
            )
        except Exception as error:
            job.fail(self, error)
        job.settle_if_drained()

    def _identity(self) -> EmbeddingIdentity:
        from palace.index.embedder_config import identity_for, load_embedder_config

        config = load_embedder_config(self.root)
        if (
            config.representation is not None
            and config.representation != self.pipeline.representation
        ):
            raise ConsumerError("representation-mismatch", stage="index")
        self.representation = config.representation
        return identity_for(config)

    def _verify_store_identity(self) -> None:
        """Before any download, provider call or write: a built store must record exactly
        the declared embedding identity. A disagreement is never an ordinary miss and
        never a rebuild; it refuses the call with Palace's own diagnosis as the cause."""
        from palace.index import IndexError as PalaceIndexError

        if not self.shelf.chunks_db.is_file():
            return
        try:
            with self._index():
                pass
        except PalaceIndexError as error:
            raise ConsumerError(
                "index-identity-mismatch", stage="index", effect="recovery-required"
            ) from error

    def _candidates(self) -> tuple[int, list[str]]:
        request = self.request
        ceiling, marked = self.volatile.marked()
        if request.entry_ids is not None:
            found = set(request.entry_ids)
        elif request.sweep:
            self.swept_index = _index_hashes(self.shelf)
            with self.shelf._handle.mutex:
                live = self.castle.snapshot()
            found = {key for key, value in live.items() if value.kind == "file"}
            found |= {row[0] for row in self.derived.conn.execute("SELECT entry_id FROM entries")}
        else:
            since = self.derived.counter("observed_revision")
            found = set(marked)
            if since or self.derived.exists():
                if self.derived.meta("observed_generation") != self.generation:
                    # A new database generation (an excision cutover, or a store this
                    # release never observed) is resynchronized from revision zero, and
                    # every recorded entry is re-examined, since an excised record leaves
                    # no change behind.
                    since = 0
                    found |= {
                        row[0] for row in self.derived.conn.execute("SELECT entry_id FROM entries")
                    }
                with self.shelf._handle.mutex:
                    found |= self.castle.changed_since(since)
        if request.update is not None:
            found.add(request.update.entry_id)
        found |= {item.entry_id for item in request.observations}
        found |= set(request.reembed)
        return ceiling, sorted(found)

    def _live(self, entry_ids: list[str]) -> dict[str, Entry]:
        if not entry_ids:
            return {}
        with self.shelf._handle.mutex:
            return self.castle.read_entries(entry_ids)

    def _answer(self, call, control: CallControl, stage):
        return _answer(call, control, stage)

    def _unindexed(self, entry_id: str, record: DerivedRecord) -> bool:
        """Only a sweep asks: is an accepted entry missing from, or stale in, the index?

        A lost or damaged index is recovered by re-presenting each accepted mirror; an
        ordinary call never reads the index per entry.
        """
        if self.swept_index is None or record.state != "accepted":
            return False
        return self.swept_index.get(entry_id) != record.index_digest

    def _reconcile_marker(self) -> None:
        """The explicit sweep is the repair for a stale or forged ``accepted-ever``
        marker: with no accepted state and no persisted stage it is removed, so the
        shelf reads as never built again. Accepted state always keeps it."""
        marker = self.root / ACCEPTED_MARKER
        if not marker.is_file() or self.derived.counter("state:accepted") > 0:
            return
        if self.volatile.conn.execute("SELECT 1 FROM stages LIMIT 1").fetchone() is not None:
            return
        marker.unlink()

    # -- the bounded verified preservation import ---------------------------------------

    def _import_legacy(self) -> None:
        """Give every legacy entry an explicit record, once per shelf (plan decision 1).

        Runs in the first writer call on a shelf that has no import record and was never
        accepted under this release (a lost store, marked ``accepted-ever``, is not a
        legacy shelf). Bounded batches commit records only, with cancellation checked
        before each; nothing is converted, embedded, rewritten or deleted, and Castle is
        never written. An entry is ``legacy-verified`` only when its current mirror is
        exactly the recorded digest of a conversion of its current source and Palace's
        committed rows are exactly what that mirror plans to; every other legacy entry
        is ``legacy-unmatched`` and stays pending. Verified entries are then staged by
        the ordinary committer with zero embedding (``_imported``).
        """
        if self.derived.counter("legacy-import") or (self.root / ACCEPTED_MARKER).is_file():
            return
        from biblios.entries import view

        with self.shelf._handle.mutex:
            live = self.castle.snapshot()
        root_id = self.shelf._root_id
        legacy = [view(record, live, root_id) for record in live.values() if record.kind == "file"]
        legacy = [entry for entry in legacy if entry.metadata and entry.metadata.legacy]
        for start in range(0, len(legacy), IMPORT_BATCH):
            self.control.check("index")
            batch = [
                (entry, self._legacy_record(entry))
                for entry in legacy[start : start + IMPORT_BATCH]
                if self.derived.record(entry.entry_id) is None
            ]
            with transaction(self.derived.conn):
                for _entry, record in batch:
                    account(self.derived, None, record)
                    self.derived.put(record)
        with transaction(self.derived.conn):
            self.derived.set_counter("legacy-import", 1)
        self.imported = {entry.entry_id for entry in legacy}

    def _legacy_record(self, entry: Entry) -> DerivedRecord:
        keys = entry.metadata.legacy
        base = DerivedRecord(
            entry_id=entry.entry_id,
            source_cas_id=keys.get("source_cas_id") or None,
            conversion=keys.get("conversion", ""),
            converter=keys.get("converter", ""),
            converter_version=keys.get("converter_version", ""),
            mirror_sha256=keys.get("mirror_sha256", ""),
            stat=None,
            recorded_ns=time.time_ns(),
            state="legacy-unmatched",
        )
        if not base.converted or base.source_cas_id != entry.cas_id:
            return base
        try:
            data = read_mirror(self.shelf.mirror_path(entry.entry_id), self.shelf.mirror_root)
        except MirrorInvalid:
            return base
        if data is None or digest(data) != base.mirror_sha256:
            return base
        if self._legacy_rows(entry, data) is None:
            return base
        return replace(base, state="legacy-verified", indexed_sha256=digest(data))

    def _legacy_rows(self, entry: Entry, data: bytes) -> list[tuple[str, str, str, bytes]] | None:
        """Palace's committed rows for this mirror when they are exactly what its current
        bytes plan to (order, chunk ids, bodies, input digests from the pipeline's input
        view); vectors as committed, never recomputed. None otherwise."""
        from palace.index import IndexError as PalaceIndexError
        from palace.index import plan_one, read_file_vectors, with_embedding_inputs

        path = self.shelf.mirror_path(entry.entry_id)
        root = self.shelf.mirror_root.resolve()
        try:
            with self._index() as conn:
                snapshot = read_file_vectors(
                    conn=conn,
                    watch_root=root,
                    path=path.resolve().relative_to(root).as_posix(),
                    expected_identity=self.identity,
                    max_chunks=self.pipeline.policy.max_chunks,
                    max_bytes=self.pipeline.max_snapshot_bytes,
                )
                plan = plan_one(
                    conn=conn,
                    file_kind="markdown",
                    change_kind="modified",
                    path=path.resolve(),
                    watch_root=root,
                    full=True,
                    source_bytes=data,
                    log=lambda line: None,
                )
        except PalaceIndexError:
            return None
        if snapshot is None or snapshot.file.file_hash != digest(data):
            return None
        if self.representation is not None:
            inputs = controlled(
                lambda: tuple(self.pipeline.input_view(plan)), self.control, "embedding"
            )
            if len(inputs) != len(plan.rows):
                return None
            plan = with_embedding_inputs(plan, representation=self.representation, inputs=inputs)
        planned = [
            (row.chunk_id, row.body, _text_digest(text))
            for row, text in zip(plan.rows, plan.embed_inputs, strict=True)
        ]
        served = _snapshot_rows(snapshot)
        if [(chunk_id, body, inputs) for chunk_id, body, inputs, _ in served] != planned:
            return None
        return served

    def _imported(self, entry: Entry, record: DerivedRecord, path: Path) -> _Prepared | None:
        """An n=1 import stage of a ``legacy-verified`` entry: zero embedding, its rows
        Palace's committed rows re-verified now, and B4 writing nothing (the one stage
        whose rows predate authorization; the caller sees provenance ``import``)."""
        try:
            data = read_mirror(path, self.shelf.mirror_root)
        except MirrorInvalid as error:
            self._report(entry.entry_id, "isolated:" + error.code, cause=error)
            return None
        rows = None
        if data is not None and digest(data) == record.mirror_sha256:
            rows = self._legacy_rows(entry, data)
        if rows is None:
            # No longer exactly what was verified: it joins the unmatched entries.
            unmatched = replace(record, state="legacy-unmatched", indexed_sha256=None)
            with transaction(self.derived.conn):
                account(self.derived, record, unmatched)
                self.derived.put(unmatched)
            self._report(entry.entry_id, "pending", "legacy-unmatched")
            return None
        prepared = _Prepared(
            entry,
            record,
            "publish",
            "import",
            data,
            rows=rows,
            converter=record.converter,
            converter_version=record.converter_version,
            conversion=record.conversion,
        )
        prepared.projection, prepared.indexed_cut = "exact", len(data)
        prepared.indexed = data
        # A declined import proceeds only through the caller's explicit re-embedding
        # (``request.reembed``, which never reaches here); naming it or a retry does not.
        refused = self._refused_before(prepared, named_clears=False)
        if refused is not None:
            self._report(entry.entry_id, "pending", refused)
            return None
        return prepared

    def _refuse_moved_index(self) -> None:
        """Refuse, before any effect, to extend an index recorded under another root.

        Palace records each file under the absolute mirror root of its indexing; a copy
        opened elsewhere still serves by entry id and digest, but adding rows would mix two
        roots in one store. One row decides it, so the check is constant-cost.
        """
        db = self.shelf.chunks_db
        if not db.is_file():
            return
        from biblios.progress import read_only_index

        with read_only_index(db) as conn:
            built = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='files'"
            ).fetchone()
            row = conn.execute("SELECT watch_root FROM files LIMIT 1").fetchone() if built else None
        if row is not None and row[0] != str(self.shelf.mirror_root.resolve()):
            raise ConsumerError("index-root-moved", stage="index")

    def _report(self, entry_id: str, outcome: str, code: str | None = None, **extra) -> None:
        self.outcomes.append(EntryOutcome(entry_id, outcome, code, **extra))
        self._progress(entry_id, outcome)

    def _progress(
        self, entry_id: str, stage: str, done: int | None = None, total: int | None = None
    ) -> None:
        if self.pipeline.progress is None:
            return
        from biblios.progress import EntryProgress

        path = self.paths.get(entry_id, "")
        controlled(
            lambda: self.pipeline.progress(
                EntryProgress(self.bound.binding.shelf_id, entry_id, path, stage, done, total)
            ),
            self.control,
            "index",
        )

    # -- one entry -----------------------------------------------------------------------

    def _bring(self, entry_id: str, entry: Entry | None, ceiling: int) -> None:
        prepared = self._head(entry_id, entry, ceiling)
        if prepared is None:
            return
        if prepared.pending is not None:
            pending = prepared.pending
            done = self._contained(
                entry_id,
                lambda: self._assemble(prepared, self._embed(prepared, list(pending.inputs))),
            )
            if done is False:
                return
        self._finish_entry(prepared, ceiling)

    def _head(self, entry_id: str, entry: Entry | None, ceiling: int) -> _Prepared | None:
        """Everything before the provider call: resume, intent check, classification and
        planning (B0). ``None`` once the entry is settled or reported."""
        if entry is not None:
            self.paths[entry_id] = entry.path
        if entry_id in self.unverified:
            self._report(
                entry_id, "pending", "observation-unverified", cause=self.unverified[entry_id]
            )
            return None
        record = self.derived.record(entry_id)
        unresolved = self.volatile.conn.execute(
            "SELECT stage_sha256, canonical, state, evidence, promotion FROM stages "
            "WHERE entry_id=?",
            (entry_id,),
        ).fetchone()
        if unresolved is None and self.request.resume_only:
            self._report(entry_id, "pending", "no-stage")
            return None
        if unresolved is not None:
            try:
                current = self._resume(entry_id, entry, unresolved)
            except ConsumerError as error:
                self._refused(entry_id, error)
                return None
            except Exception as error:
                self._report(entry_id, "recovery-required", "publication-failed", cause=error)
                return None
            if current is None:
                return None  # still pending: reported, marks kept, requeued
            record = self.derived.record(entry_id)
            if current and not self._work_after_resume(entry_id, entry, record):
                self._clear(entry_id, ceiling)
                return None
            if self.request.resume_only:
                # The stage is settled; the entry's later work waits for an ordinary call.
                self.outcomes = [item for item in self.outcomes if item.entry_id != entry_id]
                self._report(entry_id, "pending", "work-after-resume")
                return None
            # The resumed stage described an earlier source, or this call still has work
            # for the entry (an update, a re-embedding, a rename or bytes that differ from
            # the record): the resumed stage stays the truthful record of what it covered,
            # and the entry continues from there, so that work is never dropped.
            self.outcomes = [item for item in self.outcomes if item.entry_id != entry_id]
        expected = (self.request.expected_entries or {}).get(entry_id)
        if expected is not None and not self._matches(expected, entry, record):
            self._report(entry_id, "pending", "intent-mismatch")
            return None
        prepared = self._contained(entry_id, lambda: self._prepare(entry_id, entry, record))
        if prepared is False:
            return None
        if prepared is None:
            self._clear(entry_id, ceiling)
            return None
        return prepared

    def _contained(self, entry_id: str, step: Callable[[], object]) -> object:
        """Run one step of an entry's preparation; ``False`` once its failure is reported.

        Nothing is persisted before B2, so a failure while preparing (a store, file or
        provider error) is scoped to its entry: reported with its cause, marks kept for
        the next call, and the call continues with the rest."""
        try:
            return step()
        except ConsumerError as error:
            self._refused(entry_id, error)
        except Exception as error:
            self._report(entry_id, "pending", "prepare-failed", cause=error)
        return False

    def _finish_entry(self, prepared: _Prepared, ceiling: int) -> None:
        """B1 onward for an entry whose rows are complete: always on the calling thread."""
        entry_id = prepared.entry.entry_id
        try:
            self._publish(prepared)
        except ConsumerError as error:
            self._refused(entry_id, error)
            return
        except Exception as error:
            # A failure while serving is scoped to its entry (F016): its persisted stage
            # stays for restart re-presentation, and the call continues with the rest.
            self._report(entry_id, "recovery-required", "publication-failed", cause=error)
            return
        self._clear(entry_id, ceiling)

    def _work_after_resume(
        self, entry_id: str, entry: Entry | None, record: DerivedRecord | None
    ) -> bool:
        """Whether this call has work for an entry whose earlier stage it just finished.

        Marks are never cleared on the resumed stage alone: the mirror's bytes are
        compared with the record, so an edit made since that stage is indexed now.
        """
        if entry is None or record is None:
            return False
        if self.request.update is not None and self.request.update.entry_id == entry_id:
            return True
        if entry_id in self.request.reembed:
            return True
        if record.state == "served-rows-damaged":
            # Isolated by a verified read observation: the ordinary path reports it
            # isolated (or re-commits it), never "searchable" for the stage just finished.
            return True
        if record.indexed_name not in (None, entry.path.rsplit("/", 1)[-1]):
            return True
        if not record.converted:
            return False
        try:
            data = read_mirror(self.shelf.mirror_path(entry_id), self.shelf.mirror_root)
        except MirrorInvalid:
            return True  # the ordinary path isolates and reports it
        return data is None or digest(data) != record.mirror_sha256

    def _clear(self, entry_id: str, ceiling: int) -> None:
        self.volatile.clear_marks(entry_id, through=ceiling)

    def _refused(self, entry_id: str, error: ConsumerError) -> None:
        if error.effect == "uncertain":
            self._report(entry_id, "uncertain", error.code, cause=error)
        elif error.effect == "recovery-required":
            self._report(entry_id, "recovery-required", error.code, cause=error)
        elif error.code.startswith("isolated:"):
            self._report(entry_id, error.code, cause=error)
        else:
            self._report(entry_id, "pending", error.code, cause=error)

    def _matches(self, expected: ExpectedEntry, entry: Entry | None, record) -> bool:
        if entry is None or expected.source_cas_id != entry.cas_id:
            return False
        accepted = record.accepted_stage_sha256 if record is not None else None
        if expected.mirror_sha256 is not None and (
            record is None or record.mirror_sha256 != expected.mirror_sha256
        ):
            return False
        return expected.predecessor_stage_sha256 == accepted

    # -- classification and preparation --------------------------------------------------

    def _prepare(self, entry_id: str, entry: Entry | None, record: DerivedRecord | None):
        shelf = self.shelf
        path = shelf.mirror_path(entry_id)
        if entry is None:
            if record is None:
                return self._unaccepted_removal(entry_id, path)
            removal = self.volatile.conn.execute(
                "SELECT role, operation_id, kind, request_fingerprint, revision_after, state, "
                "intended_mirror_sha256 FROM grants WHERE entry_id=? AND cas_id=? "
                "AND role='removed'",
                (entry_id, record.source_cas_id),
            ).fetchone()
            prepared = _Prepared(_gone(entry_id), record, "remove", "converted")
            prepared.grant = removal
            return prepared
        if entry.metadata is None:
            self._report(entry_id, "pending", "metadata-required")
            return None
        update = (
            self.request.update
            if self.request.update and self.request.update.entry_id == entry_id
            else None
        )
        grant = self.volatile.conn.execute(
            "SELECT role, operation_id, kind, request_fingerprint, revision_after, state, "
            "intended_mirror_sha256 FROM grants WHERE entry_id=? AND cas_id=? AND role!='removed'",
            (entry_id, entry.cas_id),
        ).fetchone()
        replaced = record is not None and record.source_cas_id != entry.cas_id
        if replaced and record.accepted_stage_sha256 is not None:
            # A recorded source change nulls the accepted identity in the same derived
            # transaction, so the old stage is never served for the new source (C-F026).
            record = self._supersede(record)
        if record is not None and record.state in ("legacy-verified", "legacy-unmatched"):
            granted = grant is not None and grant[5] in ("granted", "materializing")
            if granted and (replaced or grant[0] == "explicit"):
                # A positive creation grant made under this release: an explicit first
                # ingestion, or a recorded replacement of the source.
                return self._first(entry, record, grant, path, replaced)
            named = entry_id in (self.request.entry_ids or ())
            if record.state == "legacy-verified" and not replaced:
                if entry_id not in self.request.reembed:
                    return self._imported(entry, record, path)
            elif replaced or not (record.converted and named):
                # Pending until explicitly named: a valid mirror is then re-indexed from
                # its current bytes; one never converted needs an explicit first
                # ingestion. Nothing is converted, embedded or written meanwhile.
                self._report(entry_id, "pending", "legacy-unmatched")
                return None
        elif record is None or replaced:
            return self._first(entry, record, grant, path, replaced)
        if update is not None:
            return self._update(entry, record, update, path)
        if record.state == "served-rows-damaged" and not (
            entry_id in (self.request.entry_ids or ())
            or entry_id in self.request.reembed
            or entry_id in self.repairs
        ):
            # Isolated by a verified read observation: never served, and re-indexed only
            # when a request names it (its committed rows are reused only where they
            # still reproduce the accepted stage).
            self._report(entry_id, "isolated:served-rows-damaged")
            return None
        try:
            read = read_mirror_stat(path, shelf.mirror_root)
        except MirrorInvalid as error:
            self._report(entry_id, "isolated:" + error.code, cause=error)
            return None
        data, read_stat = (None, None) if read is None else read
        if not record.converted:
            # A recorded unconverted outcome stays until its source changes.
            self._report(entry_id, "unconverted", record.conversion.removeprefix("unconverted:"))
            return None
        if data is None:
            # A converted entry whose mirror is missing is never reconverted; its repair
            # is the explicit update seam.
            self._report(entry_id, "isolated:mirror-missing")
            return None
        found = digest(data)
        reembed = entry_id in self.request.reembed or self._unindexed(entry_id, record)
        renamed = record.indexed_name not in (None, entry.path.rsplit("/", 1)[-1])
        if (
            entry_id in self.repairs
            and entry_id not in (self.request.entry_ids or ())
            and (found != record.mirror_sha256 or reembed or renamed)
        ):
            # A repair re-commits the accepted rows unchanged or not at all: a mirror
            # edit or a rename since acceptance needs new rows, which only a request
            # naming the entry may spend on.
            self._report(entry_id, "isolated:served-rows-damaged")
            return None
        migrating = (
            found == record.mirror_sha256
            and record.state == "accepted"
            and entry_id in (self.request.entry_ids or ())
            and self._unproven(record)
        )
        if (
            found == record.mirror_sha256
            and record.state == "accepted"
            and not reembed
            and not renamed
            and not migrating
        ):
            self._restat(record, read_stat)
            self._report(entry_id, SEARCHABLE, stage_sha256=record.accepted_stage_sha256)
            return None
        refused = record.refused_for(found)
        if refused is not None and entry_id not in (self.request.entry_ids or ()):
            self._report(entry_id, "isolated:" + refused)
            return None
        text = self._validated(entry, data, record)
        if text is None:
            return None
        provenance = "converted" if found == record.mirror_sha256 else "direct-edit"
        return self._planned(
            _Prepared(
                entry,
                record,
                "publish",
                provenance,
                data,
                converter=record.converter if provenance == "converted" else EDIT_CONVERTER,
                converter_version=record.converter_version if provenance == "converted" else "",
                read_stat=read_stat,
                migrating=migrating and not reembed and not renamed,
            )
        )

    def _supersede(self, record: DerivedRecord) -> DerivedRecord:

        new = replace(record, state="superseded", accepted_stage_sha256=None)
        with transaction(self.derived.conn):
            self._account(record, new)
            self.derived.put(new)
            for table in ("accepted_chunks", "accepted_nodes"):
                self.derived.conn.execute(
                    f"DELETE FROM {table} WHERE entry_id=?", (record.entry_id,)
                )
        return new

    def _restat(self, record: DerivedRecord, read_stat: StatIdentity | None) -> None:
        """Refresh a verified unchanged mirror's stat identity so the next pass reads none.

        ``read_stat`` is the identity of the descriptor the verified bytes came from, never
        a later stat of the path, so an edit after the read cannot be recorded as these
        bytes. A racy identity is refreshed too, so it stops forcing rehashes.
        """
        if read_stat is None or (
            record.stat is not None
            and record.stat.unchanged(
                read_stat, recorded_ns=record.recorded_ns, granularity_ns=GRANULARITY_NS
            )
        ):
            return
        with transaction(self.derived.conn):
            self.derived.put(replace(record, stat=read_stat, recorded_ns=time.time_ns()))

    def _validated(self, entry: Entry, data: bytes, record: DerivedRecord | None) -> str | None:
        policy = self.pipeline.policy
        try:
            return validate_mirror(
                data,
                lane=self.lane,
                max_bytes=policy.max_mirror_bytes,
                required_header_fields=policy.required_header_fields,
            )
        except MirrorInvalid as error:
            if record is not None and error.code in POLICY_CLASSES:
                self._record_refusal(record, digest(data), error.code)
            self._report(entry.entry_id, "isolated:" + error.code)
            return None

    def _record_refusal(self, record: DerivedRecord, mirror_digest: str, code: str) -> None:

        with transaction(self.derived.conn):
            self.derived.put(replace(record, refusal=f"{code}:{mirror_digest}"))

    def _update(self, entry: Entry, record: DerivedRecord, update: UpdateSeam, path: Path):
        try:
            existing = read_mirror(path, self.shelf.mirror_root)
        except MirrorInvalid as error:
            self._report(entry.entry_id, "isolated:" + error.code)
            return None
        found = None if existing is None else digest(existing)
        if found != update.expected_mirror_sha256 and found != digest(update.new_mirror):
            self._report(entry.entry_id, "pending", "update-conflict")
            return None
        policy = self.pipeline.policy
        try:
            validate_mirror(
                update.new_mirror,
                lane=self.lane,
                max_bytes=policy.max_mirror_bytes,
                required_header_fields=policy.required_header_fields,
            )
        except MirrorInvalid as error:
            self._report(entry.entry_id, "pending", "mirror-update-invalid:" + error.code)
            return None
        return self._planned(
            _Prepared(
                entry,
                record,
                "publish",
                "update-seam",
                update.new_mirror,
                converter=EDIT_CONVERTER,
                expected_exchange=found,
                preexisting=found == digest(update.new_mirror),
            )
        )

    def _first(self, entry: Entry, record, grant, path: Path, replaced: bool):
        """A first mirror, only from a positive creation grant; never from absence."""
        entry_id = entry.entry_id
        try:
            existing = read_mirror(path, self.shelf.mirror_root)
        except MirrorInvalid as error:
            self._report(entry_id, "isolated:" + error.code)
            return None
        if grant is None or grant[5] not in ("granted", "materializing"):
            if existing is not None and record is None:
                self._report(entry_id, "isolated:mirror-user-owned")
            else:
                self._report(entry_id, "unrecorded", "creation-grant-required")
            return None
        expected_exchange = None
        own_staged = False
        adopted = False
        stale = existing is not None and record is None and self._own_stale(entry, existing)
        if grant[2] == "adopt-existing":
            # A caller-vouched existing mirror (rebuilding a lost store): exactly the
            # vouched bytes, or nothing; never converted over, never written.
            if existing is None or digest(existing) != grant[6]:
                self._report(entry_id, "isolated:mirror-user-owned")
                return None
            adopted, stale = True, False
        elif existing is not None and not stale:
            found = digest(existing)
            if replaced and record is not None and found == record.mirror_sha256:
                expected_exchange = found
            elif grant[5] == "materializing" and found == grant[6]:
                own_staged = True  # this grant's own no-replace write: adopt it if current
            elif replaced and record is not None and record.converted:
                # The old mirror was edited and the edit is not indexed: the user's bytes
                # stay in place untouched, nothing is converted, and the entry is reported
                # (plan F034). Only bytes equal to the record are displaced, above.
                self._report(entry_id, "pending", "replaced-mirror-edited")
                return None
            else:
                self._report(entry_id, "isolated:mirror-user-owned")
                return None
        creation = CreationIdentity(grant[1], grant[0], grant[2], entry.cas_id, grant[3], grant[4])
        if self.authority is not None:
            # Write-grade creation admission precedes every side effect: download,
            # conversion, embedding and any write at the user's path.
            try:
                admitted = self._answer(
                    lambda: self.authority.authorize_creation(creation, entry_id),
                    self.control,
                    "conversion",
                )
            except StageRefused as refused:
                self._report(entry_id, "unrecorded", refused.code)
                return None
            if type(admitted) is not bytes or len(admitted) > MAX_EVIDENCE:
                raise ConsumerError("evidence-invalid", stage="conversion")
        if adopted:
            text = self._validated(entry, existing, record)
            if text is None:
                return None
            return self._planned(
                _Prepared(
                    entry,
                    record,
                    "publish",
                    "direct-edit",
                    existing,
                    converter=EDIT_CONVERTER,
                    grant=grant,
                )
            )
        if stale:
            # Only after write-grade creation admission: the first mirror this core wrote
            # for an earlier source of the entry is retained, digest-bound.
            self._retire_own_stale(entry, existing, path, stale)
            existing = None
        nominated = self._donor_body(entry)
        reuse = None
        if nominated is not None and self.pipeline.policy.reuse_converted_body:
            if self.authority is not None and nominated[1].checkpoint_sha256 is None:
                # The donor's body is reused only from the checkpoint its accepted stage
                # signs and the caller verifies; never silently converted instead.
                self._report(entry_id, "pending", "donor-checkpoint-unproven")
                return None
            reuse = nominated[0], nominated[1].checkpoint_sha256
        converted = self._convert(entry, reuse)
        if converted is None:
            return None
        result, mirror, checkpoint = converted
        if result.outcome == "unconverted":
            return _Prepared(
                entry,
                record,
                "unconverted",
                "converted",
                converter=result.converter,
                converter_version=result.converter_version,
                conversion="unconverted:" + result.reason,
                grant=grant,
                checkpoint=checkpoint,
            )
        data = mirror.markdown.encode("utf-8")
        prepared = _Prepared(
            entry,
            record,
            "publish",
            "copy" if grant[0] == "copied" else "converted",
            data,
            converter=result.converter,
            converter_version=result.converter_version,
            body_sha256=_text_digest(result.body),
            grant=grant,
            expected_exchange=expected_exchange,
            checkpoint=checkpoint,
        )
        if nominated is not None:
            prepared.donor, prepared.donor_rows = nominated[1], nominated[2]
        if own_staged and digest(data) != digest(existing):
            # This grant's own earlier first mirror no longer matches what the current
            # name renders (the entry was renamed before acceptance): it is retained,
            # never deleted or served, and the current rendering is written.
            retire_mirror(
                path,
                provenance=Provenance(
                    self.root,
                    "staging",
                    entry.cas_id,
                    digest(existing),
                    self.request.operation_id,
                    self.revision,
                ),
                expected=digest(existing),
            )
            existing = None
        if expected_exchange is None and existing is None:
            # Creation was admitted before any side effect (authorize_creation with a
            # stage authority). Palace plans only a file that exists, so the first mirror
            # is written here by the no-replace primitive, its intended digest recorded
            # first so a crash adopts exactly these bytes and nothing else.
            self._materialize(prepared, path)
        return self._planned(prepared, name_heading=mirror.name_heading)

    def _unaccepted_removal(self, entry_id: str, path: Path) -> _Prepared | None:
        """An entry removed before any stage was accepted may leave the first mirror this
        core wrote. It is retired only as an ordinary removal stage (no predecessor) that
        names the removal's curation provenance, the source and exactly those bytes, so a
        caller authority authenticates all three at B1 before anything moves. The grant
        rows only find the case: they are never evidence. Bytes that differ from the
        recorded first mirror, or a case without both records, stay the user's."""
        removals = self.volatile.conn.execute(
            "SELECT role, operation_id, kind, request_fingerprint, revision_after, state, "
            "intended_mirror_sha256, cas_id FROM grants WHERE entry_id=? AND role='removed'",
            (entry_id,),
        ).fetchall()
        if len(removals) != 1 or removals[0][6] is None:
            return None
        removal, intended = removals[0], removals[0][6]
        try:
            present = read_mirror(path, self.shelf.mirror_root)
        except MirrorInvalid:
            present = None
        if present is None:
            return None
        if digest(present) != intended:
            self._report(entry_id, "removed", "mirror-user-owned")
            return None
        prepared = _Prepared(
            replace(_gone(entry_id), cas_id=removal[7]), None, "remove", "converted"
        )
        prepared.grant = removal[:7]
        prepared.prior_mirror = intended
        return prepared

    def _own_stale(self, entry: Entry, existing: bytes) -> str | None:
        """The earlier source whose grant recorded exactly these bytes as the first mirror
        this core wrote for the entry (its stage fenced stale before acceptance), else
        None. Read-only; anything else at the path stays the user's."""
        stale = self.volatile.conn.execute(
            "SELECT cas_id FROM grants WHERE entry_id=? AND cas_id!=? "
            "AND state='materializing' AND intended_mirror_sha256=?",
            (entry.entry_id, entry.cas_id, digest(existing)),
        ).fetchone()
        return None if stale is None else stale[0]

    def _retire_own_stale(self, entry: Entry, existing: bytes, path: Path, stale: str) -> None:
        """Retain (never delete or serve) the core's own first mirror for ``stale``,
        exactly those bytes, so the current grant can write the new source's mirror."""
        retire_mirror(
            path,
            provenance=Provenance(
                self.root,
                "source-replaced",
                stale,
                digest(existing),
                self.request.operation_id,
                self.revision,
            ),
            expected=digest(existing),
        )
        with transaction(self.volatile.conn):
            self.volatile.conn.execute(
                "DELETE FROM grants WHERE entry_id=? AND cas_id=?", (entry.entry_id, stale)
            )

    def _materialize(self, prepared: _Prepared, path: Path) -> None:
        """The no-replace first mirror: intended digest recorded, then written."""
        entry = prepared.entry
        wanted = digest(prepared.data)
        with transaction(self.volatile.conn):
            self.volatile.conn.execute(
                "UPDATE grants SET state='materializing', intended_mirror_sha256=? "
                "WHERE entry_id=? AND cas_id=?",
                (wanted, entry.entry_id, entry.cas_id),
            )
        write_mirror(
            path,
            prepared.data,
            expected=None,
            provenance=Provenance(
                self.root, "staging", None, None, self.request.operation_id, self.revision
            ),
        )

    def _conversion_key(self, entry: Entry) -> str:
        """The conversion checkpoint of this entry's source under the current policy. The
        name is not part of it: a rename never re-runs the converter (the renderer, which
        composes the name, runs again for a mirror not yet accepted)."""
        from biblios.pipeline import _policy_document

        return (
            "conversion:"
            + hashlib.sha256(
                canonical_bytes(
                    {
                        "entry": entry.entry_id,
                        "source": entry.cas_id,
                        "media_type": entry.metadata.media_type,
                        "policy": _policy_document(self.pipeline.policy),
                    }
                )
            ).hexdigest()
        )

    def _checkpoint_identity(
        self, entry: Entry, document: dict, generation: GenerationIdentity
    ) -> CheckpointIdentity:
        """The identity of ``document``'s reusable content, rebuilt from its fields: the
        converter's result, and at ``complete`` its rendering and the name rendered for."""
        from biblios.pipeline import _policy_document

        complete = "mirror" in document
        policy = hashlib.sha256(canonical_bytes(_policy_document(self.pipeline.policy))).hexdigest()
        canonical = canonical_bytes(
            {
                "schema": CHECKPOINT_SCHEMA,
                "entry_id": entry.entry_id,
                "source_cas_id": entry.cas_id,
                "media_type": entry.metadata.media_type,
                "policy_sha256": policy,
                "generation": asdict(generation),
                "state": "complete" if complete else "converted",
                "conversion": document["conversion"],
                "mirror": document["mirror"] if complete else None,
                "mirror_name": document["mirror_name"] if complete else None,
                "donor_checkpoint_sha256": document.get("donor_checkpoint_sha256"),
                "conversion_checkpoint_sha256": document.get("conversion_checkpoint_sha256")
                if complete
                else None,
            }
        )
        return CheckpointIdentity(
            entry.entry_id,
            entry.cas_id,
            entry.metadata.media_type,
            policy,
            generation,
            "complete" if complete else "converted",
            canonical,
            hashlib.sha256(canonical).hexdigest(),
        )

    def _generation(self) -> GenerationIdentity:
        return GenerationIdentity(
            self.bound.binding.namespace_id,
            self.view.store_id,
            self.view.lineage_id,
            self.view.database_generation,
            self.epoch,
        )

    def _seal(self, entry: Entry, candidate: dict) -> tuple[dict, str | None] | str:
        """Seal content this call's converter, renderer or verified donor just produced,
        before it is persisted: the fields to persist with it and the sealed digest, or
        the caller's refusal code. Never called for content read back from storage."""
        if self.authority is None:
            return {}, None
        generation = self._generation()
        identity = self._checkpoint_identity(entry, candidate, generation)
        refused: list[str] = []

        def guarded():
            try:
                return self.authority.seal_checkpoint(identity)
            except StageRefused as error:
                refused.append(error.code)
                return None

        seal = controlled(guarded, self.control, "conversion", effect="uncertain")
        if refused:
            return refused[0]
        if type(seal) is not bytes or len(seal) > MAX_EVIDENCE:
            raise ConsumerError("evidence-invalid", stage="conversion", effect="uncertain")
        self._checkpoints_verified[identity.sha256] = seal
        return {"seal": seal.hex(), "generation": asdict(generation)}, identity.sha256

    def _verified(self, entry: Entry, document: dict) -> str | tuple[str]:
        """Verify-only reread of a persisted checkpoint under a stage authority: its
        digest as a 1-tuple when the caller accepts its seal, else the pending code.
        Without an authority there is nothing to verify (an empty digest)."""
        if self.authority is None:
            return ("",)
        if "seal" not in document or "generation" not in document:
            return "checkpoint-unsealed"
        current = self._generation()
        try:
            generation = GenerationIdentity(**document["generation"])
            if (generation.namespace_id, generation.store_id, generation.lineage_id) != (
                current.namespace_id,
                current.store_id,
                current.lineage_id,
            ):
                return "checkpoint-not-ours"
            identity = self._checkpoint_identity(entry, document, generation)
            seal = bytes.fromhex(document["seal"])
        except (KeyError, TypeError, ValueError, AttributeError):
            return "checkpoint-not-ours"
        known = self._checkpoints_verified.get(identity.sha256)
        if known == seal:
            return (identity.sha256,)
        try:
            accepted = self._answer(
                lambda: self.authority.verify_checkpoint(identity, seal),
                self.control,
                "conversion",
            )
        except StageRefused:
            return "checkpoint-not-ours"
        if accepted is not True:
            return "checkpoint-not-ours"
        self._checkpoints_verified[identity.sha256] = seal
        return (identity.sha256,)

    def _convert(self, entry: Entry, reuse: tuple[ConversionResult, str | None] | None = None):
        """Run the converter and renderer once, recording each dispatch first; an
        uncertain dispatch is never blindly repeated. ``reuse`` is a nominated donor's
        recorded conversion of the identical source (and, under a stage authority, the
        donor checkpoint it was verified against), used in place of the converter.

        Under a stage authority every reusable result is sealed by the caller before it
        is persisted, and every persisted result is verified before it is used; the
        returned checkpoint names the digest that was sealed or verified."""
        from biblios.pipeline import (
            ConversionConfigurationError,
            ConversionRefused,
            ConversionRequest,
            ConversionResult,
            MirrorDocument,
            _bounded_text,
            _validate_result,
        )

        policy = self.pipeline.policy
        key = self._conversion_key(entry)
        row = self.derived.conn.execute(
            "SELECT document FROM checkpoints WHERE key=?", (key,)
        ).fetchone()
        document = json.loads(row[0]) if row is not None else {}
        state = document.get("state")
        if state in ("dispatched", "render-dispatched") and not self.request.retry_uncertain:
            self._report(entry.entry_id, "uncertain", "external-outcome-uncertain")
            return None
        if state == "configuration-refused":
            self._report(entry.entry_id, "pending", "configuration-invalid")
            return None
        if state == "seal-refused":
            self._report(entry.entry_id, "pending", "checkpoint-seal-refused")
            return None

        def save(**values) -> None:
            document.update(values)
            with transaction(self.derived.conn):
                self.derived.conn.execute(
                    "INSERT OR REPLACE INTO checkpoints VALUES (?, ?, ?, ?)",
                    (key, entry.entry_id, entry.cas_id, json.dumps(document, sort_keys=True)),
                )

        def sealed(**values) -> str | None:
            """Persist freshly produced content with its seal, or record the refusal
            with nothing reusable; the sealed digest, or None once refused."""
            candidate = {
                field_: value
                for field_, value in document.items()
                if field_ not in ("seal", "generation")
            }
            candidate.update(values)
            outcome = self._seal(entry, candidate)
            if isinstance(outcome, str):
                document.clear()
                save(state="seal-refused", refusal=outcome)
                self._report(entry.entry_id, "pending", "checkpoint-seal-refused")
                return None
            fields, digest_ = outcome
            document.clear()
            save(**candidate, **fields)
            return digest_ or ""

        fresh = False
        checkpoint: str | None = None
        name = entry.path.rsplit("/", 1)[-1]
        if "conversion" in document:
            verified = self._verified(entry, document)
            if isinstance(verified, str):
                self._report(entry.entry_id, "pending", verified)
                return None
            checkpoint = verified[0]
            # The converted checkpoint the conversion is bound to: this one, or the one a
            # complete checkpoint's verified canonical bytes name.
            conversion = (
                document.get("conversion_checkpoint_sha256") if "mirror" in document else checkpoint
            )
        elif reuse is not None:
            checkpoint = sealed(
                state="converted",
                conversion=asdict(reuse[0]),
                reused_body=True,
                donor_checkpoint_sha256=reuse[1],
            )
            if checkpoint is None:
                return None
            conversion = checkpoint
            fresh = True
        else:
            self._progress(entry.entry_id, "converting")
            source = self.bound.download(entry.entry_id, control=self.control)
            save(state="dispatched")
            scratch = self.bound.binding.scratch_root / "consumer"
            scratch.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=scratch) as temporary:
                conversion_request = ConversionRequest(
                    source, entry.metadata.media_type, name, Path(temporary)
                )
                try:
                    result = self.pipeline.converter(conversion_request)
                except ConversionConfigurationError as error:
                    # The code is the stable diagnostic; the exception itself reaches only
                    # this call's caller, as the outcome's cause (never recorded).
                    save(state="configuration-refused")
                    self._report(entry.entry_id, "pending", "configuration-invalid", cause=error)
                    return None
                except ConversionRefused as error:
                    # Exactly a document-failed result; the exception is only this call's
                    # cause for the entry's outcome, never persisted.
                    result = ConversionResult(
                        "unconverted",
                        "",
                        error.converter,
                        error.converter_version,
                        "document-failed",
                    )
                    self.refusal_causes[entry.entry_id] = error
                except Exception as error:
                    self._report(entry.entry_id, "uncertain", "callback-failed", cause=error)
                    return None
            _validate_result(result, policy, effect="uncertain")
            checkpoint = sealed(state="converted", conversion=asdict(result))
            conversion = checkpoint
            if checkpoint is None:
                return None
            fresh = True
        result = ConversionResult(**document["conversion"])
        if result.outcome == "unconverted":
            return result, None, self._stage_checkpoint(checkpoint, fresh)
        if "mirror" not in document or document.get("mirror_name") != name:
            # Rendered for this name; a rename before acceptance renders again from the
            # recorded (verified) conversion, never re-running the converter.
            save(state="render-dispatched")
            mirror = controlled(
                lambda: self.pipeline.renderer(result, name),
                self.control,
                "conversion",
                effect="uncertain",
            )
            if (
                type(mirror) is not MirrorDocument
                or not _bounded_text(mirror.markdown, policy.max_mirror_bytes)
                or not _bounded_text(mirror.name_heading, 255)
                or not mirror.name_heading
            ):
                raise ConsumerError("mirror-result-invalid", stage="conversion", effect="uncertain")
            # The rendering is this call's own; the conversion it renders is fresh or was
            # verified above, so ``reused`` keeps saying where the conversion came from.
            checkpoint = sealed(
                state="complete",
                mirror=asdict(mirror),
                mirror_name=name,
                conversion_checkpoint_sha256=conversion,
            )
            if checkpoint is None:
                return None
        return (
            result,
            MirrorDocument(**document["mirror"]),
            self._stage_checkpoint(checkpoint, fresh),
        )

    def _stage_checkpoint(self, checkpoint: str | None, fresh: bool) -> StageCheckpoint | None:
        if self.authority is None or not checkpoint:
            return None
        return StageCheckpoint(checkpoint, not fresh)

    def _planned(self, prepared: _Prepared, *, name_heading: str | None = None) -> _Prepared | None:
        """Plan the exact bytes, compute embedding inputs and embed (B0)."""
        from palace.index import plan_one, with_embedding_inputs

        from biblios.pipeline import _bounded_text

        entry = prepared.entry
        refused = self._refused_before(prepared)
        if refused is not None:
            self._report(entry.entry_id, "pending", refused)
            return None
        path = self.shelf.mirror_path(entry.entry_id)
        name_heading = self._project(prepared, name_heading)
        with self._index() as conn:
            plan = plan_one(
                conn=conn,
                file_kind="markdown",
                change_kind="modified",
                path=path.resolve(),
                watch_root=self.shelf.mirror_root.resolve(),
                full=True,
                source_bytes=prepared.indexed,
                log=lambda line: None,
            )
        policy = self.pipeline.policy
        if len(plan.rows) > policy.max_chunks:
            self._refuse_policy(prepared, "mirror-too-large")
        if self._snapshot_floor(plan) > self.pipeline.max_snapshot_bytes:
            # Refused before any input view, provider call, authority call or write.
            if prepared.provenance == "update-seam":
                raise ConsumerError("mirror-update-invalid:snapshot-too-large", stage="index")
            if prepared.record is not None:
                self._record_refusal(prepared.record, digest(prepared.data), "snapshot-too-large")
            raise ConsumerError("isolated:snapshot-too-large", stage="index")
        if self.representation is None and prepared.projection in ("replaced", "appended"):
            # A projected name section is checked under every representation: it must
            # chunk as its own final row, never be absorbed into the user's text.
            self._check_name(prepared, plan, name_heading)
        if self.representation is not None:
            self._check_name(prepared, plan, name_heading)
            inputs = controlled(
                lambda: tuple(self.pipeline.input_view(plan)), self.control, "embedding"
            )
            if len(inputs) != len(plan.rows) or any(
                not _bounded_text(text, policy.max_mirror_bytes) for text in inputs
            ):
                # A caller input view that does not give one bounded string per planned
                # row is refused for this entry before any provider call.
                raise ConsumerError("embedding-input-invalid", stage="embedding")
            plan = with_embedding_inputs(plan, representation=self.representation, inputs=inputs)
        reusable, source = (
            ({}, None)
            if entry.entry_id in self.request.reembed
            else self._committed(plan, prepared.record)
        )
        donated = self._donated(prepared, plan)
        missing = [
            i
            for i, (row, text) in enumerate(zip(plan.rows, plan.embed_inputs, strict=True))
            if i not in donated and (row.chunk_id, _text_digest(text)) not in reusable
        ]
        if prepared.migrating and missing:
            # Re-staging an earlier stage for its membership proof never spends: when its
            # committed rows no longer reproduce it, only an explicit re-embedding does.
            self._report(entry.entry_id, "pending", "proof-migration-needs-reembed")
            return None
        prepared.plan = plan
        prepared.pending = _Pending(
            tuple(plan.embed_inputs[i] for i in missing),
            tuple(missing),
            donated,
            reusable,
            source,
        )
        if not missing:
            self._assemble(prepared, [])
        return prepared

    def _assemble(self, prepared: _Prepared, answered: list[list[float]]) -> None:
        """Complete an entry's rows from the provider's answers to its pending inputs."""
        pending, plan = prepared.pending, prepared.plan
        assert pending is not None and plan is not None
        missing, donated, reusable, source = (
            list(pending.missing),
            pending.donated,
            pending.reusable,
            pending.source,
        )
        fresh = iter(answered)
        wanted = set(missing)
        vectors = [
            next(fresh)
            if i in wanted
            else list(donated[i])
            if i in donated
            else list(reusable[(row.chunk_id, _text_digest(text))])
            for i, (row, text) in enumerate(zip(plan.rows, plan.embed_inputs, strict=True))
        ]
        prepared.pending = None
        prepared.embedded = len(missing)
        prepared.reused = len(donated)
        prepared.reused_from = source if len(missing) + len(donated) < len(plan.rows) else None
        prepared.rows = [
            (row.chunk_id, row.body, _text_digest(text), _float32(vector))
            for row, text, vector in zip(plan.rows, plan.embed_inputs, vectors, strict=True)
        ]

    def _donated(self, prepared: _Prepared, plan) -> dict[int, tuple[float, ...]]:
        """A nominated donor's vectors, by destination row.

        Every row, the name row included, is reused when the donor's complete ordered
        input digests equal the destination's (the store's one embedding identity, which
        the donor read already required, covers provider, model, dimension and
        representation). Otherwise the body rows are reused only when the donor's rows
        without its name row equal the destination's without its own, and the name row
        is embedded afresh. Anything else reuses nothing."""
        if prepared.donor_rows is None or self.representation is None or not plan.rows:
            return {}
        wanted = [_text_digest(text) for text in plan.embed_inputs]
        donor = prepared.donor_rows
        if [inputs for inputs, _vector, _name in donor] == wanted:
            return {i: vector for i, (_inputs, vector, _name) in enumerate(donor)}
        body = [(inputs, vector) for inputs, vector, name in donor if not name]
        destination = list(range(len(plan.rows) - 1))
        if [inputs for inputs, _vector in body] != [wanted[i] for i in destination]:
            return {}
        return {i: vector for i, (_inputs, vector) in zip(destination, body, strict=True)}

    def _donor_body(self, destination: Entry):
        """The nominated donor's recorded conversion, its identity and its rows, or None
        for an ordinary miss; a changed nominee or another binding refuses."""
        from palace.index import IndexError as PalaceIndexError
        from palace.index import read_file_vectors

        from biblios.pipeline import ConversionResult, MirrorDocument

        donor = (self.request.donors or {}).get(destination.entry_id)
        if donor is None:
            return None
        if donor.namespace_id != self.bound.binding.namespace_id or donor.epoch_id != self.epoch:
            raise ConsumerError("donor-binding-mismatch", stage="index")
        with self.shelf._handle.mutex:
            entry = self.castle.read_entries([donor.entry_id]).get(donor.entry_id)
        if entry is None or entry.metadata is None:
            return None  # A genuinely removed nominee is an ordinary miss.
        if entry.cas_id != destination.cas_id:
            return None
        record = self.derived.record(donor.entry_id)
        if (
            record is None
            or entry.cas_id != donor.source_cas_id
            or record.mirror_sha256 != donor.mirror_sha256
            or record.accepted_stage_sha256 != donor.stage_sha256
        ):
            raise ConsumerError("donor-view-changed", stage="index")
        try:
            present = read_mirror(self.shelf.mirror_path(donor.entry_id), self.shelf.mirror_root)
        except MirrorInvalid:
            present = None
        if record.state != "accepted" or present is None or digest(present) != record.mirror_sha256:
            return None  # A donor whose mirror is not exactly its record is never reused.
        accepted = self.derived.conn.execute(
            "SELECT canonical FROM accepted_stages WHERE entry_id=? AND stage_sha256=?",
            (donor.entry_id, donor.stage_sha256),
        ).fetchone()
        if accepted is None:
            raise ConsumerError("donor-view-changed", stage="index")
        stage = _parse_stage(bytes(accepted[0]), donor.stage_sha256)
        try:
            served = self._served_as(donor.entry_id, stage)
        except PalaceIndexError as error:
            raise ConsumerError("donor-view-changed", stage="index") from error
        if not served:
            raise ConsumerError("donor-view-changed", stage="index")
        if entry.metadata.media_type != destination.metadata.media_type:
            return None
        row = self.derived.conn.execute(
            "SELECT document FROM checkpoints WHERE key=?", (self._conversion_key(entry),)
        ).fetchone()
        document = json.loads(row[0]) if row is not None else {}
        if document.get("state") != "complete":
            return None  # converted under another policy, or never by this core
        result = ConversionResult(**document["conversion"])
        if result.outcome != "converted":
            return None
        heading = MirrorDocument(**document["mirror"]).name_heading
        proven = None
        if self.authority is not None and self.pipeline.policy.reuse_converted_body:
            # The body is the donor's only when its accepted (caller-signed) stage names
            # exactly this checkpoint and the caller verifies the checkpoint's seal.
            signed = stage.entry.checkpoint
            verified = self._verified(entry, document)
            if (
                signed is not None
                and not isinstance(verified, str)
                and verified[0] == signed.sha256
            ):
                proven = signed.sha256
        mirror_root = self.shelf.mirror_root.resolve()
        stored = self.shelf.mirror_path(donor.entry_id).resolve().relative_to(mirror_root)
        try:
            with self._index() as conn:
                snapshot = read_file_vectors(
                    conn=conn,
                    watch_root=mirror_root,
                    path=stored.as_posix(),
                    expected_identity=self.identity,
                    max_chunks=self.pipeline.policy.max_chunks,
                    max_bytes=self.pipeline.max_snapshot_bytes,
                )
        except PalaceIndexError as error:
            raise ConsumerError("donor-view-changed", stage="index") from error
        if snapshot is None:
            raise ConsumerError("donor-view-changed", stage="index")
        rows = tuple(
            (item.input_sha256, item.vector, item.chunk.heading == heading)
            for item in snapshot.chunks
        )
        identity = DonorIdentity(
            donor.entry_id,
            donor.stage_sha256,
            record.mirror_sha256,
            _text_digest(result.body),
            str(stage.entry.content_sha256),
            proven,
        )
        return result, identity, rows

    def _check_name(self, prepared: _Prepared, plan, heading: str | None) -> None:
        name = prepared.entry.path.rsplit("/", 1)[-1]
        if prepared.projection in ("replaced", "appended"):
            rows = plan.rows
            last = rows[-1] if rows else None
            if (
                last is None
                or last.heading != heading
                or last.body.partition("\n")[2].strip() != name
                and last.body != ("# " + " ".join(name.splitlines())).rstrip()
            ):
                # The projected name section did not chunk as its own last row (for
                # instance the user's text ends inside an open block): never guessed.
                # A user section with the same heading earlier in the text is content.
                # The entry keeps whatever it had accepted.
                raise ConsumerError("name-section-unprojectable", stage="index")
            return
        heading = heading or (plan.rows[-1].heading if plan.rows else None)
        name_rows = [i for i, row in enumerate(plan.rows) if row.heading == heading]
        heading_name = " ".join(name.splitlines())
        if (
            len(name_rows) != 1
            or name_rows[0] != len(plan.rows) - 1
            or not (
                plan.rows[name_rows[0]].body.partition("\n")[2].strip() == name
                or (
                    heading == heading_name.strip()
                    and plan.rows[name_rows[0]].body == ("# " + heading_name).rstrip()
                )
            )
        ):
            self._refuse_policy(prepared, "mirror-name-section-invalid")

    def _project(self, prepared: _Prepared, heading: str | None) -> str | None:
        """Choose the bytes Palace indexes for this mirror and return their name heading.

        Never writes the mirror. When the mirror ends with exactly the name section the
        renderer recorded for the name it was rendered with, that section is the core's
        metadata: unchanged name, the mirror itself (``exact``); renamed, that section is
        swapped for the renderer's section for the current name (``replaced``), provided
        the renderer changes nothing else. Otherwise every mirror byte is the user's
        content and the current name's section is appended (``appended``)."""
        from biblios.pipeline import ConversionResult, MirrorDocument

        data = prepared.data
        name = prepared.entry.path.rsplit("/", 1)[-1]
        if prepared.indexed is not None:
            return heading
        if prepared.record is None and prepared.provenance in ("converted", "copy"):
            # A first mirror is the renderer's fresh rendering for the current name: it is
            # indexed exactly, and a rendering without its own name section is refused.
            return self._projected(prepared, data, "exact", len(data), "", heading)
        document = None
        if prepared.entry.metadata is not None:
            row = self.derived.conn.execute(
                "SELECT document FROM checkpoints WHERE key=?",
                (self._conversion_key(prepared.entry),),
            ).fetchone()
            document = None if row is None else json.loads(row[0])
        if document is not None and "mirror" in document and "conversion" in document:
            # Under a stage authority the recorded rendering is used only once the caller
            # verifies its seal; otherwise every mirror byte is the user's (appended).
            verified = self._verified(prepared.entry, document)
            if isinstance(verified, str):
                document = None
            elif self.authority is not None and (
                prepared.checkpoint is None or prepared.checkpoint.sha256 != verified[0]
            ):
                prepared.checkpoint = StageCheckpoint(verified[0], True)
        if document is not None and "mirror" in document and "conversion" in document:
            recorded = MirrorDocument(**document["mirror"])
            old = _name_section(recorded.markdown, recorded.name_heading)
            if old is not None and data.endswith(old.encode("utf-8")):
                if document.get("mirror_name") == name:
                    return self._projected(prepared, data, "exact", len(data), "", heading)
                rendered = controlled(
                    lambda: self.pipeline.renderer(
                        ConversionResult(**document["conversion"]), name
                    ),
                    self.control,
                    "conversion",
                    effect="uncertain",
                )
                new = (
                    _name_section(rendered.markdown, rendered.name_heading)
                    if type(rendered) is MirrorDocument
                    else None
                )
                if new is not None and (
                    recorded.markdown[: len(recorded.markdown) - len(old)]
                    == rendered.markdown[: len(rendered.markdown) - len(new)]
                ):
                    cut = len(data) - len(old.encode("utf-8"))
                    return self._projected(
                        prepared, data, "replaced", cut, new, rendered.name_heading
                    )
        text_end = data.decode("utf-8", errors="strict")
        gap = "" if text_end.endswith("\n\n") else "\n" if text_end.endswith("\n") else "\n\n"
        if not data:
            gap = ""
        section = gap + "## Filename\n\n" + " ".join(name.splitlines()) + "\n"
        return self._projected(prepared, data, "appended", len(data), section, "Filename")

    @staticmethod
    def _projected(
        prepared: _Prepared, data: bytes, rule: str, cut: int, suffix: str, heading
    ) -> str | None:
        prepared.projection, prepared.indexed_cut, prepared.indexed_suffix = rule, cut, suffix
        prepared.indexed = data[:cut] + suffix.encode("utf-8")
        return heading

    def _snapshot_floor(self, plan) -> int:
        """A lower bound on the bytes Palace's committed-snapshot check will count for this
        plan: every body's UTF-8 bytes, every float32 vector (``dim`` × 4) and, under a
        representation, each row's 64-byte input digest. All of these are counted, so a
        plan over the budget on this floor alone is certain to be refused at B4 and is
        refused here instead, with nothing spent.

        Partial protection only: Palace counts more (file and chunk metadata, identity)
        and publishes no exact plan-time estimate, so a plan under this floor can still be
        refused at B4, after authorization; that refusal is reported as it happens."""
        evidence = 64 if self.representation is not None else 0
        return sum(
            len(row.body.encode("utf-8")) + self.identity.dim * 4 + evidence for row in plan.rows
        )

    def _refuse_policy(self, prepared: _Prepared, code: str) -> None:
        if prepared.provenance in ("converted", "copy") and prepared.record is None:
            raise ConsumerError("mirror-name-row-invalid", stage="index")
        if prepared.provenance == "update-seam":
            raise ConsumerError("mirror-update-invalid:" + code, stage="index")
        if prepared.record is not None:
            self._record_refusal(prepared.record, digest(prepared.data), code)
        raise ConsumerError("isolated:" + code, stage="index")

    def _committed(
        self, plan, record: DerivedRecord | None
    ) -> tuple[dict[tuple[str, str], tuple[float, ...]], str | None]:
        """This file's committed vectors, keyed by chunk id and exact input digest, and
        the accepted stage they reproduce (signed into the new stage as its reuse source).

        A vector is reused only for the identical input under the identical embedder
        identity, so an unchanged chunk of an edited file is never re-embedded. Reuse is
        bound to the entry's accepted stage: the committed rows must reproduce exactly
        its signed content and manifest digests, so a row changed in the store is never
        laundered into a new signed stage. Rows that do not (tampered, or with no accepted
        stage to bind them) are not reused, and the entry this call is already indexing
        embeds those chunks; the mismatch itself never starts any work or spend.
        """
        from palace.index import IndexError as PalaceIndexError
        from palace.index import read_file_vectors

        try:
            with self._index() as conn:
                snapshot = read_file_vectors(
                    conn=conn,
                    watch_root=plan.watch_root,
                    path=plan.stored,
                    expected_identity=self.identity,
                    max_chunks=self.pipeline.policy.max_chunks,
                    max_bytes=self.pipeline.max_snapshot_bytes,
                )
        except PalaceIndexError:
            # Reuse is only an optimization: an unreadable prior file embeds afresh, and
            # the publication path that follows reports any real storage fault itself.
            return {}, None
        if snapshot is None:
            return {}, None
        accepted = self._accepted_stage(record)
        if accepted is None or accepted.entry.action != "publish":
            return {}, None
        served = _snapshot_rows(snapshot)
        if len(served) != accepted.entry.row_count or content_digest(served) != (
            accepted.entry.content_sha256,
            accepted.entry.chunk_manifest_sha256,
        ):
            return {}, None
        reusable = {
            (item.chunk.chunk_id, item.input_sha256): item.vector for item in snapshot.chunks
        }
        return reusable, accepted.stage_sha256

    def _unproven(self, record: DerivedRecord) -> bool:
        """Whether the accepted stage is a ``biblios.stage/1`` stage with rows, whose hits
        carry no membership proof and are withheld ``proof-missing``."""
        accepted = self._accepted_stage(record)
        return (
            accepted is not None
            and accepted.entry.action == "publish"
            and accepted.entry.row_count > 0
            and accepted.entry.chunk_root_sha256 is None
        )

    def _accepted_stage(self, record: DerivedRecord | None) -> Stage | None:
        """The entry's last accepted stage, verified against the record's stage digest.

        A recorded source replacement supersedes the record (its accepted identity is
        nulled so the old stage is never served for the new source), but the stage that
        was accepted stays the binding for which committed rows may be reused.
        """
        if record is None:
            return None
        if record.accepted_stage_sha256 is None and record.state != "superseded":
            return None
        row = self.derived.conn.execute(
            "SELECT stage_sha256, canonical FROM accepted_stages WHERE entry_id=?",
            (record.entry_id,),
        ).fetchone()
        if row is None or (
            record.accepted_stage_sha256 is not None and row[0] != record.accepted_stage_sha256
        ):
            return None
        try:
            return _parse_stage(bytes(row[1]), row[0])
        except (ConsumerError, ValueError, KeyError, TypeError):
            return None

    def _embed(self, prepared: _Prepared, inputs: list[str]) -> list[list[float]]:
        entry = prepared.entry
        vectors: list[list[float]] = []
        for start in range(0, len(inputs), EMBED_SLICE):
            self.control.check("embedding")
            batch = list(inputs[start : start + EMBED_SLICE])
            if start == 0:
                # The commit fence, run before any spend: a change that already landed
                # requeues the entry without a provider call.
                self._fence(prepared)
            returned = controlled(
                lambda batch=batch: self.pipeline.embedder.embed(batch),
                self.control,
                "embedding",
                effect="uncertain",
            )
            self.provider_calls += 1
            vectors.extend(self._answered(entry, batch, returned, start, len(inputs)))
        return vectors

    def _answered(
        self, entry: Entry, batch: list[str], returned, start: int, total: int
    ) -> list[list[float]]:
        """One provider answer, checked: one finite vector of the store's width per input."""
        self._progress(entry.entry_id, "indexing", start + len(batch), total)
        if len(returned) != len(batch):
            raise ConsumerError("embedding-result-invalid", stage="embedding", effect="uncertain")
        vectors = []
        for vector in returned:
            if len(vector) != self.identity.dim or any(not math.isfinite(v) for v in vector):
                raise ConsumerError(
                    "embedding-result-invalid", stage="embedding", effect="uncertain"
                )
            vectors.append(list(vector))
        return vectors

    # -- the stage -----------------------------------------------------------------------

    def _index(self):
        return _open_index(self.shelf, self.root, self.identity)

    def _stage(self, prepared: _Prepared) -> Stage:
        entry, record = prepared.entry, prepared.record
        content = manifest = root = None
        if prepared.action == "publish":
            content, manifest = content_digest(prepared.rows)
            root = chunk_root(prepared.rows)
        grant = prepared.grant
        view = self.bound.binding
        return build_stage(
            stage_id=str(uuid.uuid4()),
            namespace_id=view.namespace_id,
            epoch=self.epoch,
            store_id=self.view.store_id,
            lineage_id=self.view.lineage_id,
            database_generation=self.view.database_generation,
            observed_revision=self.revision,
            view_sha256=self._view_digest(),
            operation_id=self.request.operation_id,
            predecessor_sha256=None if record is None else record.accepted_stage_sha256,
            entry=StageEntry(
                entry.entry_id,
                entry.path,
                entry.cas_id,
                prepared.action,
                prepared.prior_mirror if record is None else record.mirror_sha256,
                None if prepared.data is None else digest(prepared.data),
                prepared.provenance,
                content,
                manifest,
                len(prepared.rows),
                prepared.projection if prepared.action == "publish" else None,
                digest(prepared.indexed)
                if prepared.action == "publish" and prepared.indexed is not None
                else None,
                root,
                prepared.checkpoint,
            ),
            derivation=self._derivation(prepared),
            embedder=EmbedderIdentity(
                f"{self.identity.convention}/{self.identity.provider}",
                self.identity.model,
                self.identity.dim,
            ),
            donor=prepared.donor,
            reused_from_sha256=prepared.reused_from,
            # For a removal this carries the removal's curation provenance (role
            # ``removed``), which the caller authenticates at B1 before any retirement.
            creation=None
            if grant is None
            else CreationIdentity(grant[1], grant[0], grant[2], entry.cas_id, grant[3], grant[4]),
            voids=tuple(void for void in self.request.voided if void.entry_id == entry.entry_id),
            resume_sha256=_text_digest(_resume_document(prepared)),
            authenticated=self.authority is not None,
        )

    def _derivation(self, prepared: _Prepared) -> DerivationIdentity:
        from biblios.pipeline import _policy_document

        policy = self.pipeline.policy
        return DerivationIdentity(
            prepared.converter,
            prepared.converter_version,
            hashlib.sha256(canonical_bytes(_policy_document(policy))).hexdigest(),
            policy.rendering,
            "palace-default" if self.representation is None else policy.implementation,
            prepared.body_sha256,
        )

    def _refusal_key(self, prepared: _Prepared) -> tuple[str, str, str]:
        """What a caller's authorize refusal is bound to: the entry, its exact mirror
        bytes and the derivation that would stage them."""
        derivation = hashlib.sha256(canonical_bytes(asdict(self._derivation(prepared)))).hexdigest()
        mirror = "" if prepared.data is None else digest(prepared.data)
        return prepared.entry.entry_id, mirror, derivation

    def _refused_before(self, prepared: _Prepared, *, named_clears: bool = True) -> str | None:
        """The code of the caller's earlier refusal of exactly this stage's inputs.

        A request naming the entry asks again: the refusal is cleared and the entry is
        prepared. Otherwise the same bytes under the same derivation are never prepared
        or embedded again; a byte change or another derivation is a different key.
        """
        if self.authority is None:
            return None
        key = self._refusal_key(prepared)
        named = prepared.entry.entry_id in (self.request.entry_ids or ())
        if (named_clears and named) or prepared.entry.entry_id in self.request.reembed:
            with transaction(self.volatile.conn):
                self.volatile.conn.execute("DELETE FROM refusals WHERE entry_id=?", (key[0],))
            return None
        row = self.volatile.conn.execute(
            "SELECT code FROM refusals WHERE entry_id=? AND mirror_sha256=? "
            "AND derivation_sha256=?",
            key,
        ).fetchone()
        return None if row is None else str(row[0])

    def _record_refused(self, prepared: _Prepared, code: str) -> None:
        key = self._refusal_key(prepared)
        with transaction(self.volatile.conn):
            # One refusal per entry: an older key can never match again once replaced.
            self.volatile.conn.execute("DELETE FROM refusals WHERE entry_id=?", (key[0],))
            self.volatile.conn.execute("INSERT INTO refusals VALUES (?, ?, ?, ?)", (*key, code))

    def _view_digest(self) -> str:
        row = self.derived.conn.execute("SELECT value FROM view WHERE key='accumulator'").fetchone()
        accumulator = row[0] if row is not None else "0"
        return hashlib.sha256(
            canonical_bytes([self.bound.binding.namespace_id, accumulator, "none"])
        ).hexdigest()

    def _publish(self, prepared: _Prepared) -> None:
        entry_id = prepared.entry.entry_id
        stage = self._stage(prepared)
        # Before B1 a foreign change discards only in-memory work.
        self._fence(prepared)
        # B1: authorize the exact in-memory stage; nothing is persisted yet.
        evidence = b""
        if self.authority is not None:
            try:
                evidence = self._answer(
                    lambda: self.authority.authorize(stage), self.control, "index"
                )
            except StageRefused as refused:
                # Nothing is persisted; the refusal is recorded against these exact
                # inputs so later calls neither re-prepare nor re-embed them.
                self._record_refused(prepared, refused.code)
                self._report(entry_id, "pending", refused.code)
                return
            _check_evidence(evidence)
        # B2: persist the stage and its rows.
        with transaction(self.volatile.conn):
            self.volatile.conn.execute(
                "INSERT INTO stages VALUES (?, ?, ?, 'authorized', ?, NULL)",
                (entry_id, stage.stage_sha256, stage.canonical, evidence),
            )
            self.volatile.conn.execute(
                "INSERT INTO stage_resume VALUES (?, ?, ?)",
                (entry_id, _resume_document(prepared), prepared.data),
            )
            self.volatile.conn.executemany(
                "INSERT INTO stage_rows VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (entry_id, position, chunk_id, body, inputs, vector)
                    for position, (chunk_id, body, inputs, vector) in enumerate(prepared.rows)
                ],
            )
        # B3: promote, with the content recomputed from the persisted rows.
        rows = self._stored_rows(entry_id)
        staged = content_digest(rows)[0] if prepared.action == "publish" else None
        if staged != stage.entry.content_sha256:
            raise ConsumerError("stage-rows-tampered", stage="index", effect="recovery-required")
        if self.authority is not None:
            verified = StageVerification(stage.stage_sha256, staged, len(rows))
            try:
                promotion = self._answer(
                    lambda: self.authority.promote(stage, verified), self.control, "index"
                )
                _check_evidence(promotion)
            except StageRefused as refused:
                self._report(entry_id, "busy", refused.code, stage_sha256=stage.stage_sha256)
                return
            with transaction(self.volatile.conn):
                self.volatile.conn.execute(
                    "UPDATE stages SET state='promoted', promotion=? WHERE entry_id=?",
                    (promotion, entry_id),
                )
        # B4: serve. A source change after B2 is a race, not a void: the stage is the
        # truthful record of the bytes it describes and is driven to acceptance; the
        # recorded change then nulls its identity and the entry is requeued.
        self._serve(prepared, stage, rows)
        # B5: accept; only then is the caller told.
        self._accept(prepared, stage, rows)

    # -- restart: a persisted stage is finished, never rebuilt ---------------------------

    def _resume(self, entry_id: str, entry: Entry | None, row) -> bool | None:
        """Drive a stage persisted by an earlier call (B2 or later) to acceptance.

        Everything is re-verified on reread (G4): the canonical bytes against the stage
        digest, the rows against its content and manifest, the mirror bytes against its
        mirror digest, and a re-plan of those bytes against the persisted chunk ids and
        input digests. A stage from another epoch is refused ``stage-fenced`` and kept until
        the caller discards it. Returns None when the stage stays pending (reported, not
        cleared), else whether the entry is now current.
        """
        sha, canonical, state, authorization, promotion = row
        stage = _parse_stage(bytes(canonical), sha)
        if state == "cancelling" or _cancellation_begun(self.derived.conn, stage.stage_sha256):
            # A caller-signed cancellation began (its link is written before the flag) and
            # has not finished: only cancel_stage continues it. Nothing here touches it.
            self._report(
                entry_id, "recovery-required", "stage-cancelling", stage_sha256=stage.stage_sha256
            )
            return None
        resume = self.volatile.conn.execute(
            "SELECT document, data FROM stage_resume WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if resume is None or _text_digest(resume[0]) != stage.resume_sha256:
            raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
        document = json.loads(resume[0])
        data = None if resume[1] is None else bytes(resume[1])
        rows = self._stored_rows(entry_id)
        planned = stage.entry
        if planned.action == "publish" and (
            data is None
            or digest(data) != planned.mirror_sha256
            or len(rows) != planned.row_count
            or content_digest(rows) != (planned.content_sha256, planned.chunk_manifest_sha256)
            or (
                planned.chunk_root_sha256 is not None
                and chunk_root(rows) != planned.chunk_root_sha256
            )
        ):
            raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
        view = self.view
        current = GenerationIdentity(
            view.namespace_id, view.store_id, view.lineage_id, view.database_generation, view.epoch
        )
        if stage.generation() != current:
            # Fenced by any change of generation (namespace, store, lineage, database
            # generation or epoch), not the epoch alone: never served or re-presented,
            # and never destroyed implicitly. The stage and its evidence stay until the
            # caller disposes of it (discard_stage, or cancel_stage once serving).
            raise ConsumerError(
                "stage-fenced",
                stage="index",
                effect="recovery-required" if self._serving_now(entry_id) else "none",
            )
        if stage.authenticated and self.authority is None:
            # A stage the caller's authority authorized is rechecked by that authority on
            # every reread (G4); a call without one never finishes, cleans up or
            # re-presents it. This only refuses: an unauthenticated stage is no evidence.
            self._report(
                entry_id, "pending", "stage-authority-required", stage_sha256=stage.stage_sha256
            )
            return None
        verified = StageVerification(stage.stage_sha256, planned.content_sha256, len(rows))
        recognized = None
        if self.authority is not None:
            # Before any effect, and whatever the volatile state flag says: the caller
            # verifies its own persisted evidence for exactly this stage.
            try:
                recognized = self._answer(
                    lambda: self.authority.recheck(
                        stage,
                        verified,
                        b"" if authorization is None else bytes(authorization),
                        None if promotion is None else bytes(promotion),
                    ),
                    self.control,
                    "index",
                )
            except StageRefused as refused:
                self._report(entry_id, "pending", refused.code, stage_sha256=stage.stage_sha256)
                return None
            if recognized not in ("pending", "accepted"):
                raise ConsumerError("evidence-invalid", stage="index")
        record = self.derived.record(entry_id)
        first_removal = planned.action == "remove" and stage.predecessor_sha256 is None
        if first_removal:
            # No record ever existed, so its absence proves nothing: only B5 (which marks
            # the stage accepted in the transaction that drops its grants) claims it.
            claims_accepted = record is None and state == "accepted"
        elif planned.action == "remove":
            claims_accepted = record is None
        else:
            claims_accepted = (
                record is not None and record.accepted_stage_sha256 == stage.stage_sha256
            )
        outcome = {"publish": SEARCHABLE, "remove": "removed"}.get(planned.action, "unconverted")
        if self.authority is not None and claims_accepted and recognized == "pending":
            # B5 recorded this stage but the caller never received (or never recorded) the
            # result. Derived equality authorizes nothing: the exact result is re-presented
            # only when the caller's own promotion for exactly this stage was rechecked
            # above and the store actually serves exactly the staged rows. Nothing is
            # written or cleaned up; the stage stays until the caller acknowledges it.
            from palace.index import IndexError as PalaceIndexError

            try:
                served = promotion is not None and self._served_as(entry_id, stage)
            except PalaceIndexError as error:
                # The store could not be read as proof: disputed, with Palace's own refusal
                # as the cause; nothing is cleaned up or re-presented.
                self._report(
                    entry_id,
                    "recovery-required",
                    "stage-acceptance-disputed",
                    stage_sha256=stage.stage_sha256,
                    cause=error,
                )
                return None
            if served:
                self.accepted.append(AcceptedStage(entry_id, stage.stage_sha256))
                self._report(
                    entry_id,
                    outcome,
                    "acceptance-unacknowledged",
                    stage_sha256=stage.stage_sha256,
                )
                return None
        if self.authority is not None and claims_accepted != (recognized == "accepted"):
            # Biblios's record and the caller's signed head disagree and the store does not
            # prove completion: nothing is cleaned up, served or accepted.
            self._report(
                entry_id,
                "recovery-required",
                "stage-acceptance-disputed",
                stage_sha256=stage.stage_sha256,
            )
            return None
        if claims_accepted:
            # The caller's signed head records this exact stage (or no caller holds one):
            # finish the cleanup only, with no effect and no new signature.
            self._finish_cleanup(entry_id, planned, document)
            self._report(entry_id, outcome, stage_sha256=stage.stage_sha256)
            if entry is None:
                # An entry removed after this stage: its removal continues in this call.
                return planned.action == "remove"
            return entry.cas_id == planned.source_cas_id
        stale = (
            None
            if planned.action == "remove"
            else "entry-removed"
            if entry is None
            else "source-replaced"
            if entry.cas_id != planned.source_cas_id
            else "entry-renamed"
            if entry.path != planned.path
            else None
        )
        if stale is not None:
            # The entry left the live tree, its source was replaced (a new ingestion), or
            # it was renamed (the staged name section names the old name) after this
            # stage was authorized: current state overrides that authorization.
            # Nothing is promoted, signed, embedded, written or served, whatever the stage's
            # phase marker says; the stage and its evidence stay until the caller's own
            # authenticated decision disposes of it. Reads withhold the entry meanwhile
            # (not live, a different source, or an odd served_seq).
            self._report(
                entry_id,
                "recovery-required" if self._serving_now(entry_id) else "pending",
                stale,
                stage_sha256=stage.stage_sha256,
            )
            return None
        if planned.action == "remove":
            source = replace(_gone(entry_id), cas_id=planned.source_cas_id)
        else:
            source = replace(entry, cas_id=planned.source_cas_id, path=planned.path)
        prepared = _Prepared(
            source,
            record,
            planned.action,
            planned.mirror_provenance,
            data,
            rows=rows,
            converter=stage.derivation.converter,
            converter_version=stage.derivation.converter_version,
            conversion=document["conversion"],
            body_sha256=stage.derivation.body_sha256,
            grant=None if document["grant"] is None else tuple(document["grant"]),
            expected_exchange=document["expected_exchange"],
            preexisting=document["preexisting"],
        )
        if planned.action == "remove" and record is None:
            prepared.prior_mirror = planned.prior_mirror_sha256
        if planned.action == "publish":
            cut, suffix = document["indexed"]
            prepared.projection, prepared.indexed_cut, prepared.indexed_suffix = (
                planned.projection,
                cut,
                suffix,
            )
            prepared.indexed = data[:cut] + suffix.encode("utf-8")
            if digest(prepared.indexed) != planned.indexed_sha256:
                raise ConsumerError("stage-tampered", stage="index", effect="recovery-required")
        path = self.shelf.mirror_path(entry_id)
        if planned.action == "publish":
            prepared.plan = self._replan(prepared, rows)
            try:
                present = read_mirror(path, self.shelf.mirror_root)
            except MirrorInvalid:
                present = None
            prepared.mirror_written = present is not None and digest(present) == digest(data)
        if self.authority is not None and promotion is None:
            # Not yet promoted: the caller signs only the stage it has just rechecked.
            try:
                promotion = self._answer(
                    lambda: self.authority.promote(stage, verified), self.control, "index"
                )
                _check_evidence(promotion)
            except StageRefused as refused:
                self._report(entry_id, "busy", refused.code, stage_sha256=stage.stage_sha256)
                return None
            with transaction(self.volatile.conn):
                self.volatile.conn.execute(
                    "UPDATE stages SET state='promoted', promotion=? WHERE entry_id=?",
                    (promotion, entry_id),
                )
        self._serve(prepared, stage, rows)
        self._accept(prepared, stage, rows)
        return True  # the fence above admits only a removal or the current source

    def _serving_now(self, entry_id: str) -> bool:
        return _serving(self.derived.conn, self.volatile.conn, entry_id)

    def _served_as(self, entry_id: str, stage: Stage) -> bool:
        """Whether the store serves exactly this stage: B4 finished (even served_seq) and
        Palace's committed file and rows equal the staged mirror digest and rows (a
        removal or unconverted stage: no committed file). Read-only. An absent file is an
        answer; a Palace refusal to read the committed file (identity, malformed or over
        budget) propagates to the caller, which keeps it as the cause."""
        from palace.index import read_file_vectors

        row = self.derived.conn.execute(
            "SELECT served_seq FROM entries WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if row is not None and int(row[0]) % 2 == 1:
            return False
        planned = stage.entry
        mirror_root = self.shelf.mirror_root.resolve()
        stored = self.shelf.mirror_path(entry_id).resolve().relative_to(mirror_root).as_posix()
        with self._index() as conn:
            snapshot = read_file_vectors(
                conn=conn,
                watch_root=mirror_root,
                path=stored,
                expected_identity=self.identity,
                max_chunks=self.pipeline.policy.max_chunks,
                max_bytes=self.pipeline.max_snapshot_bytes,
            )
        if planned.action == "remove" and stage.predecessor_sha256 is not None:
            return snapshot is None
        if planned.action == "remove":
            # A removal before any acceptance served only the retirement of its first
            # mirror: those exact bytes are no longer at the live path.
            try:
                present = read_mirror(self.shelf.mirror_path(entry_id), self.shelf.mirror_root)
            except MirrorInvalid:
                return False
            return snapshot is None and (
                present is None or digest(present) != planned.prior_mirror_sha256
            )
        if planned.action != "publish":
            return snapshot is None
        if snapshot is None or snapshot.file.file_hash != planned.indexed_sha256:
            return False
        served = _snapshot_rows(snapshot)
        return len(served) == planned.row_count and content_digest(served) == (
            planned.content_sha256,
            planned.chunk_manifest_sha256,
        )

    def _finish_cleanup(self, entry_id: str, planned: StageEntry, document: dict) -> None:
        with transaction(self.volatile.conn):
            _drop_stage(self.volatile.conn, entry_id)
            if document["grant"] is not None:
                # A publication consumes its creation grant only; a removal recorded since
                # (the same source's row, role ``removed``) stays for that removal.
                self.volatile.conn.execute(
                    "DELETE FROM grants WHERE entry_id=? AND cas_id=?"
                    + ("" if planned.action == "remove" else " AND role!='removed'"),
                    (entry_id, planned.source_cas_id),
                )

    def _replan(self, prepared: _Prepared, rows) -> FilePlan:
        """Plan the persisted mirror bytes again; the plan must reproduce the persisted
        chunk ids, bodies and exact input digests, or the stage is not resumed."""
        from palace.index import plan_one, with_embedding_inputs

        from biblios.pipeline import _bounded_text

        policy = self.pipeline.policy

        path = self.shelf.mirror_path(prepared.entry.entry_id)
        with self._index() as conn:
            plan = plan_one(
                conn=conn,
                file_kind="markdown",
                change_kind="modified",
                path=path.resolve(),
                watch_root=self.shelf.mirror_root.resolve(),
                full=True,
                source_bytes=prepared.indexed,
                log=lambda line: None,
            )
        if self.representation is not None:
            inputs = controlled(
                lambda: tuple(self.pipeline.input_view(plan)), self.control, "embedding"
            )
            if len(inputs) != len(plan.rows) or any(
                not _bounded_text(text, policy.max_mirror_bytes) for text in inputs
            ):
                # A caller input view that does not give one bounded string per planned
                # row is refused for this entry before any provider call.
                raise ConsumerError("embedding-input-invalid", stage="embedding")
            plan = with_embedding_inputs(plan, representation=self.representation, inputs=inputs)
        replanned = [
            (row.chunk_id, row.body, _text_digest(text))
            for row, text in zip(plan.rows, plan.embed_inputs, strict=True)
        ]
        if replanned != [(chunk_id, body, inputs) for chunk_id, body, inputs, _ in rows]:
            raise ConsumerError("stage-replan-mismatch", stage="index", effect="recovery-required")
        return plan

    def _accept_nodes(self, stage: Stage, rows: list[tuple[str, str, str, bytes]]) -> None:
        """Write the accepted stage's membership nodes, inside the acceptance transaction.

        Built from exactly the accepted rows (O(n)) and written only when their root is the
        one the stage signs; anything else raises and rolls the acceptance back. A
        ``biblios.stage/1`` stage signs no root and gets no nodes."""
        from biblios.membership import root_of, tree_nodes

        signed = stage.entry.chunk_root_sha256
        if signed is None or not rows:
            if signed is not None:
                raise ConsumerError(
                    "stage-rows-tampered", stage="index", effect="recovery-required"
                )
            return
        nodes = tree_nodes(chunk_leaves(rows))
        if root_of(nodes, len(rows)).hex() != signed:
            raise ConsumerError("stage-rows-tampered", stage="index", effect="recovery-required")
        self.derived.conn.executemany(
            "INSERT INTO accepted_nodes VALUES (?, ?, ?, ?)",
            [(stage.entry.entry_id, lo, hi, value) for (lo, hi), value in nodes.items()],
        )

    def _stored_rows(self, entry_id: str) -> list[tuple[str, str, str, bytes]]:
        return [
            (chunk_id, body, inputs, bytes(vector))
            for chunk_id, body, inputs, vector in self.volatile.conn.execute(
                "SELECT chunk_id, body, input_sha256, vector FROM stage_rows "
                "WHERE entry_id=? ORDER BY position",
                (entry_id,),
            )
        ]

    def _fence(self, prepared: _Prepared) -> None:
        """Before the serialized commit: has another writer changed this entry?"""
        with self.shelf._handle.mutex:
            revision = self.castle.observe_revision()
            if revision == self.revision:
                return
            fresh = self.castle.read_entries([prepared.entry.entry_id]).get(prepared.entry.entry_id)
        self.revision = revision
        if prepared.action == "remove":
            if fresh is None:
                return
        elif (
            fresh is not None
            and fresh.cas_id == prepared.entry.cas_id
            and fresh.path == prepared.entry.path
        ):
            return
        # The bytes or the name the stage embeds changed (a rename changes the name row
        # and the staged path). Nothing was persisted for this stage yet: requeue, never
        # publish it.
        raise ConsumerError("stale-view", stage="index")

    def _serve(self, prepared: _Prepared, stage: Stage, rows) -> None:
        from palace.index import FileVectorLimits, commit_plan
        from palace.index.core import delete_path

        mark_accepted_ever(self.root)
        entry = prepared.entry
        path = self.shelf.mirror_path(entry.entry_id)
        record = prepared.record
        with transaction(self.volatile.conn):
            self.volatile.conn.execute(
                "UPDATE stages SET state='serving' WHERE entry_id=?", (entry.entry_id,)
            )
        self._served_seq(entry.entry_id, odd=True)
        provenance = Provenance(
            self.root,
            "removed" if prepared.action == "remove" else "update",
            None if record is None else record.source_cas_id,
            None if record is None else record.mirror_sha256,
            self.request.operation_id,
            self.revision,
        )
        with self._index() as conn:
            if prepared.action == "publish":
                if prepared.mirror_written:
                    pass
                elif prepared.expected_exchange is not None:
                    reason = "update" if prepared.provenance == "update-seam" else "source-replaced"
                    write_mirror(
                        path,
                        prepared.data,
                        expected=prepared.expected_exchange,
                        provenance=Provenance(
                            self.root,
                            reason,
                            provenance.source_cas_id,
                            provenance.recorded_mirror_sha256,
                            self.request.operation_id,
                            self.revision,
                        ),
                    )
                elif prepared.provenance == "update-seam":
                    write_mirror(path, prepared.data, expected=None, provenance=provenance)
                if prepared.provenance == "import":
                    # An import stage's rows are Palace's committed rows, verified when it
                    # was prepared: B4 writes nothing, after proving they are still served.
                    if self._legacy_rows(prepared.entry, prepared.data) != list(rows):
                        raise ConsumerError(
                            "import-rows-changed", stage="index", effect="recovery-required"
                        )
                else:
                    commit_plan(
                        conn=conn,
                        plan=prepared.plan,
                        embeddings=[struct.unpack(f"<{len(v) // 4}f", v) for *_, v in rows],
                        identity=self.identity,
                        snapshot_limits=FileVectorLimits(
                            max_chunks=self.pipeline.policy.max_chunks,
                            max_bytes=self.pipeline.max_snapshot_bytes,
                        ),
                        log=lambda line: None,
                    )
            else:
                delete_path(
                    conn=conn,
                    watch_root=str(self.shelf.mirror_root.resolve()),
                    path_str=entry.entry_id + ".md",
                )
                # A conversion failure never moves user bytes. A removed entry's mirror
                # is retired only with the authorized curation provenance of its removal.
                if prepared.action == "remove" and prepared.grant is not None:
                    first = record is None
                    retire_mirror(
                        path,
                        provenance=Provenance(
                            self.root,
                            "removed",
                            entry.cas_id if first else provenance.source_cas_id,
                            prepared.prior_mirror if first else provenance.recorded_mirror_sha256,
                            self.request.operation_id,
                            self.revision,
                        ),
                        # Before any acceptance only the core's own exact bytes move.
                        expected=prepared.prior_mirror if first else None,
                    )

    def _served_seq(self, entry_id: str, *, odd: bool) -> None:
        with transaction(self.derived.conn):
            row = self.derived.conn.execute(
                "SELECT served_seq FROM entries WHERE entry_id=?", (entry_id,)
            ).fetchone()
            if row is None:
                return
            current = int(row[0])
            wanted = current + 1 if (current % 2 == 0) == odd else current
            self.derived.conn.execute(
                "UPDATE entries SET served_seq=? WHERE entry_id=?", (wanted, entry_id)
            )

    def _accepted_stat(self, prepared: _Prepared, path: Path) -> StatIdentity | None:
        """The stat identity that describes exactly the accepted mirror bytes, or None.

        It is the identity of the descriptor the bytes were read from when the path still
        has it; otherwise the mirror is read once more and kept only when it hashes to the
        accepted digest. An edit made while the stage was prepared leaves None, so the next
        pass hashes the file, and the entry is marked so that pass happens.
        """
        if prepared.action != "publish" or prepared.data is None:
            return None
        if prepared.read_stat is not None and StatIdentity.of(path) == prepared.read_stat:
            return prepared.read_stat
        try:
            read = read_mirror_stat(path, self.shelf.mirror_root)
        except MirrorInvalid:
            read = None
        if read is not None and digest(read[0]) == digest(prepared.data):
            return read[1]
        self.volatile.mark_dirty([prepared.entry.entry_id], source="changed-while-staged")
        return None

    def _accept(self, prepared: _Prepared, stage: Stage, rows) -> None:
        entry = prepared.entry
        entry_id = entry.entry_id
        path = self.shelf.mirror_path(entry_id)
        old = prepared.record
        with transaction(self.derived.conn):
            if prepared.action == "remove":
                self._account(old, None)
                for table in (
                    "entries",
                    "accepted_stages",
                    "accepted_chunks",
                    "accepted_nodes",
                    "checkpoints",
                ):
                    self.derived.conn.execute(f"DELETE FROM {table} WHERE entry_id=?", (entry_id,))
            else:
                row = self.derived.conn.execute(
                    "SELECT served_seq FROM entries WHERE entry_id=?", (entry_id,)
                ).fetchone()
                seq = 0 if row is None else int(row[0]) + int(row[0]) % 2
                new = DerivedRecord(
                    entry_id=entry_id,
                    source_cas_id=entry.cas_id,
                    conversion=prepared.conversion,
                    converter=prepared.converter,
                    converter_version=prepared.converter_version,
                    mirror_sha256="" if prepared.data is None else digest(prepared.data),
                    indexed_sha256=None if prepared.indexed is None else digest(prepared.indexed),
                    indexed_name=entry.path.rsplit("/", 1)[-1]
                    if prepared.action == "publish"
                    else None,
                    stat=self._accepted_stat(prepared, path),
                    recorded_ns=time.time_ns(),
                    state="accepted",
                    refusal=None,
                    accepted_stage_sha256=stage.stage_sha256,
                    served_seq=seq,
                )
                self._account(old, new)
                self.derived.put(new)
                self.derived.conn.execute(
                    "INSERT OR REPLACE INTO accepted_stages VALUES (?, ?, ?)",
                    (entry_id, stage.stage_sha256, stage.canonical),
                )
                for table in ("accepted_chunks", "accepted_nodes"):
                    self.derived.conn.execute(f"DELETE FROM {table} WHERE entry_id=?", (entry_id,))
                self.derived.conn.executemany(
                    "INSERT INTO accepted_chunks VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            entry_id,
                            chunk_id,
                            position,
                            _text_digest(body),
                            inputs,
                            hashlib.sha256(vector).hexdigest(),
                        )
                        for position, (chunk_id, body, inputs, vector) in enumerate(rows)
                    ],
                )
                self._accept_nodes(stage, rows)
        with transaction(self.volatile.conn):
            if self.authority is None:
                _drop_stage(self.volatile.conn, entry_id)
            else:
                # The stage and its evidence stay until the caller's signed head records it
                # (recheck answers ``accepted``), so a result lost after this point is
                # re-presented exactly rather than disputed or re-derived.
                self.volatile.conn.execute(
                    "UPDATE stages SET state='accepted' WHERE entry_id=?", (entry_id,)
                )
            if prepared.grant is not None:
                self.volatile.conn.execute(
                    "DELETE FROM grants WHERE entry_id=? AND cas_id=?"
                    + ("" if prepared.action == "remove" else " AND role!='removed'"),
                    (
                        entry_id,
                        old.source_cas_id
                        if prepared.action == "remove" and old is not None
                        else entry.cas_id,
                    ),
                )
        self.accepted.append(AcceptedStage(entry_id, stage.stage_sha256))
        if prepared.action == "remove":
            self._report(entry_id, "removed", stage_sha256=stage.stage_sha256)
        elif prepared.action == "unconverted":
            self._report(
                entry_id,
                "unconverted",
                prepared.conversion.removeprefix("unconverted:"),
                stage_sha256=stage.stage_sha256,
                cause=self.refusal_causes.get(entry_id),
            )
        else:
            self._report(
                entry_id,
                SEARCHABLE,
                stage_sha256=stage.stage_sha256,
                embedded=prepared.embedded,
                reused=prepared.reused,
            )

    def _account(self, old: DerivedRecord | None, new: DerivedRecord | None) -> None:
        """Maintain the per-state counters and the served-view accumulator in the same
        transaction that changes an entry's served identity."""
        account(self.derived, old, new)


def _answer(call, control: CallControl, stage):
    """A stage-authority callback under the usual fence, except that StageRefused is the
    caller's answer and passes through; any other exception stays contained."""
    refused: list[StageRefused] = []

    def guarded():
        try:
            return call()
        except StageRefused as error:
            refused.append(StageRefused(error.code))
            return None

    value = controlled(guarded, control, stage)
    if refused:
        raise refused[0]
    return value


def _open_index(shelf, root: Path, identity: EmbeddingIdentity):
    from palace.index.schema import assert_identity, init_db

    db = shelf.chunks_db
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, isolation_level=None)
    try:
        import sqlite_vec

        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        init_db(conn, store=root)
        assert_identity(conn, identity)
    except BaseException:
        conn.close()
        raise
    return closing(conn)


def _gone(entry_id: str) -> Entry:
    from biblios.entries import Entry

    return Entry(entry_id, "file", "", None, None, None)
