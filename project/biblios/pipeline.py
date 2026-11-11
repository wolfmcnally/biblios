"""Contained, durable conversion outcomes for the explicitly supplied consumer lane.

Dispatch records describe uncertainty at a callback boundary, not a second
namespace journal or an exactly-once external execution/billing guarantee.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import struct
import tempfile
import uuid
from collections.abc import Callable, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from castle.namespace import canonical_bytes
from palace.index import (
    EmbeddingRepresentation,
    FilePlan,
    FileVectorLimits,
    FileVectorSnapshot,
    commit_plan,
    plan_one,
    read_file_vectors,
    with_embedding_inputs,
)
from palace.index.config import EmbeddingIdentity
from palace.index.embedder import Embedder
from palace.index.embedder_config import identity_for, load_embedder_config
from palace.index.schema import assert_identity, init_db

from biblios.control import (
    CallControl,
    ConsumerError,
    Effect,
    Observation,
    ShelfView,
    Stage,
    controlled,
    observe,
    save_checkpoint,
)
from biblios.entries import CONVERTED, UNCONVERTED_PREFIX
from biblios.mirrors import (
    EDIT_CONVERTER,
    POLICY_CLASSES,
    MirrorInvalid,
    Provenance,
    classify,
    clear_refusal,
    lane_for,
    read_mirror,
    record_refusal,
    retire_mirror,
    validate_mirror,
    write_mirror,
)
from biblios.mirrors import digest as _bytes_digest
from biblios.progress import (
    EMBED_SLICE,
    EntryProgress,
    index_status,
    indexed_files,
    membership,
    write_watermark,
)

if TYPE_CHECKING:
    from biblios.consumer import BoundShelf
    from biblios.entries import Entry


_MAX_CHECKPOINT_BYTES = 512 * 1024 * 1024


def _save_checkpoint(file: Path, document: dict, *, stage: Stage) -> None:
    save_checkpoint(file, document, stage=stage, max_bytes=_MAX_CHECKPOINT_BYTES)


@dataclass(frozen=True, slots=True)
class ReuseDonor:
    """One explicitly nominated same-shelf committed view; never authorization."""

    entry_id: str
    namespace_id: str
    epoch_id: str
    source_cas_id: str
    mirror_sha256: str
    view_sha256: str
    conversion_operation_id: str


@dataclass(frozen=True, slots=True)
class DerivationStatus:
    operation_id: str
    entry_id: str
    state: str
    source_cas_id: str
    reused_body_rows: int = 0
    embedded_rows: int = 0
    replayed: bool = False
    observation: Observation | None = None


def literal_inputs(plan: FilePlan) -> tuple[str, ...]:
    """Explicit opt-in view: each exact chunk body, including the separate name row."""
    return tuple(row.body for row in plan.rows)


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    source: bytes
    media_type: str
    display_name: str
    scratch_root: Path


@dataclass(frozen=True, slots=True)
class ConversionResult:
    outcome: Literal["converted", "unconverted"]
    body: str
    converter: str
    converter_version: str
    reason: Literal["unsupported", "document-failed", "deferred"] | None = None


class ConversionConfigurationError(Exception):
    """A supplied converter can distinguish configuration from document refusal.

    Its message and traceback are never included in a consumer diagnostic.
    """


@dataclass(frozen=True, slots=True)
class MirrorDocument:
    markdown: str
    name_heading: str


@dataclass(frozen=True, slots=True)
class DerivationPolicy:
    """Caller-declared implementation identity, never inferred from equal dimensions."""

    converter: str
    converter_version: str
    rendering: str
    convention: str
    implementation: str
    chunker: str
    name_policy: str
    max_body_bytes: int = 4 * 1024 * 1024
    max_mirror_bytes: int = 8 * 1024 * 1024
    max_chunks: int = 16384
    reuse_converted_body: bool = False
    required_header_fields: tuple[str, ...] = ()
    """Front-matter keys a consumer's own mirror format requires; none are imposed."""

    def validate(self) -> None:
        labels = (
            self.converter,
            self.converter_version,
            self.rendering,
            self.convention,
            self.implementation,
            self.chunker,
            self.name_policy,
        )
        if (
            any(not _bounded_text(value, 256) or not value for value in labels)
            or type(self.max_body_bytes) is not int
            or not 0 < self.max_body_bytes <= _MAX_CHECKPOINT_BYTES
            or type(self.max_mirror_bytes) is not int
            or not 0 < self.max_mirror_bytes <= _MAX_CHECKPOINT_BYTES
            or type(self.max_chunks) is not int
            or not 0 < self.max_chunks <= 16384
            or type(self.reuse_converted_body) is not bool
            or type(self.required_header_fields) is not tuple
            or not all(_bounded_text(key, 256) and key for key in self.required_header_fields)
        ):
            raise ConsumerError("policy-invalid", stage="conversion")


@dataclass(frozen=True, slots=True)
class ConversionStatus:
    operation_id: str
    entry_id: str
    source_cas_id: str
    state: Literal["converted", "unconverted"]
    result: ConversionResult
    mirror: MirrorDocument | None
    mirror_sha256: str
    replayed: bool


def render_escaped(result: ConversionResult, display_name: str) -> MirrorDocument:
    """An explicit optional policy: quote structure, preserve text, one finite name section."""
    if len(display_name.encode()) > 255 or any(char in display_name for char in "\r\n\x00"):
        raise ConsumerError("name-invalid", stage="conversion")
    body = "\n".join("| " + line for line in result.body.split("\n"))
    return MirrorDocument(body + "\n\n## Filename\n\n" + display_name + "\n", "Filename")


def _record_path(bound: BoundShelf, operation_id: str) -> Path:
    try:
        parsed = uuid.UUID(operation_id)
        if parsed.version != 4 or str(parsed) != operation_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ConsumerError("operation-invalid", stage="conversion") from None
    root = bound.binding.palace_root / "consumer-records"
    file = root / (operation_id + ".json")
    if file.is_symlink() or not file.resolve().is_relative_to(bound.binding.palace_root.resolve()):
        raise ConsumerError("artifact-outside-domain", stage="conversion")
    return file


def _policy_document(policy: DerivationPolicy) -> dict:
    """The policy as a JSON- and canonical-encodable mapping (sequences as lists)."""
    document = asdict(policy)
    document["required_header_fields"] = list(policy.required_header_fields)
    return document


def _bounded_text(value: object, limit: int) -> bool:
    if type(value) is not str:
        return False
    try:
        return len(value.encode("utf-8")) <= limit
    except UnicodeEncodeError:
        return False


def _validate_result(
    result: ConversionResult, policy: DerivationPolicy, *, effect: Effect = "none"
) -> None:
    if (
        type(result) is not ConversionResult
        or not _bounded_text(result.body, policy.max_body_bytes)
        or (result.converter, result.converter_version)
        != (policy.converter, policy.converter_version)
        or result.outcome not in {"converted", "unconverted"}
        or (result.outcome == "converted" and result.reason is not None)
        or (
            result.outcome == "unconverted"
            and (result.body or result.reason not in {"unsupported", "document-failed", "deferred"})
        )
    ):
        raise ConsumerError("conversion-result-invalid", stage="conversion", effect=effect)


def _status(record: dict, replayed: bool, policy: DerivationPolicy) -> ConversionStatus:
    result = ConversionResult(**record["conversion"])
    _validate_result(result, policy, effect="recovery-required")
    mirror = MirrorDocument(**record["mirror"]) if record["mirror"] is not None else None
    if mirror is not None:
        if (
            len(mirror.markdown.encode()) > policy.max_mirror_bytes
            or hashlib.sha256(mirror.markdown.encode()).hexdigest() != record["mirror_sha256"]
        ):
            raise ConsumerError(
                "checkpoint-corrupt", stage="conversion", effect="recovery-required"
            )
    return ConversionStatus(
        record["operation_id"],
        record["entry_id"],
        record["source_cas_id"],
        result.outcome,
        result,
        mirror,
        record["mirror_sha256"],
        replayed,
    )


def convert(
    bound: BoundShelf,
    entry_id: str,
    *,
    operation_id: str,
    converter: Callable[[ConversionRequest], ConversionResult],
    renderer: Callable[[ConversionResult, str], MirrorDocument],
    policy: DerivationPolicy,
    retry_uncertain: bool = False,
    control: CallControl = CallControl(),
) -> ConversionStatus:
    """Persist completed conversion/render results; never blindly repeat a dispatch.

    The caller owns the supplied callbacks. They must respect the provided scratch
    root; Biblios confines its own files but cannot sandbox arbitrary Python code.
    """
    with bound._working(), bound._shelf._handle.mutex:
        view = bound._admit(write=True)
        control.check("conversion")
        policy.validate()
        if not callable(converter) or not callable(renderer) or type(retry_uncertain) is not bool:
            raise ConsumerError("provider-required", stage="conversion")
        entry = bound.resolve(entry_id, control=control)
        if entry.kind != "file" or entry.metadata is None:
            raise ConsumerError("metadata-required", stage="conversion")
        display_name = entry.path.rsplit("/", 1)[-1]
        fingerprint = hashlib.sha256(
            canonical_bytes(
                {
                    "namespace": view.namespace_id,
                    "store": view.store_id,
                    "epoch": view.epoch,
                    "entry": entry_id,
                    "source": entry.cas_id,
                    "name": display_name,
                    "media_type": entry.metadata.media_type,
                    "policy": _policy_document(policy),
                }
            )
        ).hexdigest()
        file = _record_path(bound, operation_id)
        record = _load_checkpoint(file, stage="conversion")
        if record is not None:
            if (
                record.get("schema") != "biblios.consumer-conversion/1"
                or record.get("operation_id") != operation_id
            ):
                raise ConsumerError(
                    "checkpoint-corrupt", stage="conversion", effect="recovery-required"
                )
            if (
                record.get("fingerprint") != fingerprint
                or record.get("entry_id") != entry_id
                or record.get("source_cas_id") != entry.cas_id
            ):
                raise ConsumerError("operation-conflict", stage="conversion")
            if record.get("state") == "configuration-refused":
                raise ConsumerError("configuration-invalid", stage="conversion")
            if record.get("state") == "complete":
                try:
                    return _status(record, True, policy)
                except ConsumerError:
                    raise
                except Exception:
                    raise ConsumerError(
                        "checkpoint-corrupt", stage="conversion", effect="recovery-required"
                    ) from None
            if record.get("state") not in {"dispatched", "render-dispatched", "converted"}:
                raise ConsumerError(
                    "checkpoint-corrupt", stage="conversion", effect="recovery-required"
                )
            if record["state"] in {"dispatched", "render-dispatched"} and not retry_uncertain:
                raise ConsumerError(
                    "external-outcome-uncertain", stage="conversion", effect="uncertain"
                )
        else:
            record = {
                "schema": "biblios.consumer-conversion/1",
                "operation_id": operation_id,
                "fingerprint": fingerprint,
                "entry_id": entry_id,
                "source_cas_id": entry.cas_id,
                "policy": _policy_document(policy),
                "media_type": entry.metadata.media_type,
            }
        if "conversion" not in record:
            source = bound.download(entry_id, control=control)
            file.parent.mkdir(parents=True, exist_ok=True)
            scratch = bound.binding.scratch_root / "consumer"
            scratch.mkdir(parents=True, exist_ok=True)
            record["state"] = "dispatched"
            _save_checkpoint(file, record, stage="conversion")
            with tempfile.TemporaryDirectory(prefix=operation_id + ".", dir=scratch) as temporary:
                request = ConversionRequest(
                    source, entry.metadata.media_type, display_name, Path(temporary)
                )

                # Keep a declared configuration refusal distinct while discarding its text.
                control.check("conversion")
                try:
                    result = converter(request)
                except ConversionConfigurationError:
                    record["state"] = "configuration-refused"
                    _save_checkpoint(file, record, stage="conversion")
                    raise ConsumerError("configuration-invalid", stage="conversion") from None
                except Exception:
                    raise ConsumerError(
                        "callback-failed", stage="conversion", effect="uncertain"
                    ) from None
                control.check("conversion", effect="uncertain")
                _validate_result(result, policy, effect="uncertain")
                record.update(state="converted", conversion=asdict(result))
                _save_checkpoint(file, record, stage="conversion")
        result = ConversionResult(**record["conversion"])
        _validate_result(result, policy)
        mirror = None
        if result.outcome == "converted":
            record["state"] = "render-dispatched"
            _save_checkpoint(file, record, stage="conversion")
            mirror = controlled(
                lambda: renderer(result, display_name), control, "conversion", effect="uncertain"
            )
            if (
                type(mirror) is not MirrorDocument
                or not _bounded_text(mirror.markdown, policy.max_mirror_bytes)
                or not _bounded_text(mirror.name_heading, 255)
                or not mirror.name_heading
            ):
                raise ConsumerError("mirror-result-invalid", stage="conversion", effect="uncertain")
        record.update(
            state="complete",
            mirror=asdict(mirror) if mirror is not None else None,
            mirror_sha256=hashlib.sha256(mirror.markdown.encode()).hexdigest()
            if mirror is not None
            else "",
        )
        _save_checkpoint(file, record, stage="conversion")
        return _status(record, False, policy)


def _load_checkpoint(file: Path, *, stage: Stage = "index") -> dict | None:
    try:
        if file.is_symlink():
            raise ValueError
        if not file.exists():
            return None
        if file.stat().st_size > _MAX_CHECKPOINT_BYTES:
            raise ValueError
        with file.open("rb") as stream:
            payload = stream.read(_MAX_CHECKPOINT_BYTES + 1)
        if len(payload) > _MAX_CHECKPOINT_BYTES:
            raise ValueError
        document = json.loads(payload.decode("utf-8"))
        if type(document) is not dict:
            raise ValueError
        return document
    except Exception:
        raise ConsumerError("checkpoint-corrupt", stage=stage, effect="recovery-required") from None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _float32(vector: Sequence[float]) -> tuple[float, ...]:
    return struct.unpack(f"{len(vector)}f", struct.pack(f"{len(vector)}f", *vector))


@dataclass(slots=True)
class _PreparedDerivation:
    before: ShelfView
    admitted: ShelfView | None
    entry: Entry
    conversion: ConversionStatus
    checkpoint: Path
    record: dict
    path: Path
    plan: FilePlan | None
    vectors: list
    data: bytes | None
    staged_new: bool = False
    publication_started: bool = False
    lane: Literal["convert", "reindex", "update"] = "convert"
    copied_by: str | None = None
    expected: str | None = None
    """Update lane: the digest the caller expects at the path when publishing."""
    previous: bytes | None = None
    """Update lane: the bytes publication displaces, for a rollback that never overwrites."""


def _provenance(bound, entry, reason: str, operation_id: str, revision: int) -> Provenance:
    """What the entry's Castle record says about the mirror about to be retained."""
    metadata = entry.metadata if entry is not None else None
    recorded = metadata is not None and metadata.converted
    return Provenance(
        bound.binding.palace_root,
        reason,
        metadata.source_cas_id if recorded else None,
        metadata.mirror_sha256 if recorded else None,
        operation_id,
        revision,
    )


class ConsumerPipeline:
    """Explicit conversion, input view and model composition over public Palace plans.

    The caller declares the selector's representation before using this lane.
    Existing incompatible derived state is refused, never relabeled or rebuilt
    implicitly. Injected providers remain caller-owned.
    """

    def __init__(
        self,
        *,
        converter: Callable[[ConversionRequest], ConversionResult],
        renderer: Callable[[ConversionResult, str], MirrorDocument],
        input_view: Callable[[FilePlan], Sequence[str]],
        policy: DerivationPolicy,
        embedder: Embedder,
        progress: Callable[[EntryProgress], None] | None = None,
        max_snapshot_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        policy.validate()
        if type(max_snapshot_bytes) is not int or max_snapshot_bytes <= 0:
            raise ConsumerError("snapshot-budget-invalid", stage="index")
        if embedder is None or not all(
            callable(value) for value in (converter, renderer, input_view)
        ):
            raise ConsumerError("provider-required", stage="index")
        self.converter, self.renderer, self.input_view = converter, renderer, input_view
        self.policy, self.embedder, self.progress = policy, embedder, progress
        self.max_snapshot_bytes = max_snapshot_bytes

    @property
    def representation(self) -> EmbeddingRepresentation:
        policy = self.policy
        return EmbeddingRepresentation(
            policy.convention, policy.implementation, policy.chunker, policy.name_policy
        )

    def _identity(self, bound: BoundShelf) -> EmbeddingIdentity:
        config = load_embedder_config(bound.binding.palace_root)
        if config.representation != self.representation:
            raise ConsumerError("representation-mismatch", stage="index")
        return identity_for(config)

    def _snapshot(
        self, bound: BoundShelf, connection, entry_id: str, identity
    ) -> FileVectorSnapshot | None:
        return read_file_vectors(
            conn=connection,
            watch_root=str(bound.binding.mirror_root.resolve()),
            path=entry_id + ".md",
            expected_identity=identity,
            max_bytes=self.max_snapshot_bytes,
            max_chunks=self.policy.max_chunks,
        )

    def donor(self, bound: BoundShelf, entry_id: str, *, operation_id: str) -> ReuseDonor:
        """Capture an actual completed view; retaining it does not reserve the donor."""
        try:
            return self._capture_donor(bound, entry_id, operation_id)
        except ConsumerError:
            raise
        except Exception:
            raise ConsumerError("read-failed", stage="read") from None

    def _capture_donor(self, bound: BoundShelf, entry_id: str, operation_id: str) -> ReuseDonor:
        with bound._writer("consumer donor capture"), bound._shelf._handle.mutex:
            bound._admit(write=True)
            view = bound.view
            entry = bound.resolve(entry_id)
            record = _load_checkpoint(
                _record_path(bound, operation_id).with_suffix(".derived.json")
            )
            state = classify(bound._shelf, entry, lane=lane_for(bound.binding.palace_root))
            if (
                record is None
                or record.get("state") != "complete"
                or record.get("entry_id") != entry_id
                or not entry.conversion_current
                or state is None
                or not state.searchable
            ):
                raise ConsumerError("donor-not-complete", stage="index")
            identity = self._identity(bound)
            with closing(sqlite3.connect(bound._shelf.chunks_db, isolation_level=None)) as conn:
                # The public reader loads no extension; bootstrap only on the writer path.
                import sqlite_vec

                conn.enable_load_extension(True)
                sqlite_vec.load(conn)
                conn.enable_load_extension(False)
                snapshot = self._snapshot(bound, conn, entry_id, identity)
                if snapshot is None or snapshot.view_sha256 != record.get("view_sha256"):
                    raise ConsumerError("donor-view-changed", stage="index")
            bound._admit()
            return ReuseDonor(
                entry_id,
                view.namespace_id,
                view.epoch,
                entry.cas_id,
                entry.metadata.mirror_sha256,
                snapshot.view_sha256,
                operation_id,
            )

    def status(self, bound: BoundShelf, *, operation_id: str) -> DerivationStatus:
        """Observe persisted local work without dispatching or repairing providers."""

        def read():
            record = _load_checkpoint(
                _record_path(bound, operation_id).with_suffix(".derived.json")
            )
            if record is None:
                conversion = _load_checkpoint(_record_path(bound, operation_id))
                if conversion is None:
                    return DerivationStatus(operation_id, "", "unknown", "")
                return DerivationStatus(
                    operation_id,
                    conversion["entry_id"],
                    "uncertain"
                    if conversion.get("state") in {"dispatched", "render-dispatched"}
                    else "conversion",
                    conversion["source_cas_id"],
                )
            return self._result(bound, record, replayed=True)

        return bound._read(read, CallControl())

    def finish(
        self,
        bound: BoundShelf,
        *,
        operation_id: str,
        control: CallControl = CallControl(),
        observer: Callable[[Observation], None] | None = None,
    ) -> Observation:
        """Remove obsolete index members and validate a complete current watermark.

        This performs no conversion or embedding, and refuses pending entries.
        The separate writer checkpoint() prepares actual read-only mounting.
        """
        from palace.index.core import delete_path

        with bound._writer("consumer completion", control=control), bound._shelf._handle.mutex:
            before = bound._admit(write=True)
            _record_path(bound, operation_id)
            control.check("index")
            try:
                identity = self._identity(bound)
                with closing(sqlite3.connect(bound._shelf.chunks_db, isolation_level=None)) as conn:
                    init_db(conn, store=bound.binding.palace_root)
                    assert_identity(conn, identity)
                    from biblios.mirrors import classify_shelf

                    live = {entry.entry_id for entry in bound._shelf.files()}
                    for entry_id in sorted(set(indexed_files(bound._shelf)) - live):
                        delete_path(
                            conn=conn,
                            watch_root=str(bound.binding.mirror_root.resolve()),
                            path_str=entry_id + ".md",
                        )
                        # A removed entry's mirror is retired, never deleted.
                        # Castle's live view no longer holds its record: the source is
                        # recorded as unknown, never inferred.
                        retire_mirror(
                            bound._shelf.mirror_path(entry_id),
                            provenance=_provenance(
                                bound, None, "removed", operation_id, before.revision
                            ),
                        )
                    states = classify_shelf(bound._shelf)
                    expected, withheld = membership(bound._shelf, states)
                    indexed = {
                        key: value
                        for key, value in indexed_files(bound._shelf).items()
                        if key not in withheld
                    }
                    if index_status(bound._shelf, states).pending or indexed != expected:
                        raise ConsumerError(
                            "index-incomplete", stage="index", effect="derived-committed"
                        )
                    write_watermark(bound._shelf, before.revision)
                result = Observation(
                    operation_id,
                    "index",
                    "derived-committed",
                    bound.view,
                    before.revision,
                    tuple(sorted(expected)),
                )
                control.check("index", effect="derived-committed")
                observe(observer, result, control)
                return result
            except ConsumerError:
                raise
            except Exception:
                raise ConsumerError(
                    "completion-failed", stage="index", effect="recovery-required"
                ) from None

    def _result(self, bound: BoundShelf, record: dict, *, replayed: bool) -> DerivationStatus:
        complete = record["state"] == "complete"
        observation = None
        if complete:
            observation = Observation(
                record["operation_id"],
                "index",
                "derived-committed",
                ShelfView(**record["committed_view"]),
                record["revision_before"],
                (record["entry_id"],),
                (record["source_cas_id"],),
                record["mirror_sha256"],
                replayed,
            )
        return DerivationStatus(
            record["operation_id"],
            record["entry_id"],
            record["outcome"]
            if complete
            else ("uncertain" if record["state"] == "embedding-dispatched" else "indexing"),
            record["source_cas_id"],
            record.get("reused", 0),
            record.get("embedded", 0),
            replayed,
            observation,
        )

    def _donor_body(self, bound: BoundShelf, conn, donor, destination, identity):
        if donor is None:
            return None
        view = bound._admit()
        if donor.namespace_id != view.namespace_id or donor.epoch_id != view.epoch:
            raise ConsumerError("donor-binding-mismatch", stage="index")
        entry = next((item for item in bound.files() if item.entry_id == donor.entry_id), None)
        if entry is None:
            return None  # A genuinely removed nominee is an ordinary miss.
        if not entry.conversion_current or entry.cas_id != destination.cas_id:
            return None
        if (
            entry.metadata.mirror_sha256 != donor.mirror_sha256
            or entry.cas_id != donor.source_cas_id
        ):
            raise ConsumerError("donor-view-changed", stage="index")
        state = classify(bound._shelf, entry, lane=lane_for(bound.binding.palace_root))
        if state is None or not state.searchable:
            return None  # A donor whose mirror is not exactly its record is never reused.
        snapshot = self._snapshot(bound, conn, entry.entry_id, identity)
        if snapshot is None or snapshot.view_sha256 != donor.view_sha256:
            raise ConsumerError("donor-view-changed", stage="index")
        record = _load_checkpoint(_record_path(bound, donor.conversion_operation_id))
        if record is None or record.get("state") != "complete":
            return None
        if record.get("policy") != _policy_document(self.policy):
            return None
        if record.get("media_type") != destination.metadata.media_type:
            return None
        converted = _status(record, True, self.policy)
        if converted.state != "converted" or converted.source_cas_id != destination.cas_id:
            return None
        return converted.result, snapshot, converted.mirror.name_heading

    def derive(
        self,
        bound: BoundShelf,
        entry_id: str,
        *,
        operation_id: str,
        donor: ReuseDonor | None = None,
        retry_uncertain: bool = False,
        copied_by: str | None = None,
        control: CallControl = CallControl(),
        observer: Callable[[Observation], None] | None = None,
    ) -> DerivationStatus:
        """Index an entry: re-index its current mirror, or convert data that has none.

        A converted entry whose mirror exists is re-indexed from the mirror's current
        bytes; the mirror is never written. Conversion runs for new source data, for a
        copy named by ``copied_by`` (the committed Castle duplicate operation that
        created it), and for replaced source data, which is a new ingestion: the
        previous mirror is exchanged out to a retained name, never overwritten.
        """
        return self._derive(
            bound,
            entry_id,
            operation_id=operation_id,
            donor=donor,
            retry_uncertain=retry_uncertain,
            copied_by=copied_by,
            update=None,
            control=control,
            observer=observer,
        )

    def update_mirror(
        self,
        bound: BoundShelf,
        entry_id: str,
        *,
        operation_id: str,
        markdown: str,
        expected_sha256: str | None,
        retry_uncertain: bool = False,
        control: CallControl = CallControl(),
        observer: Callable[[Observation], None] | None = None,
    ) -> DerivationStatus:
        """Replace an entry's mirror with user-supplied text and re-index it.

        The text is validated before anything is written. ``expected_sha256`` is the
        digest the caller saw at the mirror path, or None when it saw no mirror; a
        different intervening edit refuses ``mirror-changed-concurrently`` and is kept.
        """
        if type(markdown) is not str or not (
            expected_sha256 is None or _bounded_text(expected_sha256, 64)
        ):
            raise ConsumerError("request-invalid", stage="index")
        return self._derive(
            bound,
            entry_id,
            operation_id=operation_id,
            donor=None,
            retry_uncertain=retry_uncertain,
            copied_by=None,
            update=(markdown, expected_sha256),
            control=control,
            observer=observer,
        )

    def _derive(
        self,
        bound: BoundShelf,
        entry_id: str,
        *,
        operation_id: str,
        donor: ReuseDonor | None,
        retry_uncertain: bool,
        copied_by: str | None,
        update: tuple[str, str | None] | None,
        control: CallControl,
        observer: Callable[[Observation], None] | None,
    ) -> DerivationStatus:
        with bound._writer("consumer derivation", control=control):
            prepared = None
            try:
                with bound._shelf._handle.mutex:
                    bound._admit(write=True)
                    control.check("index")
                    identity = self._identity(bound)
                    db = bound._shelf.chunks_db
                    db.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(db, isolation_level=None)
                    try:
                        init_db(conn, store=bound.binding.palace_root)
                        assert_identity(conn, identity)
                    except BaseException:
                        conn.close()
                        raise
                with closing(conn):
                    with bound._shelf._handle.mutex:
                        prepared = self._prepare(
                            bound,
                            conn,
                            identity,
                            entry_id,
                            operation_id,
                            donor,
                            retry_uncertain,
                            control,
                            observer,
                            copied_by,
                            update,
                        )
                    if isinstance(prepared, DerivationStatus):
                        return prepared
                    self._embed(bound, identity, prepared, control)
                    with bound._shelf._handle.mutex:
                        self._fence(bound, prepared, complete=True)
                        return self._publish(bound, conn, identity, prepared, control, observer)
            except BaseException as error:
                if isinstance(prepared, _PreparedDerivation):
                    with bound._shelf._handle.mutex:
                        if prepared.staged_new and not prepared.publication_started:
                            self._discard_staging(
                                bound,
                                prepared.path,
                                prepared.data,
                                error,
                                prepared.record["operation_id"],
                                prepared.before.revision,
                            )
                if isinstance(error, ConsumerError) or not isinstance(error, Exception):
                    raise
                raise ConsumerError(
                    "derivation-failed", stage="index", effect="recovery-required"
                ) from None

    @staticmethod
    def _discard_staging(
        bound, path: Path, data: bytes, error: BaseException, operation_id: str, revision: int
    ) -> None:
        """Move a mirror Biblios created for this operation and never published to a
        retained name, so the next attempt converts afresh. Nothing is deleted: a writer
        that edited or holds the new file open keeps its bytes in the retained version."""
        from biblios.mirrors import discard_staged

        try:
            discard_staged(
                path, data, _provenance(bound, None, "staging-discarded", operation_id, revision)
            )
        except (OSError, ConsumerError):
            error.add_note("Owned unadmitted mirror cleanup failed; recovery required.")

    def _copy_evidence(self, bound: BoundShelf, entry_id: str, copied_by: str | None) -> None:
        """Require a newly created copy's unused receipt (see :func:`copy_evidence`)."""
        from biblios.mirrors import copy_evidence

        if copied_by is None:
            raise ConsumerError("mirror-missing", stage="index")
        if entry_id not in copy_evidence(bound._shelf, copied_by):
            raise ConsumerError("materialize-evidence-stale", stage="index")

    def _user_owned(
        self, bound: BoundShelf, entry, data: bytes, operation_id: str, *, prefix: str = ""
    ):
        """Validate user-owned mirror bytes and present them as a conversion status."""
        lane = lane_for(bound.binding.palace_root)
        try:
            text = validate_mirror(
                data,
                lane=lane,
                max_bytes=self.policy.max_mirror_bytes,
                required_header_fields=self.policy.required_header_fields,
            )
        except MirrorInvalid as error:
            if not prefix and error.code in POLICY_CLASSES:
                # A direct edit refused only by this pipeline's policy is recorded for
                # exactly these bytes, so status isolates the entry instead of leaving it
                # pending (which would hold back the shelf's completion).
                record_refusal(
                    bound.binding.palace_root, entry.entry_id, _bytes_digest(data), error.code
                )
            raise ConsumerError(prefix + error.code, stage="index") from None
        result = ConversionResult("converted", "", EDIT_CONVERTER, "")
        return ConversionStatus(
            operation_id,
            entry.entry_id,
            entry.cas_id,
            "converted",
            result,
            MirrorDocument(text, ""),
            _bytes_digest(data),
            False,
        )

    def _prepare(
        self,
        bound,
        conn,
        identity,
        entry_id,
        operation_id,
        donor,
        retry_uncertain,
        control,
        observer,
        copied_by=None,
        update=None,
    ):
        before = bound._admit(write=True)
        entry = bound.resolve(entry_id)
        if entry.kind != "file" or entry.metadata is None:
            raise ConsumerError("metadata-required", stage="index")
        suffix = ".update.json" if update is not None else ".derived.json"
        file = _record_path(bound, operation_id).with_suffix(suffix)
        record = _load_checkpoint(file)
        replayed = record is not None and record.get("state") == "complete"
        path = bound._shelf.mirror_path(entry_id)
        try:
            existing = read_mirror(path, bound.binding.mirror_root)
        except MirrorInvalid as error:
            raise ConsumerError(error.code, stage="index") from None
        current = entry.metadata.converted and entry.metadata.current_for(entry.cas_id)
        expected = previous = nominated = None
        replacing = False
        if update is not None:
            if not entry.metadata.current_for(entry.cas_id) and not entry.metadata.pending:
                raise ConsumerError("mirror-source-changed", stage="index")
            data = update[0].encode("utf-8")
            expected, previous = update[1], existing
            if (
                existing is not None
                and _bytes_digest(existing) != expected
                and _bytes_digest(existing) != _bytes_digest(data)
            ):
                raise ConsumerError("mirror-changed-concurrently", stage="index")
            if existing is None and expected is not None:
                raise ConsumerError("mirror-changed-concurrently", stage="index")
            conversion = self._user_owned(
                bound, entry, data, operation_id, prefix="mirror-update-invalid:"
            )
            lane = "update"
        elif (
            current
            and existing is not None
            and not (
                # This operation's own conversion already wrote these exact bytes:
                # a replay or resume stays the conversion it recorded.
                record is not None
                and record.get("lane") == "convert"
                and record.get("mirror_sha256") == _bytes_digest(existing)
            )
        ):
            conversion = self._user_owned(bound, entry, existing, operation_id)
            lane = "reindex"
        elif current and existing is None and copied_by is None:
            raise ConsumerError("mirror-missing", stage="index")
        else:
            # Replacing a file is a new ingestion: its previous mirror moves to a retained
            # name when this conversion is written, and is never overwritten or deleted.
            replacing = not current and existing is not None and entry.metadata.converted
            if current and existing is None:
                # Creating a mirror for a converted entry needs the copy's own receipt;
                # replaying this operation over its own written bytes does not.
                self._copy_evidence(bound, entry_id, copied_by)
            nominated = (
                self._donor_body(bound, conn, donor, entry, identity) if record is None else None
            )
            conversion = convert(
                bound,
                entry_id,
                operation_id=operation_id,
                converter=(lambda request: nominated[0])
                if nominated and self.policy.reuse_converted_body
                else self.converter,
                renderer=self.renderer,
                policy=self.policy,
                retry_uncertain=retry_uncertain,
                control=control,
            )
            lane = "convert"
            if (
                existing is not None
                and not replacing
                and (
                    conversion.state != "converted"
                    or existing != conversion.mirror.markdown.encode()
                )
            ):
                # A file at a never-converted entry's path is a user's mirror, unless it
                # is exactly this operation's own staged conversion (resume).
                raise ConsumerError("mirror-user-owned", stage="index")
            if replacing and conversion.state == "converted":
                # Published by exchange: the previous mirror is retained, and an edit
                # made meanwhile is put back and refuses.
                expected, previous = _bytes_digest(existing), existing
        if bound._admit() != before:
            raise ConsumerError("stale-view", stage="index", effect="uncertain")
        fingerprint = _digest(
            json.dumps(
                {
                    "namespace": before.namespace_id,
                    "epoch": before.epoch,
                    "store": before.store_id,
                    "entry": entry_id,
                    "source": entry.cas_id,
                    "name": entry.path.rsplit("/", 1)[-1],
                    "media_type": entry.metadata.media_type,
                    "policy": _policy_document(self.policy),
                    "identity": asdict(identity),
                    "mirror": conversion.mirror_sha256,
                    "lane": lane,
                    "expected": expected if lane == "update" else None,
                    "copied_by": copied_by,
                },
                sort_keys=True,
            )
        )
        if record is not None and record.get("fingerprint") != fingerprint:
            raise ConsumerError("operation-conflict", stage="index")
        if record is not None and record.get("state") == "superseded":
            raise ConsumerError(
                "mirror-changed-concurrently", stage="index", effect="derived-committed"
            )
        if replayed:
            actual = self._snapshot(bound, conn, entry_id, identity)
            valid = (
                (
                    actual is not None
                    and actual.view_sha256 == record.get("view_sha256")
                    and not path.is_symlink()
                    and path.is_file()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == conversion.mirror_sha256
                )
                if conversion.state == "converted"
                else actual is None
            )
            if not valid or not entry.metadata.current_for(entry.cas_id):
                raise ConsumerError(
                    "completed-view-changed", stage="index", effect="recovery-required"
                )
            result = self._result(bound, record, replayed=True)
            control.check("index", effect="derived-committed")
            observe(observer, result.observation, control)
            return result
        if record is None:
            record = {
                "schema": "biblios.consumer-update/1"
                if lane == "update"
                else "biblios.consumer-derived/1",
                "operation_id": operation_id,
                "entry_id": entry_id,
                "source_cas_id": entry.cas_id,
                "fingerprint": fingerprint,
                "revision_before": before.revision,
                "mirror_sha256": conversion.mirror_sha256,
                "state": "prepared",
                "outcome": "indexed" if conversion.state == "converted" else "unconverted",
                "reused": 0,
                "embedded": 0,
                "lane": lane,
            }
            _save_checkpoint(file, record, stage="index")
        elif record["state"] == "embedding-dispatched" and not retry_uncertain:
            raise ConsumerError("external-outcome-uncertain", stage="embedding", effect="uncertain")

        staged_new = False
        if conversion.state == "converted":
            data = conversion.mirror.markdown.encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.with_suffix(".partial").is_symlink():
                raise ConsumerError("artifact-outside-domain", stage="index")
            if existing is None:
                # Palace plans a regular nominated file. A path with no mirror gets this
                # operation's bytes by a no-replace create; nothing existing is replaced.
                write_mirror(
                    path,
                    data,
                    expected=None,
                    provenance=_provenance(bound, None, "staging", operation_id, before.revision),
                )
            # Exactly this conversion's own unpublished bytes are owned staging.
            staged_new = lane == "convert" and (existing is None or existing == data)
            try:
                plan = plan_one(
                    conn=conn,
                    file_kind="markdown",
                    change_kind="modified",
                    path=path.resolve(),
                    watch_root=bound.binding.mirror_root.resolve(),
                    full=True,
                    source_bytes=data,
                    log=lambda line: None,
                )
                name = entry.path.rsplit("/", 1)[-1]
                heading = conversion.mirror.name_heading or (
                    plan.rows[-1].heading if plan.rows else None
                )
                name_rows = [i for i, row in enumerate(plan.rows) if row.heading == heading]
                heading_name = " ".join(name.splitlines())
                if len(plan.rows) > self.policy.max_chunks:
                    self._refuse(bound, lane, entry_id, conversion, "mirror-too-large")
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
                    self._refuse(bound, lane, entry_id, conversion, "mirror-name-section-invalid")
                inputs = controlled(lambda: tuple(self.input_view(plan)), control, "embedding")
                if any(not _bounded_text(text, self.policy.max_mirror_bytes) for text in inputs):
                    raise ConsumerError("embedding-input-invalid", stage="embedding")
                plan = with_embedding_inputs(
                    plan, representation=self.representation, inputs=inputs
                )
                hashes = [_digest(text) for text in plan.embed_inputs]
                if "inputs" in record and record["inputs"] != hashes:
                    raise ConsumerError("operation-conflict", stage="embedding")
                vectors = record.get("vectors", [None] * len(plan.rows))
                if len(vectors) != len(plan.rows):
                    raise ConsumerError(
                        "checkpoint-corrupt", stage="embedding", effect="recovery-required"
                    )
                if nominated is not None:
                    source_rows = [
                        row for row in nominated[1].chunks if row.chunk.heading != nominated[2]
                    ]
                    destination_rows = [i for i in range(len(plan.rows)) if i != name_rows[0]]
                    if [row.input_sha256 for row in source_rows] == [
                        hashes[i] for i in destination_rows
                    ] and len(source_rows) == len(destination_rows):
                        for i, row in zip(destination_rows, source_rows, strict=True):
                            vectors[i] = list(row.vector)
                        record["reused"] = len(source_rows)
                record.update(inputs=hashes, vectors=vectors)
                _save_checkpoint(file, record, stage="embedding")
            except BaseException as error:
                if staged_new:
                    self._discard_staging(bound, path, data, error, operation_id, before.revision)
                raise
        else:
            plan, vectors, data = None, [], None
        prepared = _PreparedDerivation(
            before,
            None,
            entry,
            conversion,
            file,
            record,
            path,
            plan,
            vectors,
            data,
            staged_new,
            lane=lane,
            expected=expected,
            previous=previous,
            copied_by=copied_by,
        )
        prepared.admitted = bound.view
        return prepared

    def _refuse(self, bound, lane, entry_id, conversion, code) -> None:
        """Refuse policy-dependent mirror bytes; persist the class for user-owned bytes.

        Converter output keeps its existing refusal; an update refuses at the seam.
        """
        if lane == "convert":
            raise ConsumerError("mirror-name-row-invalid", stage="index")
        if lane == "update":
            raise ConsumerError("mirror-update-invalid:" + code, stage="index")
        record_refusal(bound.binding.palace_root, entry_id, conversion.mirror_sha256, code)
        raise ConsumerError(code, stage="index")

    def _fence(
        self, bound: BoundShelf, work: _PreparedDerivation, *, complete=False, effect="uncertain"
    ) -> None:
        try:
            current = bound.view if complete and work.admitted is not None else bound._admit()
        except ConsumerError as error:
            raise ConsumerError(error.code, stage="index", effect=effect) from None
        expected = work.admitted if complete and work.admitted is not None else work.before
        if current != expected:
            raise ConsumerError("stale-view", stage="index", effect=effect)

    def _embed(self, bound: BoundShelf, identity, work: _PreparedDerivation, control) -> None:
        plan, vectors, record = work.plan, work.vectors, work.record
        if plan is None:
            return
        for start in range(0, len(plan.rows), EMBED_SLICE):
            with bound._shelf._handle.mutex:
                effect = "uncertain" if record["state"] == "embedding-dispatched" else "none"
                self._fence(bound, work, effect=effect)
                control.check("embedding", effect=effect)
                selected = [
                    i
                    for i in range(start, min(start + EMBED_SLICE, len(plan.rows)))
                    if vectors[i] is None
                ]
                if not selected:
                    continue
                record["state"] = "embedding-dispatched"
                _save_checkpoint(work.checkpoint, record, stage="embedding")
            returned = controlled(
                lambda: self.embedder.embed([plan.embed_inputs[i] for i in selected]),
                control,
                "embedding",
                effect="uncertain",
            )
            with bound._shelf._handle.mutex:
                self._fence(bound, work)
                control.check("embedding", effect="uncertain")
                if len(returned) != len(selected):
                    raise ConsumerError(
                        "embedding-result-invalid", stage="embedding", effect="uncertain"
                    )
                for i, vector in zip(selected, returned, strict=True):
                    if len(vector) != identity.dim or any(
                        not math.isfinite(value) for value in vector
                    ):
                        raise ConsumerError(
                            "embedding-result-invalid", stage="embedding", effect="uncertain"
                        )
                    vectors[i] = list(_float32(vector))
                record.update(state="vectors-ready", embedded=record["embedded"] + len(selected))
                _save_checkpoint(work.checkpoint, record, stage="embedding")
            if self.progress is not None:
                controlled(
                    lambda: self.progress(
                        EntryProgress(
                            work.before.shelf_id,
                            work.entry.entry_id,
                            work.entry.path,
                            "indexing",
                            sum(vector is not None for vector in vectors),
                            len(vectors),
                        )
                    ),
                    control,
                    "index",
                    effect="uncertain",
                )

    @staticmethod
    def _same_snapshot(snapshot: FileVectorSnapshot | None, work: _PreparedDerivation) -> bool:
        return (
            snapshot is not None
            and snapshot.file.file_hash == work.conversion.mirror_sha256
            and [row.input_sha256 for row in snapshot.chunks] == work.record["inputs"]
            and [row.vector for row in snapshot.chunks]
            == [tuple(vector) for vector in work.vectors]
        )

    @staticmethod
    def _mirror_digest(path: Path) -> str | None:
        if path.is_symlink() or not path.is_file():
            return None
        return _bytes_digest(path.read_bytes())

    @staticmethod
    def _supersede(file: Path, record: dict, *, effect: Effect) -> None:
        """The mirror changed under this operation; it ends without recording anything."""
        record["state"] = "superseded"
        _save_checkpoint(file, record, stage="index")
        raise ConsumerError("mirror-changed-concurrently", stage="index", effect=effect)

    def _publish(self, bound, conn, identity, work: _PreparedDerivation, control, observer):
        entry, conversion, record = work.entry, work.conversion, work.record
        path, file, vectors = work.path, work.checkpoint, work.vectors
        entry_id = entry.entry_id
        work.publication_started = True
        if conversion.state == "converted":
            plan = work.plan
            existing = self._snapshot(bound, conn, entry_id, identity)
            if work.lane == "update" or work.previous is not None:
                # An explicit update, or the new ingestion of a replaced file, writes an
                # existing mirror only by exchange over exactly the expected bytes; the
                # displaced bytes are retained.
                write_mirror(
                    path,
                    work.data,
                    expected=work.expected,
                    provenance=_provenance(
                        bound,
                        entry,
                        "update" if work.lane == "update" else "source-replaced",
                        record["operation_id"],
                        work.before.revision,
                    ),
                )
            elif self._mirror_digest(path) != conversion.mirror_sha256:
                self._supersede(file, record, effect="none")
            try:
                if not self._same_snapshot(existing, work):
                    commit_plan(
                        conn=conn,
                        plan=plan,
                        embeddings=vectors,
                        identity=identity,
                        snapshot_limits=FileVectorLimits(
                            max_bytes=self.max_snapshot_bytes, max_chunks=self.policy.max_chunks
                        ),
                        log=lambda line: None,
                    )
            except Exception as error:
                # A general Palace error alone does not establish rollback. Restore the
                # displaced mirror only after checking the actual idle database view, and
                # only over exactly the bytes this update wrote.
                if (
                    not conn.in_transaction
                    and work.previous is not None
                    and self._snapshot(bound, conn, entry_id, identity) == existing
                ):
                    try:
                        # The displaced bytes are this operation's own unrecorded write.
                        write_mirror(
                            path,
                            work.previous,
                            expected=conversion.mirror_sha256,
                            provenance=Provenance(
                                bound.binding.palace_root,
                                "rollback",
                                None,
                                None,
                                record["operation_id"],
                                work.before.revision,
                            ),
                        )
                    except ConsumerError:
                        error.add_note("A later edit was kept; the update was not rolled back.")
                raise
            actual = self._snapshot(bound, conn, entry_id, identity)
            record["view_sha256"] = actual.view_sha256
            record["state"] = "index-committed"
            _save_checkpoint(file, record, stage="index")
            if self._mirror_digest(path) != conversion.mirror_sha256:
                # Indexed bytes are no longer the mirror: record nothing about them.
                self._supersede(file, record, effect="derived-committed")
        else:
            from palace.index.core import delete_path

            retire_mirror(
                path,
                provenance=_provenance(
                    bound, entry, "unconverted", record["operation_id"], work.before.revision
                ),
            )
            delete_path(
                conn=conn,
                watch_root=str(bound.binding.mirror_root.resolve()),
                path_str=entry_id + ".md",
            )

        fresh = bound.resolve(entry_id)
        if fresh.cas_id != entry.cas_id or fresh.path != entry.path or fresh.metadata is None:
            raise ConsumerError("stale-view", stage="index", effect="derived-committed")
        user_owned = work.lane in {"reindex", "update"}
        unchanged = user_owned and fresh.metadata.mirror_sha256 == conversion.mirror_sha256
        metadata = fresh.metadata.with_conversion(
            converter=fresh.metadata.converter if unchanged else conversion.result.converter,
            converter_version=fresh.metadata.converter_version
            if unchanged
            else conversion.result.converter_version,
            mirror_sha256=conversion.mirror_sha256,
            source_cas_id=entry.cas_id,
            conversion=CONVERTED
            if conversion.state == "converted"
            else UNCONVERTED_PREFIX + conversion.result.reason,
        )
        if metadata != fresh.metadata or work.copied_by is not None:
            # A copy's materialization always records, spending its duplicate receipt.
            bound._shelf.record_conversion(fresh, metadata)
        clear_refusal(bound.binding.palace_root, entry_id)
        from biblios.mirrors import classify_shelf

        states = classify_shelf(bound._shelf)
        status = index_status(bound._shelf, states)
        expected, withheld = membership(bound._shelf, states)
        indexed = {k: v for k, v in indexed_files(bound._shelf).items() if k not in withheld}
        if status.pending == 0 and indexed == expected:
            write_watermark(bound._shelf, bound._shelf.revision)
        record.update(state="complete", committed_view=asdict(bound.view))
        _save_checkpoint(file, record, stage="index")
        result = self._result(bound, record, replayed=False)
        control.check("index", effect="derived-committed")
        observe(observer, result.observation, control)
        return result
