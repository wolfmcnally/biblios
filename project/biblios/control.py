"""Safe outcomes and cooperative completion fences for the consumer interface.

These records describe library effects. They do not authenticate a consumer's
publication or interrupt arbitrary synchronous Python code.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
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
    """A bounded diagnostic whose text never contains caller or provider text."""

    def __init__(
        self,
        code: str,
        *,
        stage: Stage,
        effect: Effect = "none",
        observation: Observation | None = None,
    ) -> None:
        # Only library constants or Castle's validated safe codes reach this class.
        self.code = code
        self.stage = stage
        self.effect = effect
        self.observation = observation
        super().__init__(f"{stage}: {code} ({effect})")


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
            except Exception:
                raise ConsumerError("cancellation-failed", stage=stage, effect=effect) from None
            if cancelled:
                raise ConsumerError("cancelled", stage=stage, effect=effect)


def save_checkpoint(file: Path, document: dict, *, stage: Stage) -> None:
    """Durably replace a finite local checkpoint without following a staging symlink."""
    temporary = file.with_suffix(".partial")
    if any(item.is_symlink() for item in (file, file.parent, temporary)):
        raise ConsumerError("artifact-outside-domain", stage=stage, effect="recovery-required")
    try:
        descriptor = os.open(
            temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY | os.O_NOFOLLOW, 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(document, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, file)
        descriptor = os.open(file.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except Exception:
        raise ConsumerError(
            "checkpoint-write-failed", stage=stage, effect="recovery-required"
        ) from None


def controlled[T](
    call: Callable[[], T],
    control: CallControl,
    stage: Stage,
    *,
    effect: Effect = "none",
) -> T:
    """Fence a supplied callback and discard its unsafe exception diagnostics."""
    control.check(stage, effect=effect)
    try:
        result = call()
    except Exception:
        raise ConsumerError("callback-failed", stage=stage, effect=effect) from None
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
        ) from None
