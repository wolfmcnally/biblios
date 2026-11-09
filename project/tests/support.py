"""Shared helpers for the product tests: configuration files, shelves and fixture texts."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from biblios import Settings, Shelf, init_shelf, load_settings

SETTINGS_TEXT = """palace:
  embedding:
    provider: ollama
drawbridge: {}
vellric: {}
"""

NOTES = (
    "Lighthouse maintenance log\n\n"
    "The lamp rotates every twelve seconds. Keepers polish the fresnel lens each spring "
    "and record the kerosene used.\n"
)
RECIPE = (
    "Sourdough notes\n\n"
    "Feed the starter twice a day. Bulk fermentation takes about five hours at room "
    "temperature; shape, then cold retard overnight.\n"
)
TIDE = (
    "Tide tables\n\n"
    "Spring tides follow the new moon and the full moon. Neap tides fall between them "
    "and have the smallest range.\n"
)
GUIDE = (
    "# Field guide\n\n## Knots\n\nThe bowline makes a fixed loop that will not slip.\n\n"
    "## Weather\n\nThe second section: a falling barometer warns of wind.\n"
)


def write_settings(directory: Path, text: str = SETTINGS_TEXT) -> Settings:
    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "settings.yaml"
    file.write_text(text)
    return load_settings([file], directory)


def make_shelf(tmp_path: Path, name: str = "test shelf") -> tuple[Shelf, Settings]:
    settings = write_settings(tmp_path / "config")
    shelf = init_shelf(tmp_path / "shelf", settings, name=name, description="a test shelf")
    return shelf, settings


LONG = "# Harbour almanac\n\n" + "\n\n".join(
    f"## Chapter {n}\n\n"
    + " ".join(
        f"Entry {n}-{i}: the harbour master logs the tide, the wind and the moorings at dawn;"
        f" vessel {i * n} reports draught, cargo and berth before the lock opens."
        for i in range(1, 9)
    )
    for n in range(1, 25)
)

OPENROUTER_SETTINGS_TEXT = """palace:
  embedding:
    provider: openrouter
    model: other/embedder
    dim: 4096
    upstream: https://embeddings.example.invalid/v1
drawbridge: {}
vellric: {}
"""


def make_shelves(
    tmp_path: Path, names: Sequence[str], *, settings_text: str = SETTINGS_TEXT
) -> tuple[list[Shelf], Settings]:
    """One opened shelf per name under ``tmp_path``, all sharing one settings file."""
    settings = write_settings(tmp_path / "config", settings_text)
    shelves = [
        init_shelf(tmp_path / name, settings, name=name, description=f"the {name} shelf")
        for name in names
    ]
    return shelves, settings
