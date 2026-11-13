"""The Drawbridge adapter's result and error mapping, with synthetic Drawbridge stand-ins:
a configuration refusal is the operator's (raised, its cause kept); a document refusal,
deferral or pending result is an unconverted outcome; only a finished rendering is the
body, and it is converted from a copy under a neutral name, never the entry's."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from drawbridge.errors import DrawbridgeError

from biblios import representation
from biblios.pipeline import ConversionConfigurationError, ConversionRefused, ConversionRequest
from tests.support import make_shelf


@pytest.fixture
def convert(tmp_path: Path):
    _shelf, settings = make_shelf(tmp_path)
    with _shelf:
        scratch = tmp_path / "scratch-convert"
        scratch.mkdir()
        request = ConversionRequest(b"# Notes\n\nbody\n", "text/markdown", scratch)
        yield representation.drawbridge_converter(settings), request


def test_a_configuration_refusal_is_raised_with_its_cause(convert, monkeypatch):
    """Failure mode: an unreadable configuration is recorded as the document's failure
    (an unconverted entry), or raised without the error that says where it failed."""
    converter, request = convert
    failure = ValueError("profile unreadable")

    def refuse(*args, **kwargs):
        raise failure

    monkeypatch.setattr(representation, "load_configuration", refuse)
    with pytest.raises(ConversionConfigurationError) as raised:
        converter(request)
    assert raised.value.__cause__ is failure


@pytest.mark.parametrize(
    ("fake", "expected"),
    [
        ("deferred", ("unconverted", "", "deferred")),
        ("pending", ("unconverted", "", "deferred")),
        ("semantic", ("converted", "SEMANTIC", None)),
        ("mirror", ("converted", "MIRROR", None)),
    ],
)
def test_drawbridge_outcomes_map_to_unconverted_or_the_finished_rendering(
    convert, monkeypatch, fake, expected
):
    """Failure mode: a refused, deferred or still-pending conversion becomes a mirror
    (so an unfinished rendering is indexed), the semantic rendering is ignored when the
    profile produced one, or the entry's filename reaches the converter (as the staged
    file's name, the title or the provenance header), so it would land in the mirror."""
    converter, request = convert
    seen: list[Path] = []
    options: list = []

    def convert_file(source, **kwargs):
        seen.append(source)
        options.append(kwargs["options"])
        return SimpleNamespace(
            method="conversion-deferred:size" if fake == "deferred" else "fidelity",
            flags={"ocr-pending"} if fake == "pending" else set(),
            semantic="SEMANTIC" if fake == "semantic" else None,
            mirror="MIRROR",
        )

    monkeypatch.setattr(representation, "convert_file", convert_file)
    monkeypatch.setattr(representation, "render_mirror", lambda rendering: rendering)
    result = converter(request)
    assert (result.outcome, result.body, result.reason) == expected
    assert (result.converter, result.converter_version) == (
        representation.CONVERTER,
        representation.drawbridge.__version__,
    )
    [source] = seen
    assert source == request.scratch_root / "document.md"
    assert source.read_bytes() == request.source
    [given] = options
    assert (given.title, given.mirror_of, given.acquired_from) == (
        representation.NEUTRAL_TITLE,
        "document",
        "biblios",
    )


def test_a_document_refusal_is_raised_typed_with_drawbridge_s_own_error(convert, monkeypatch):
    """Failure mode: the adapter turns Drawbridge's refusal into a result that keeps
    nothing of the error, so the refusal's site is lost to the caller."""
    converter, request = convert
    failure = DrawbridgeError("unprocessable")

    def convert_file(source, **kwargs):
        raise failure

    monkeypatch.setattr(representation, "convert_file", convert_file)
    with pytest.raises(ConversionRefused) as raised:
        converter(request)
    assert raised.value.__cause__ is failure
    assert (raised.value.converter, raised.value.converter_version) == (
        representation.CONVERTER,
        representation.drawbridge.__version__,
    )


def test_the_core_records_a_refusal_as_unconverted_and_gives_its_cause_once(tmp_path, monkeypatch):
    """Failure mode: a typed document refusal is treated as an uncertain dispatch (and
    retried), recorded differently from a document-failed result, or loses Drawbridge's
    error from the outcome; or the exception is persisted and replayed."""
    import uuid

    from biblios import BringCurrentRequest, bring_current
    from biblios.consumer import BoundShelf
    from biblios.pipeline import ConsumerPipeline
    from biblios.testing import HashEmbedder

    shelf, settings = make_shelf(tmp_path)
    with shelf:
        entry_id = shelf.add("notes.md", b"# Notes\n\nbody\n").affected_entry_ids[0]
        failure = DrawbridgeError("unprocessable")
        calls: list[Path] = []

        def convert_file(source, **kwargs):
            calls.append(source)
            raise failure

        monkeypatch.setattr(representation, "convert_file", convert_file)
        pipeline = ConsumerPipeline(
            converter=representation.drawbridge_converter(settings),
            renderer=representation.envelope_renderer,
            input_view=None,
            policy=representation.default_policy(),
            embedder=HashEmbedder(),
        )
        bound = BoundShelf(shelf.local_binding(), shelf)
        operation = str(uuid.uuid4())
        first = bring_current(bound, BringCurrentRequest(operation, (entry_id,)), pipeline=pipeline)
        outcome = first.outcome(entry_id)
        assert (outcome.outcome, outcome.code) == ("unconverted", "document-failed")
        assert isinstance(outcome.cause, ConversionRefused) and outcome.cause.__cause__ is failure
        replay = bring_current(
            bound, BringCurrentRequest(operation, (entry_id,)), pipeline=pipeline
        )
        assert replay.replayed and replay.outcome(entry_id).cause is None
        again = bring_current(
            bound,
            BringCurrentRequest(str(uuid.uuid4()), (entry_id,), retry_uncertain=True),
            pipeline=pipeline,
        )
        assert again.outcome(entry_id).outcome == "unconverted" and len(calls) == 1
