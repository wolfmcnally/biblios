"""The provider and policy bundle the core runs: converter, renderer, input view,
embedder and the caller-declared derivation policy.

The orchestration lives in :mod:`biblios.core`; this module holds only the public
contracts. Conversion dispatch records (in the derived store) describe uncertainty at a
callback boundary, not an exactly-once external execution or billing guarantee.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from palace.index import EmbeddingRepresentation, FilePlan
from palace.index.embedder import Embedder

from biblios.control import ConsumerError, Effect
from biblios.progress import EntryProgress

_MAX_CHECKPOINT_BYTES = 512 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ReuseDonor:
    """One explicitly nominated same-shelf accepted view; never authorization.

    Captured by :meth:`biblios.consumer.BoundShelf.donor`; retaining it reserves
    nothing. A nominee whose bytes or accepted stage changed since capture refuses
    ``donor-view-changed``; one from another binding refuses ``donor-binding-mismatch``;
    one that is gone or no longer exactly its record is an ordinary miss."""

    entry_id: str
    namespace_id: str
    epoch_id: str
    source_cas_id: str
    mirror_sha256: str
    stage_sha256: str


def literal_inputs(plan: FilePlan) -> tuple[str, ...]:
    """Explicit opt-in view: each exact chunk body, including the separate name row."""
    return tuple(row.body for row in plan.rows)


@dataclass(frozen=True, slots=True)
class ConversionRequest:
    source: bytes
    media_type: str
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

    Its message and traceback are never included in a consumer diagnostic (the outcome
    code ``configuration-invalid``) or in any recorded or replayed result. The calling
    process receives the exception itself, with its traceback, as that call's
    :attr:`~biblios.core.EntryOutcome.cause`; a later call that finds the refusal recorded
    has no exception to give.
    """


class ConversionRefused(Exception):  # noqa: N818 - a document's answer, not a fault
    """A supplied converter's refusal of this document (not of its configuration).

    The core records exactly what a ``ConversionResult('unconverted', reason=
    'document-failed')`` records (the same outcome, checkpoint and retry behaviour) and
    gives the calling process this exception, chained to the converter's own error, as
    that call's :attr:`~biblios.core.EntryOutcome.cause`. It is never recorded or
    replayed; a later call that finds the outcome recorded has no exception to give."""

    def __init__(self, converter: str, converter_version: str) -> None:
        super().__init__(f"{converter} {converter_version} refused the document")
        self.converter = converter
        self.converter_version = converter_version


@dataclass(frozen=True, slots=True)
class MirrorDocument:
    """A rendered mirror: document content only. The entry's name and path are Castle's
    and never appear here; the index adds the current filename as its own section."""

    markdown: str


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
    provenance_header_fields: tuple[str, ...] = ()
    """Mirror-header keys that are converter provenance: kept in the mirror, never indexed
    as attributed metadata. Castle-controlled keys are excluded regardless."""
    max_concurrency: int = 4
    """Entries prepared in overlap; the effective bound is also the embedder's own."""

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
            or type(self.provenance_header_fields) is not tuple
            or not all(_bounded_text(key, 256) and key for key in self.provenance_header_fields)
            or type(self.max_concurrency) is not int
            or not 1 <= self.max_concurrency <= 32
        ):
            raise ConsumerError("policy-invalid", stage="conversion")


def render_escaped(result: ConversionResult) -> MirrorDocument:
    """An explicit optional policy: quote structure and preserve text."""
    return MirrorDocument("\n".join("| " + line for line in result.body.split("\n")) + "\n")


def _policy_document(policy: DerivationPolicy) -> dict:
    """The policy as a JSON- and canonical-encodable mapping (sequences as lists)."""
    document = asdict(policy)
    document["provenance_header_fields"] = list(policy.provenance_header_fields)
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


class ConsumerPipeline:
    """Explicit conversion, input view and model composition over public Palace plans.

    With a store whose selector declares a representation the input view supplies the
    embedding inputs; without one Palace's own inputs are used and ``input_view`` may be
    None. Incompatible derived state is refused, never relabeled or rebuilt implicitly.
    Injected providers remain caller-owned.
    """

    def __init__(
        self,
        *,
        converter: Callable[[ConversionRequest], ConversionResult],
        renderer: Callable[[ConversionResult], MirrorDocument],
        input_view: Callable[[FilePlan], Sequence[str]] | None,
        policy: DerivationPolicy,
        embedder: Embedder,
        progress: Callable[[EntryProgress], None] | None = None,
        max_snapshot_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        policy.validate()
        if type(max_snapshot_bytes) is not int or max_snapshot_bytes <= 0:
            raise ConsumerError("snapshot-budget-invalid", stage="index")
        if embedder is None or not all(callable(value) for value in (converter, renderer)):
            raise ConsumerError("provider-required", stage="index")
        if input_view is not None and not callable(input_view):
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
