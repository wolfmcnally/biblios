"""The shelf's default providers: Drawbridge's converter and its own mirror rendering.

Conversion runs inside Drawbridge's configuration context over the composed configuration
files, so the ``drawbridge`` and ``vellric`` sections reach Drawbridge unchanged. The
fidelity conversion is the default; when the composed profile declares a stage, Drawbridge
runs it and its ``semantic`` rendering, if one is produced, is the mirror. Every
conversion is handed Drawbridge's command-line OCR backend, so an image or a scanned page
is recognized with the Tesseract on the machine's PATH; when it is absent recognition
fails and the entry records a truthful unconverted outcome. A file Drawbridge defers or
refuses yields no mirror and is never indexed.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import drawbridge
from drawbridge.configuration import Configuration, configuration_context, load_configuration
from drawbridge.documents import convert_file
from drawbridge.errors import DrawbridgeError
from drawbridge.mirror import REQUIRED_HEADER_FIELDS, render_mirror

from biblios.config import Settings
from biblios.pipeline import (
    ConversionConfigurationError,
    ConversionRefused,
    ConversionRequest,
    ConversionResult,
    DerivationPolicy,
    MirrorDocument,
)

CONVERTER = "drawbridge"
RENDERING = "drawbridge-mirror"
_DEFERRED_PREFIX = "conversion-deferred:"
_PENDING_FLAGS = {"mirror-pending", "ocr-pending", "description-pending"}
NEUTRAL_TITLE = "Document"
"""The title Drawbridge writes as the mirror's H1. A mirror carries no filename or path:
Castle's namespace is the only authority for them (operator ruling, 2026-11-13)."""
_STAGED_STEM = "document"
DRAWBRIDGE_PROVENANCE = tuple(
    sorted(
        REQUIRED_HEADER_FIELDS
        | {"format", "unit", "extensions", "page_citations", "pdf_page_map", "page_count"}
        | {"pdf_processor", "ocr_pages", "slide_count", "slide_map", "presentation"}
        | {"attachment_count", "audio_duration_seconds", "detected_language"}
        | {"speaker_count", "transcription_package", "speech_level_spread_db"}
        | {"leveling_applied", "second_pass_agreement", "speakers_rendered_at"}
    )
)
"""The header keys Drawbridge writes about its own conversion (its required set and the
per-format fields of the pinned release): provenance, not attributed metadata, so a freshly
converted mirror contributes no metadata section. A Drawbridge release that adds a key
must add it here."""


def default_policy(max_mirror_bytes: int = 8 * 1024 * 1024) -> DerivationPolicy:
    """The standalone shelf's declared derivation: Drawbridge's conversion and mirror,
    indexed with Palace's own embedding inputs."""
    return DerivationPolicy(
        converter=CONVERTER,
        converter_version=drawbridge.__version__,
        rendering=RENDERING,
        convention="palace",
        implementation="palace",
        chunker="palace",
        name_policy="drawbridge",
        max_mirror_bytes=max_mirror_bytes,
        provenance_header_fields=DRAWBRIDGE_PROVENANCE,
    )


def _convert(source: Path, request: ConversionRequest, configuration: Configuration) -> str | None:
    with configuration_context(configuration):
        result = convert_file(
            source,
            ocr=drawbridge.CliOcrBackend(available_cores=1, preflight=False),
            options=drawbridge.ConversionOptions(
                title=NEUTRAL_TITLE, mirror_of=_STAGED_STEM, acquired_from="biblios"
            ),
            media_type=request.media_type,
            stages=bool(configuration.profile.stages),
        )
    if result.method.startswith(_DEFERRED_PREFIX) or _PENDING_FLAGS.intersection(result.flags):
        return None
    if result.semantic is not None:
        return render_mirror(result.semantic)
    return render_mirror(result.mirror)


def drawbridge_converter(settings: Settings) -> Callable[[ConversionRequest], ConversionResult]:
    """A converter over the composed configuration; its rendered mirror is the body."""

    def convert(request: ConversionRequest) -> ConversionResult:
        try:
            configuration = load_configuration(settings.files, root=settings.root)
        except Exception as error:
            # A configuration refusal is the operator's problem, not the document's.
            raise ConversionConfigurationError from error
        # Drawbridge would name the mirror after the file it reads: convert a copy under a
        # neutral name, so no filename reaches the converted text or its header.
        staged = request.scratch_root / (_STAGED_STEM + _suffix(request.media_type))
        staged.write_bytes(request.source)
        try:
            rendered = _convert(staged, request, configuration)
        except DrawbridgeError as error:
            # The document's refusal, with Drawbridge's own error (and its trace) kept.
            raise ConversionRefused(CONVERTER, drawbridge.__version__) from error
        if rendered is None:
            return ConversionResult(
                "unconverted", "", CONVERTER, drawbridge.__version__, "deferred"
            )
        return ConversionResult("converted", rendered, CONVERTER, drawbridge.__version__)

    return convert


def envelope_renderer(result: ConversionResult) -> MirrorDocument:
    """Drawbridge's rendered mirror is already the shelf's mirror; it is kept exactly."""
    return MirrorDocument(result.body)


def _suffix(media_type: str) -> str:
    import mimetypes

    return mimetypes.guess_extension(media_type, strict=False) or ""
