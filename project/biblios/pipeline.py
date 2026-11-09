"""Contained, durable conversion outcomes for the explicitly supplied consumer lane.

Dispatch records describe uncertainty at a callback boundary, not a second
namespace journal or an exactly-once external execution/billing guarantee.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
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
from palace.writer_lock import writer_lock

from biblios.control import (
    CallControl,
    ConsumerError,
    Effect,
    Observation,
    ShelfView,
    controlled,
    observe,
    save_checkpoint,
)
from biblios.entries import CONVERTED, UNCONVERTED_PREFIX
from biblios.progress import (
    EMBED_SLICE,
    EntryProgress,
    index_status,
    indexed_files,
    write_watermark,
)

if TYPE_CHECKING:
    from biblios.consumer import BoundShelf


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
            or not 0 < self.max_body_bytes <= 4 * 1024 * 1024
            or type(self.max_mirror_bytes) is not int
            or not 0 < self.max_mirror_bytes <= 8 * 1024 * 1024
            or type(self.max_chunks) is not int
            or not 0 < self.max_chunks <= 16384
            or type(self.reuse_converted_body) is not bool
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
                    "policy": asdict(policy),
                }
            )
        ).hexdigest()
        file = _record_path(bound, operation_id)
        try:
            record = json.loads(file.read_text()) if file.exists() else None
            if record is not None and type(record) is not dict:
                raise ValueError
        except Exception:
            raise ConsumerError(
                "checkpoint-corrupt", stage="conversion", effect="recovery-required"
            ) from None
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
                "policy": asdict(policy),
                "media_type": entry.metadata.media_type,
            }
        if "conversion" not in record:
            source = bound.download(entry_id, control=control)
            file.parent.mkdir(parents=True, exist_ok=True)
            scratch = bound.binding.scratch_root / "consumer"
            scratch.mkdir(parents=True, exist_ok=True)
            record["state"] = "dispatched"
            save_checkpoint(file, record, stage="conversion")
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
                    save_checkpoint(file, record, stage="conversion")
                    raise ConsumerError("configuration-invalid", stage="conversion") from None
                except Exception:
                    raise ConsumerError(
                        "callback-failed", stage="conversion", effect="uncertain"
                    ) from None
                control.check("conversion", effect="uncertain")
                _validate_result(result, policy, effect="uncertain")
                record.update(state="converted", conversion=asdict(result))
                save_checkpoint(file, record, stage="conversion")
        result = ConversionResult(**record["conversion"])
        _validate_result(result, policy)
        mirror = None
        if result.outcome == "converted":
            record["state"] = "render-dispatched"
            save_checkpoint(file, record, stage="conversion")
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
        save_checkpoint(file, record, stage="conversion")
        return _status(record, False, policy)


def _load_checkpoint(file: Path) -> dict | None:
    if not file.exists():
        return None
    try:
        if file.is_symlink() or file.stat().st_size > 512 * 1024 * 1024:
            raise ValueError
        document = json.loads(file.read_text())
        if type(document) is not dict:
            raise ValueError
        return document
    except Exception:
        raise ConsumerError(
            "checkpoint-corrupt", stage="index", effect="recovery-required"
        ) from None


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _float32(vector: Sequence[float]) -> tuple[float, ...]:
    return struct.unpack(f"{len(vector)}f", struct.pack(f"{len(vector)}f", *vector))


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
    ) -> None:
        policy.validate()
        if embedder is None or not all(
            callable(value) for value in (converter, renderer, input_view)
        ):
            raise ConsumerError("provider-required", stage="index")
        self.converter, self.renderer, self.input_view = converter, renderer, input_view
        self.policy, self.embedder, self.progress = policy, embedder, progress

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
            max_chunks=self.policy.max_chunks,
        )

    def donor(self, bound: BoundShelf, entry_id: str, *, operation_id: str) -> ReuseDonor:
        """Capture an actual completed view; retaining it does not reserve the donor."""
        return bound._read(
            lambda: self._capture_donor(bound, entry_id, operation_id), CallControl()
        )

    def _capture_donor(self, bound: BoundShelf, entry_id: str, operation_id: str) -> ReuseDonor:
        with bound._working(), bound._shelf._handle.mutex:
            bound._admit(write=True)
            view = bound.view
            entry = bound.resolve(entry_id)
            record = _load_checkpoint(
                _record_path(bound, operation_id).with_suffix(".derived.json")
            )
            if (
                record is None
                or record.get("state") != "complete"
                or record.get("entry_id") != entry_id
                or not entry.searchable
            ):
                raise ConsumerError("donor-not-complete", stage="index")
            with writer_lock(bound.binding.palace_root, operation="consumer donor capture"):
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

        with bound._working(), bound._shelf._handle.mutex:
            before = bound._admit(write=True)
            _record_path(bound, operation_id)
            control.check("index")
            try:
                with writer_lock(bound.binding.palace_root, operation="consumer completion"):
                    identity = self._identity(bound)
                    with closing(
                        sqlite3.connect(bound._shelf.chunks_db, isolation_level=None)
                    ) as conn:
                        init_db(conn, store=bound.binding.palace_root)
                        assert_identity(conn, identity)
                        expected = {
                            entry.entry_id: entry.metadata.mirror_sha256
                            for entry in bound._shelf.files()
                            if entry.searchable
                        }
                        for entry_id in sorted(set(indexed_files(bound._shelf)) - set(expected)):
                            delete_path(
                                conn=conn,
                                watch_root=str(bound.binding.mirror_root.resolve()),
                                path_str=entry_id + ".md",
                            )
                            bound._shelf.mirror_path(entry_id).unlink(missing_ok=True)
                        bound.view  # Validate actual mirrors before declaring completion.
                        if (
                            index_status(bound._shelf).pending
                            or indexed_files(bound._shelf) != expected
                        ):
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
        if not entry.searchable or entry.cas_id != destination.cas_id:
            return None
        if (
            entry.metadata.mirror_sha256 != donor.mirror_sha256
            or entry.cas_id != donor.source_cas_id
        ):
            raise ConsumerError("donor-view-changed", stage="index")
        file = bound._shelf.mirror_path(entry.entry_id)
        if file.is_symlink() or _digest(file.read_text()) != donor.mirror_sha256:
            raise ConsumerError("donor-integrity-mismatch", stage="index")
        snapshot = self._snapshot(bound, conn, entry.entry_id, identity)
        if snapshot is None or snapshot.view_sha256 != donor.view_sha256:
            raise ConsumerError("donor-view-changed", stage="index")
        record = _load_checkpoint(_record_path(bound, donor.conversion_operation_id))
        if record is None or record.get("state") != "complete":
            return None
        if record.get("policy") != asdict(self.policy):
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
        control: CallControl = CallControl(),
        observer: Callable[[Observation], None] | None = None,
    ) -> DerivationStatus:
        with bound._working(), bound._shelf._handle.mutex:
            bound._admit(write=True)
            control.check("index")
            try:
                with writer_lock(bound.binding.palace_root, operation="consumer derivation"):
                    identity = self._identity(bound)
                    db = bound._shelf.chunks_db
                    control.check("index", effect="uncertain")
                    db.parent.mkdir(parents=True, exist_ok=True)
                    with closing(sqlite3.connect(db, isolation_level=None)) as conn:
                        init_db(conn, store=bound.binding.palace_root)
                        assert_identity(conn, identity)
                        return self._derive(
                            bound,
                            conn,
                            identity,
                            entry_id,
                            operation_id,
                            donor,
                            retry_uncertain,
                            control,
                            observer,
                        )
            except ConsumerError:
                raise
            except Exception:
                raise ConsumerError(
                    "derivation-failed", stage="index", effect="recovery-required"
                ) from None

    def _derive(
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
    ):
        before = bound._admit(write=True)
        entry = bound.resolve(entry_id)
        if entry.kind != "file" or entry.metadata is None:
            raise ConsumerError("metadata-required", stage="index")
        file = _record_path(bound, operation_id).with_suffix(".derived.json")
        record = _load_checkpoint(file)
        replayed = record is not None and record.get("state") == "complete"
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
                    "policy": asdict(self.policy),
                    "identity": asdict(identity),
                    "mirror": conversion.mirror_sha256,
                },
                sort_keys=True,
            )
        )
        if record is not None and record.get("fingerprint") != fingerprint:
            raise ConsumerError("operation-conflict", stage="index")
        if replayed:
            path = bound._shelf.mirror_path(entry_id)
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
                "schema": "biblios.consumer-derived/1",
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
            }
            save_checkpoint(file, record, stage="index")
        elif record["state"] == "embedding-dispatched" and not retry_uncertain:
            raise ConsumerError("external-outcome-uncertain", stage="embedding", effect="uncertain")

        path = bound._shelf.mirror_path(entry_id)
        if conversion.state == "converted":
            data = conversion.mirror.markdown.encode("utf-8")
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink() or path.with_suffix(".partial").is_symlink():
                raise ConsumerError("artifact-outside-domain", stage="index")
            if not path.exists() or path.read_bytes() != data:
                temporary = path.with_suffix(".partial")
                with temporary.open("wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
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
            name_rows = [
                i
                for i, row in enumerate(plan.rows)
                if row.heading == conversion.mirror.name_heading
            ]
            if (
                len(plan.rows) > self.policy.max_chunks
                or len(name_rows) != 1
                or name_rows[0] != len(plan.rows) - 1
                or plan.rows[name_rows[0]].body.partition("\n")[2].strip()
                != entry.path.rsplit("/", 1)[-1]
            ):
                raise ConsumerError("mirror-name-row-invalid", stage="index")
            inputs = controlled(lambda: tuple(self.input_view(plan)), control, "embedding")
            if any(not _bounded_text(text, self.policy.max_mirror_bytes) for text in inputs):
                raise ConsumerError("embedding-input-invalid", stage="embedding")
            plan = with_embedding_inputs(plan, representation=self.representation, inputs=inputs)
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
            save_checkpoint(file, record, stage="embedding")
            for start in range(0, len(plan.rows), EMBED_SLICE):
                if bound._admit() != before:
                    raise ConsumerError("stale-view", stage="index", effect="uncertain")
                selected = [
                    i
                    for i in range(start, min(start + EMBED_SLICE, len(plan.rows)))
                    if vectors[i] is None
                ]
                if not selected:
                    continue
                record["state"] = "embedding-dispatched"
                save_checkpoint(file, record, stage="embedding")
                returned = controlled(
                    lambda: self.embedder.embed([plan.embed_inputs[i] for i in selected]),
                    control,
                    "embedding",
                    effect="uncertain",
                )
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
                save_checkpoint(file, record, stage="embedding")
                if self.progress is not None:
                    controlled(
                        lambda: self.progress(
                            EntryProgress(
                                before.shelf_id,
                                entry_id,
                                entry.path,
                                "indexing",
                                sum(vector is not None for vector in vectors),
                                len(vectors),
                            )
                        ),
                        control,
                        "index",
                        effect="uncertain",
                    )
            if bound._admit() != before:
                raise ConsumerError("stale-view", stage="index", effect="uncertain")
            existing = self._snapshot(bound, conn, entry_id, identity)
            same = (
                existing is not None
                and existing.file.file_hash == conversion.mirror_sha256
                and [row.input_sha256 for row in existing.chunks] == hashes
                and [row.vector for row in existing.chunks] == [tuple(vector) for vector in vectors]
            )
            if not same:
                commit_plan(
                    conn=conn,
                    plan=plan,
                    embeddings=vectors,
                    identity=identity,
                    log=lambda line: None,
                )
            actual = self._snapshot(bound, conn, entry_id, identity)
            record["view_sha256"] = actual.view_sha256
            record["state"] = "index-committed"
            save_checkpoint(file, record, stage="index")
        else:
            from palace.index.core import delete_path

            path.unlink(missing_ok=True)
            delete_path(
                conn=conn,
                watch_root=str(bound.binding.mirror_root.resolve()),
                path_str=entry_id + ".md",
            )

        fresh = bound.resolve(entry_id)
        if fresh.cas_id != entry.cas_id or fresh.path != entry.path or fresh.metadata is None:
            raise ConsumerError("stale-view", stage="index", effect="derived-committed")
        metadata = fresh.metadata.with_conversion(
            converter=conversion.result.converter,
            converter_version=conversion.result.converter_version,
            mirror_sha256=conversion.mirror_sha256,
            source_cas_id=entry.cas_id,
            conversion=CONVERTED
            if conversion.state == "converted"
            else UNCONVERTED_PREFIX + conversion.result.reason,
        )
        if metadata != fresh.metadata:
            bound._shelf.record_conversion(fresh, metadata)
        status = index_status(bound._shelf)
        expected = {
            row.entry_id: row.metadata.mirror_sha256
            for row in bound._shelf.files()
            if row.searchable
        }
        if status.pending == 0 and indexed_files(bound._shelf) == expected:
            write_watermark(bound._shelf, bound._shelf.revision)
        record.update(state="complete", committed_view=asdict(bound.view))
        save_checkpoint(file, record, stage="index")
        result = self._result(bound, record, replayed=replayed)
        control.check("index", effect="derived-committed")
        observe(observer, result.observation, control)
        return result
