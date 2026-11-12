"""The ``biblios.entry/1`` metadata schema and the public view of a namespace entry.

Castle's namespace is the only catalog of entries and their descriptive metadata. What
Biblios knows about indexing an entry lives in its own derived store
(:mod:`biblios.derived`), never in Castle metadata.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from castle.namespace import EntryRecord

from biblios.errors import BibliosError

ENTRY_SCHEMA = "biblios.entry/1"
"""Schema identifier carried by every entry's metadata."""

_FILE_FIELDS = frozenset({"schema", "media_type", "display_name", "added_at", "tags", "dates"})
_STRING_FIELDS = ("media_type", "display_name", "added_at")
LEGACY_FIELDS = frozenset(
    {"converter", "converter_version", "mirror_sha256", "source_cas_id", "conversion"}
)
"""Indexing fields earlier releases kept in Castle metadata. Only the bounded preservation
import reads them; every metadata write carries them through verbatim and none adds them."""

DIRECTORY_METADATA: dict[str, Any] = {"schema": ENTRY_SCHEMA, "kind": "directory"}


class EntryMetadataError(BibliosError):
    """An entry's metadata is not a valid ``biblios.entry/1`` record."""


@dataclass(frozen=True, slots=True)
class EntryMetadata:
    """What Biblios records in Castle about one file entry: descriptive fields only."""

    media_type: str
    display_name: str
    added_at: str
    tags: tuple[str, ...] = ()
    dates: dict[str, str] = field(default_factory=dict)
    legacy: dict[str, str] = field(default_factory=dict)
    """The five legacy indexing keys exactly as stored, or empty; opaque to everything
    except the preservation import."""

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": ENTRY_SCHEMA,
            "media_type": self.media_type,
            "display_name": self.display_name,
            "added_at": self.added_at,
            "tags": list(self.tags),
            "dates": dict(self.dates),
            **self.legacy,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> EntryMetadata:
        if not isinstance(value, dict):
            raise EntryMetadataError("entry metadata does not carry the biblios.entry/1 fields")
        keys = set(value)
        legacy_keys = keys - _FILE_FIELDS
        if not _FILE_FIELDS <= keys or legacy_keys not in (set(), set(LEGACY_FIELDS)):
            raise EntryMetadataError("entry metadata does not carry the biblios.entry/1 fields")
        if value["schema"] != ENTRY_SCHEMA:
            raise EntryMetadataError(f"unsupported entry schema: {value['schema']!r}")
        for key in (*_STRING_FIELDS, *sorted(legacy_keys)):
            if not isinstance(value[key], str):
                raise EntryMetadataError(f"entry metadata field {key} must be a string")
        tags = value["tags"]
        if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
            raise EntryMetadataError("entry tags must be a list of strings")
        dates = value["dates"]
        if not isinstance(dates, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in dates.items()
        ):
            raise EntryMetadataError("entry dates must map strings to strings")
        return cls(
            media_type=value["media_type"],
            display_name=value["display_name"],
            added_at=value["added_at"],
            tags=tuple(tags),
            dates=dict(dates),
            legacy={key: value[key] for key in sorted(legacy_keys)},
        )


@dataclass(frozen=True, slots=True)
class Entry:
    """The public view of one live entry: its stable id and its current path."""

    entry_id: str
    kind: str
    path: str
    size: int | None
    cas_id: str | None
    metadata: EntryMetadata | None

    @property
    def is_file(self) -> bool:
        return self.kind == "file"


def path_of(entry_id: str, live: dict[str, EntryRecord], root_id: str) -> str:
    """Resolve an entry's current path from the live tree."""
    parts: list[str] = []
    current = live[entry_id]
    while current.entry_id != root_id:
        if current.name is None or current.parent_id is None:
            raise BibliosError(f"entry {entry_id} has no path")
        parts.append(current.name)
        current = live[current.parent_id]
    return "/".join(reversed(parts))


def view(record: EntryRecord, live: dict[str, EntryRecord], root_id: str) -> Entry:
    # Castle's explicit metadata-only excision preserves the live entry with {}.
    # Absence is redaction, never newly invented descriptive data.
    metadata = (
        EntryMetadata.from_mapping(record.metadata)
        if record.kind == "file" and record.metadata != {}
        else None
    )
    if record.kind == "directory" and record.entry_id != root_id:
        if record.metadata not in ({}, DIRECTORY_METADATA):
            raise EntryMetadataError("directory entry does not carry biblios.entry/1 metadata")
    return Entry(
        entry_id=record.entry_id,
        kind=record.kind,
        path=path_of(record.entry_id, live, root_id),
        size=record.size,
        cas_id=record.cas_id,
        metadata=metadata,
    )
