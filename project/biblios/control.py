"""Safe outcomes and cooperative completion fences for the consumer interface.

These records describe library effects. They do not authenticate a consumer's
publication or interrupt arbitrary synchronous Python code.
"""

from __future__ import annotations

import json
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from biblios.errors import BibliosError

Effect = Literal[
    "none", "namespace-committed", "derived-committed", "recovery-required", "uncertain"
]
Stage = Literal[
    "binding",
    "read",
    "curation",
    "conversion",
    "embedding",
    "index",
    "search",
    "history",
    "observer",
]


@dataclass(frozen=True, slots=True)
class ShelfView:
    """A particular admitted authority and derived checkpoint, never a capability."""

    shelf_id: str
    namespace_id: str
    store_id: str
    epoch: str
    lineage_id: str
    generation: int
    revision: int
    database_generation: str
    derived_view: str = ""


@dataclass(frozen=True, slots=True)
class Observation:
    """One actual local commit; observer delivery is not external publication."""

    operation_id: str
    stage: Stage
    effect: Effect
    view: ShelfView
    revision_before: int
    entry_ids: tuple[str, ...] = ()
    content_ids: tuple[str, ...] = ()
    mirror_sha256: str = ""
    replayed: bool = False


class ConsumerError(BibliosError):
    """A bounded diagnostic whose text never contains caller or provider text.

    ``retry_after`` is set only for a provider throttle that gave a hint: the seconds
    still to wait, measured when the error was made. It is a number, never provider text,
    and it is never recorded, so a replay cannot renew it."""

    def __init__(
        self,
        code: str,
        *,
        stage: Stage,
        effect: Effect = "none",
        observation: Observation | None = None,
        retry_after: float | None = None,
    ) -> None:
        # Only library constants or Castle's validated safe codes reach this class.
        self.code = code
        self.stage = stage
        self.effect = effect
        self.observation = observation
        self.retry_after = retry_after
        super().__init__(f"{stage}: {code} ({effect})")


#: Why a provider call that stopped definitely, with nothing performed, did not finish.
_STOPPED = {
    "deadline": "deadline-exceeded",
    "cancelled": "cancelled",
    "cancellation-failed": "cancellation-failed",
    "pacer-refused": "pacer-refused",
}


@dataclass(frozen=True, slots=True)
class ProviderOutcome:
    """The classification of one unit of provider work's drained failures.

    ``definite`` is true only when every failure is known to have left nothing performed
    for its request and was a throttle, a deadline, a cancellation or a pacer refusal (or
    a Biblios check refused before dispatch): the unit may be reported ``pending`` with
    effect ``none``. Anything else is uncertain. ``throttled`` is true when any failure was
    a provider throttle, whatever its certainty, or when the unit was only stopped because
    the call observed a throttle. ``code`` says why: ``provider-throttled`` when throttled,
    otherwise a definite unit's first code, otherwise the first uncertain failure's code.
    ``retry_after`` is the longest remaining provider hint among the throttles, or ``None``.
    """

    definite: bool
    throttled: bool
    code: str
    retry_after: float | None


def provider_outcome(
    failures: Sequence[BaseException], *, stopped_by_throttle: bool = False
) -> ProviderOutcome:
    """Classify the failures of one unit of provider work after every call has drained.

    ``failures`` are the exceptions raised by the unit's provider calls and the checks
    around them, in slice order; the result never depends on completion order. Only the
    exception as raised is classified, never its ``__cause__``: a caller's wrapper that
    re-raises a Palace error as its own is an ordinary failure. A Palace
    ``ProviderCallError`` is definite when its merged ``delivery`` is ``not-sent`` or
    ``rejected`` and it was a throttle or stopped for a deadline, cancellation or pacer
    refusal (its latest ``kind``; an earlier throttle followed by another refusal stays
    uncertain); its class is never read for certainty. A :class:`ConsumerError` is definite
    when its effect is ``none``. Every other exception is uncertain ``callback-failed``.
    """
    if not failures and not stopped_by_throttle:
        raise ValueError("provider_outcome needs a failure or a throttle stop")
    from palace.index import ProviderCallError

    now = time.monotonic()
    definite, throttled, hints = True, stopped_by_throttle, []
    first_definite: str | None = None
    first_uncertain: str | None = None
    for error in failures:
        if isinstance(error, ProviderCallError):
            certain = error.delivery in ("not-sent", "rejected")
            if error.throttled:
                throttled = True
                hint = _remaining(error, now)
                if hint is not None:
                    hints.append(hint)
            if error.kind == "throttled":
                code = "provider-throttled"
            elif error.stop == "beyond-deadline":
                code = "deadline-exceeded"
            else:
                code = _STOPPED.get(error.kind, "callback-failed")
            certain = certain and code != "callback-failed"
        elif isinstance(error, ConsumerError):
            code, certain = error.code, error.effect == "none"
        else:
            code, certain = "callback-failed", False
        if certain:
            first_definite = first_definite or code
        else:
            definite = False
            first_uncertain = first_uncertain or code
    if throttled:
        code = "provider-throttled"
    elif definite:
        assert first_definite is not None
        code = first_definite
    else:
        assert first_uncertain is not None
        code = first_uncertain
    return ProviderOutcome(definite, throttled, code, max(hints) if hints else None)


def provider_throttled(error: BaseException) -> bool:
    """Whether a raised provider call was throttled on any attempt, whatever its certainty:
    the signal to stop sending fresh work, never a judgement of what was performed."""
    from palace.index import ProviderCallError

    return isinstance(error, ProviderCallError) and error.throttled


def _remaining(error, now: float) -> float | None:
    """Seconds left of a provider hint at ``now``: bounded, finite and never negative."""
    if error.retry_not_before is not None and math.isfinite(error.retry_not_before):
        return max(0.0, error.retry_not_before - now)
    hint = error.retry_after
    if hint is None or not math.isfinite(hint) or hint < 0:
        return None
    return float(hint)


@dataclass(frozen=True, slots=True)
class CallControl:
    """An absolute monotonic deadline and a caller-owned cancellation predicate."""

    deadline: float | None = None
    cancelled: Callable[[], bool] | None = None

    def check(self, stage: Stage, *, effect: Effect = "none") -> None:
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ConsumerError("deadline-exceeded", stage=stage, effect=effect)
        if self.cancelled is not None:
            try:
                cancelled = self.cancelled()
            except Exception as error:
                raise ConsumerError("cancellation-failed", stage=stage, effect=effect) from error
            if cancelled:
                raise ConsumerError("cancelled", stage=stage, effect=effect)


def save_checkpoint(
    file: Path, document: dict, *, stage: Stage, max_bytes: int | None = None
) -> None:
    """Durably replace a checkpoint, optionally bounding its encoded UTF-8 JSON.

    Bounded saves encode once before opening the staging file. The encoded text
    and bytes coexist with the document; this limit does not bound process memory.
    """
    temporary = file.with_suffix(".partial")
    if any(item.is_symlink() for item in (file, file.parent, temporary)):
        raise ConsumerError("artifact-outside-domain", stage=stage, effect="recovery-required")
    try:
        payload = None
        if max_bytes is not None:
            if type(max_bytes) is not int or max_bytes <= 0:
                raise ValueError
            payload = json.dumps(document, sort_keys=True)
            if len(payload.encode("utf-8")) > max_bytes:
                raise ValueError
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            if payload is None:
                json.dump(document, stream, sort_keys=True)
            else:
                stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        descriptor = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception as error:
        raise ConsumerError(
            "checkpoint-write-failed", stage=stage, effect="recovery-required"
        ) from error


def controlled[T](
    call: Callable[[], T],
    control: CallControl,
    stage: Stage,
    *,
    effect: Effect = "none",
) -> T:
    """Fence a supplied callback: its failure becomes ``callback-failed`` with the
    original exception kept as ``__cause__``, so the traceback reaches the raising frame."""
    control.check(stage, effect=effect)
    try:
        result = call()
    except Exception as error:
        raise ConsumerError("callback-failed", stage=stage, effect=effect) from error
    control.check(stage, effect=effect)
    return result


def observe(
    callback: Callable[[Observation], None] | None,
    observation: Observation,
    control: CallControl,
) -> None:
    if callback is None:
        return
    try:
        controlled(lambda: callback(observation), control, "observer", effect=observation.effect)
    except ConsumerError as error:
        raise ConsumerError(
            error.code,
            stage="observer",
            effect=observation.effect,
            observation=observation,
        ) from error
