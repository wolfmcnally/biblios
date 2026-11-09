"""The Conric composition Biblios is configured through.

Biblios validates only its own ``biblios`` section, which today declares nothing: the
embedding selection belongs to Palace and lives in the top-level ``palace`` section, which
Biblios hands to Palace exactly as composed and never reads for itself. The ``drawbridge``
and ``vellric`` sections travel to Drawbridge unchanged, because Drawbridge accepts
configuration only as file paths: conversion runs inside Drawbridge's configuration
context over the same files, so the dependency closure inherits the composed settings.
Nothing here reads the environment or discovers files; Conric's own rules apply.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from conric import SchemaError, Value, load
from palace.settings import PalaceSettings, load_palace_settings

SECTION = "biblios"
_FIELDS: frozenset[str] = frozenset()


@dataclass(frozen=True, slots=True)
class Settings:
    """The composed files, their root, and Palace's validated ``palace`` section."""

    files: tuple[Path, ...]
    root: Path
    palace: PalaceSettings


def _section(value: Value) -> None:
    if not isinstance(value, dict):
        raise SchemaError("expected_mapping")
    unknown = sorted(set(value) - _FIELDS)
    if unknown:
        raise SchemaError("unknown_field", path=(unknown[0],))


def load_settings(files: list[Path] | tuple[Path, ...], root: Path) -> Settings:
    """Compose the explicit files; validate the ``biblios`` section when present and hand
    the ``palace`` section to Palace.

    A ``biblios`` refusal is Conric's ``ConfigError`` exactly as Conric reports it. A
    ``palace`` refusal is Palace's own, naming the file, line, column, dotted path and
    reason without echoing any value. The other sections are left untouched in the files.
    """
    paths = tuple(Path(file) for file in files)
    config = load(paths, root=root)
    if isinstance(config.data, dict) and SECTION in config.data:
        config.validate(_section, path=(SECTION,))
    palace = load_palace_settings(paths, root=root)
    return Settings(files=paths, root=Path(root), palace=palace)
