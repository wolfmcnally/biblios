"""Derive one Drawbridge mirror per file entry, keyed by entry id.

Conversion runs inside Drawbridge's configuration context over the composed configuration
files, so the ``drawbridge`` and ``vellric`` sections reach Drawbridge unchanged. The
fidelity conversion is the default; when the composed profile declares a stage, Drawbridge
runs it and its ``semantic`` rendering, if one is produced, is the mirror that is written
and indexed. Every conversion is handed Drawbridge's command-line OCR backend, so an image,
or a scanned page, is recognized with the Tesseract on the machine's PATH; when it is
absent the recognition fails and the entry carries a truthful unconverted mark. A file
Drawbridge defers or refuses yields no mirror and an ``unconverted:<reason>`` label; it is
never indexed.
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import drawbridge
from castle import cas
from drawbridge.configuration import Configuration, configuration_context, load_configuration
from drawbridge.documents import convert_file
from drawbridge.errors import DrawbridgeError, blocked_reason
from drawbridge.mirror import render_mirror

from biblios.config import Settings
from biblios.entries import CONVERTED, UNCONVERTED_PREFIX, Entry, EntryMetadata

if TYPE_CHECKING:
    from biblios.shelf import Shelf

DEFERRED_PREFIX = "conversion-deferred:"
PENDING_FLAGS = {"mirror-pending", "ocr-pending", "description-pending"}


@dataclass(frozen=True, slots=True)
class MirrorResult:
    """The outcome of converting one entry."""

    entry_id: str
    mirror_path: Path | None
    mirror_sha256: str
    converter: str
    converter_version: str
    conversion: str

    @property
    def converted(self) -> bool:
        return self.conversion == CONVERTED


def _convert(source: Path, entry: Entry, configuration: Configuration) -> tuple[str, str | None]:
    """Return ``(converter label, rendered mirror or None)`` for one source file."""
    assert entry.metadata is not None
    with configuration_context(configuration):
        result = convert_file(
            source,
            ocr=drawbridge.CliOcrBackend(available_cores=1, preflight=False),
            options=drawbridge.ConversionOptions(title=entry.metadata.display_name),
            media_type=entry.metadata.media_type,
            stages=bool(configuration.profile.stages),
        )
    if result.method.startswith(DEFERRED_PREFIX) or PENDING_FLAGS.intersection(result.flags):
        return result.method, None
    if result.semantic is not None:
        return result.method + ":semantic", render_mirror(result.semantic)
    return result.method, render_mirror(result.mirror)


def convert_entry(shelf: Shelf, entry: Entry, settings: Settings) -> MirrorResult:
    """Convert one file entry, write or remove its mirror, and return what to record."""
    assert entry.cas_id is not None and entry.metadata is not None
    source = cas.require_object(shelf.castle_root, entry.cas_id)
    mirror_path = shelf.mirror_path(entry.entry_id)
    version = drawbridge.__version__
    # A configuration refusal is the operator's problem, not the document's: it propagates
    # instead of marking the entry unconverted.
    configuration = load_configuration(settings.files, root=settings.root)
    try:
        with tempfile.TemporaryDirectory(prefix="biblios-convert-") as temporary:
            # Drawbridge names the mirror after the file it reads, so convert a copy
            # carrying the entry's display name rather than the content address.
            staged = Path(temporary) / entry.metadata.display_name
            staged.write_bytes(source.read_bytes())
            method, rendered = _convert(staged, entry, configuration)
    except DrawbridgeError as error:
        reason = blocked_reason(error) or "conversion-failed"
        mirror_path.unlink(missing_ok=True)
        return MirrorResult(
            entry.entry_id, None, "", type(error).__name__, version, UNCONVERTED_PREFIX + reason
        )
    if rendered is None:
        mirror_path.unlink(missing_ok=True)
        reason = method.removeprefix(DEFERRED_PREFIX)
        return MirrorResult(entry.entry_id, None, "", method, version, UNCONVERTED_PREFIX + reason)
    data = rendered.encode("utf-8")
    shelf.mirror_root.mkdir(exist_ok=True)
    mirror_path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    return MirrorResult(entry.entry_id, mirror_path, digest, method, version, CONVERTED)


def recorded(entry: Entry, result: MirrorResult) -> EntryMetadata:
    """The entry's metadata with the conversion fields filled in."""
    assert entry.metadata is not None and entry.cas_id is not None
    return entry.metadata.with_conversion(
        converter=result.converter,
        converter_version=result.converter_version,
        mirror_sha256=result.mirror_sha256,
        source_cas_id=entry.cas_id,
        conversion=result.conversion,
    )
