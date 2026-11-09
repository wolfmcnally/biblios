"""The ``biblios.entry/1`` metadata schema and the public view of a namespace entry.

Castle's namespace is the only catalog. Everything Biblios knows about a file lives in the
entry's metadata map under this schema and is validated on every read.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Any

from castle.namespace import EntryRecord

from biblios.errors import BibliosError

ENTRY_SCHEMA = "biblios.entry/1"
"""Schema identifier carried by every entry's metadata."""

CONVERTED = "converted"
UNCONVERTED_PREFIX = "unconverted:"

_FILE_FIELDS = {
    "schema",
    "media_type",
    "display_name",
    "added_at",
    "converter",
    "converter_version",
    "mirror_sha256",
    "source_cas_id",
    "conversion",
    "tags",
    "dates",
}
_STRING_FIELDS = (
    "media_type",
    "display_name",
    "added_at",
    "converter",
    "converter_version",
    "mirror_sha256",
    "source_cas_id",
    "conversion",
)

DIRECTORY_METADATA: dict[str, Any] = {"schema": ENTRY_SCHEMA, "kind": "directory"}


class EntryMetadataError(BibliosError):
    """An entry's metadata is not a valid ``biblios.entry/1`` record."""


@dataclass(frozen=True, slots=True)
class EntryMetadata:
    """What Biblios records about one file entry.

    ``source_cas_id`` is the content address the recorded conversion ran over. A live
    entry whose cas id differs has been replaced since, and its mirror is stale.
    """

    media_type: str
    display_name: str
    added_at: str
    converter: str = ""
    converter_version: str = ""
    mirror_sha256: str = ""
    source_cas_id: str = ""
    conversion: str = ""
    tags: tuple[str, ...] = ()
    dates: dict[str, str] = field(default_factory=dict)

    @property
    def converted(self) -> bool:
        return self.conversion == CONVERTED

    @property
    def pending(self) -> bool:
        """No conversion has been recorded yet."""
        return self.conversion == ""

    def current_for(self, cas_id: str | None) -> bool:
        """Whether the recorded conversion was run over the entry's live bytes."""
        return not self.pending and self.source_cas_id == cas_id

    def with_conversion(
        self,
        *,
        converter: str,
        converter_version: str,
        mirror_sha256: str,
        source_cas_id: str,
        conversion: str,
    ) -> EntryMetadata:
        return replace(
            self,
            converter=converter,
            converter_version=converter_version,
            mirror_sha256=mirror_sha256,
            source_cas_id=source_cas_id,
            conversion=conversion,
        )

    def to_mapping(self) -> dict[str, Any]:
        return {
            "schema": ENTRY_SCHEMA,
            "media_type": self.media_type,
            "display_name": self.display_name,
            "added_at": self.added_at,
            "converter": self.converter,
            "converter_version": self.converter_version,
            "mirror_sha256": self.mirror_sha256,
            "source_cas_id": self.source_cas_id,
            "conversion": self.conversion,
            "tags": list(self.tags),
            "dates": dict(self.dates),
        }

    @classmethod
    def from_mapping(cls, value: Any) -> EntryMetadata:
        if not isinstance(value, dict) or set(value) != _FILE_FIELDS:
            raise EntryMetadataError("entry metadata does not carry the biblios.entry/1 fields")
        if value["schema"] != ENTRY_SCHEMA:
            raise EntryMetadataError(f"unsupported entry schema: {value['schema']!r}")
        for key in _STRING_FIELDS:
            if not isinstance(value[key], str):
                raise EntryMetadataError(f"entry metadata field {key} must be a string")
        conversion = value["conversion"]
        if conversion not in ("", CONVERTED) and not conversion.startswith(UNCONVERTED_PREFIX):
            raise EntryMetadataError(f"unknown conversion state: {conversion!r}")
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
            converter=value["converter"],
            converter_version=value["converter_version"],
            mirror_sha256=value["mirror_sha256"],
            source_cas_id=value["source_cas_id"],
            conversion=conversion,
            tags=tuple(tags),
            dates=dict(dates),
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

    @property
    def searchable(self) -> bool:
        """Converted over the live bytes, so its mirror and chunks are current."""
        return (
            self.metadata is not None
            and self.metadata.converted
            and self.metadata.current_for(self.cas_id)
        )


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
    # Absence is redaction, never newly invented conversion or descriptive data.
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
