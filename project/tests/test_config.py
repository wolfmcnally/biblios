"""The Conric composition: Biblios validates its own section, hands the palace section to
Palace, and leaves the rest alone."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from conric import ConfigError
from palace.index._errors import IndexError as PalaceRefusal

from biblios import load_settings
from tests.support import write_settings


def test_palace_section_is_palaces_and_other_sections_pass_through(tmp_path: Path) -> None:
    """Failure mode: Biblios validates or rewrites Palace's selector itself, reads or rejects
    a dependency's section, or accepts a composition that never declared an embedding."""
    text = """palace:
  embedding:
    provider: ollama
biblios: {}
drawbridge:
  stages:
    structure:
      endpoint: local
  anything: [1, 2, 3]
vellric:
  ocr: {languages: [eng]}
"""
    settings = write_settings(tmp_path / "config", text)
    assert settings.palace.embedding.provider == "ollama"
    assert settings.palace.embedding.dim == 4096
    assert settings.files == (tmp_path / "config" / "settings.yaml",)
    assert not hasattr(settings, "drawbridge") and not hasattr(settings, "embedder")
    # A Bedrock selector in Palace's shape is Palace's to validate; Biblios passes it through.
    bedrock = write_settings(
        tmp_path / "bedrock",
        "palace:\n  embedding:\n    provider: bedrock\n    model: cohere.embed-v4:0\n"
        "    region: us-east-1\n    dim: 1536\n    published_corpus: true\n"
        "    published_corpus_note: synthetic\n"
        "    published_corpus_asserted_at: '2026-11-07T00:00:00Z'\ndrawbridge: {}\nvellric: {}\n",
    )
    assert (bedrock.palace.embedding.provider, bedrock.palace.embedding.region) == (
        "bedrock",
        "us-east-1",
    )
    assert bedrock.palace.embedding.dim == 1536
    # Palace's refusals, in Palace's words, with the path and no value echoed.
    with pytest.raises(PalaceRefusal, match="palace.embedding.provider"):
        write_settings(
            tmp_path / "unknown",
            "palace:\n  embedding:\n    provider: carrier-pigeon\ndrawbridge: {}\nvellric: {}\n",
        )
    with pytest.raises(PalaceRefusal, match="palace"):
        write_settings(tmp_path / "missing", "biblios: {}\ndrawbridge: {}\nvellric: {}\n")
    # Biblios's own section is optional; an unknown key is refused.
    with pytest.raises(ConfigError, match="unknown_field"):
        write_settings(
            tmp_path / "stray",
            "palace:\n  embedding:\n    provider: ollama\nbiblios:\n  embedder:\n"
            "    provider: ollama\ndrawbridge: {}\nvellric: {}\n",
        )
    absent = tmp_path / "absent"
    absent.mkdir()
    (absent / "settings.yaml").write_text("drawbridge: {}\nvellric: {}\n")
    with pytest.raises(PalaceRefusal):
        load_settings([absent / "settings.yaml"], absent)


def test_source_file_limit_defaults_composition_and_validation(tmp_path: Path) -> None:
    """A configured source allowance must survive composition without bool coercion."""
    base = "palace:\n  embedding:\n    provider: ollama\n"
    defaults = write_settings(tmp_path / "default", base)
    empty = write_settings(tmp_path / "empty", base + "biblios: {}\n")
    override = tmp_path / "override.yaml"
    override.write_text("biblios:\n  max_file_bytes: 7\n")
    composed = load_settings([defaults.files[0], override], tmp_path)
    assert (defaults.max_file_bytes, empty.max_file_bytes, composed.max_file_bytes) == (
        104857600,
        104857600,
        7,
    ), "source-file default or composed override was lost"
    for index, value in enumerate(("0", "-1", "true", "1.5", "'7'", "null")):
        with pytest.raises(ConfigError, match="expected_positive_integer") as refused:
            write_settings(
                tmp_path / f"invalid-{index}", base + f"biblios:\n  max_file_bytes: {value}\n"
            )
        assert refused.value.path == ("biblios", "max_file_bytes"), (
            "invalid source limit lost its configuration key location"
        )
    with pytest.raises(ValueError, match="max_file_bytes must be a positive integer"):
        replace(defaults, max_file_bytes=True)
