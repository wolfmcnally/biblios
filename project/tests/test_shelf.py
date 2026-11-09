"""Layout and manifest proofs over a temporary shelf on disk."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from biblios import SHELF_SCHEMA, BibliosError, ShelfManifest, open_shelf
from biblios.shelf import MANIFEST_NAME, NAMESPACE_LOCK
from tests.support import make_shelf


def test_init_creates_the_layout_and_declares_the_embedder_to_palace_only(tmp_path: Path) -> None:
    """Failure mode: init leaves a layer missing, or records the embedder somewhere besides
    Palace's selector so two records can disagree."""
    shelf, settings = make_shelf(tmp_path)
    with shelf:
        root = shelf.root
        assert (root / MANIFEST_NAME).is_file()
        assert (root / "castle" / "journal.jsonl").is_file()
        assert (root / "namespace").is_dir()
        assert (root / NAMESPACE_LOCK).exists()
        assert (root / "mirror").is_dir()
        assert (root / "palace" / "meta" / "embedder.toml").is_file()
        assert "ollama" in (root / "palace" / "meta" / "embedder.toml").read_text()
        manifest = json.loads((root / MANIFEST_NAME).read_text())
        assert manifest["schema"] == SHELF_SCHEMA == "biblios.shelf/1"
        assert not any("embed" in key for key in manifest)
        assert not any("embed" in f.name for f in dataclasses.fields(ShelfManifest))
        assert shelf.ls() == []


def test_manifest_refuses_another_schema_and_bad_fields(tmp_path: Path) -> None:
    """Failure mode: a shelf written by another schema opens as if it were this one."""
    shelf, _ = make_shelf(tmp_path)
    shelf.close()
    manifest_path = shelf.root / MANIFEST_NAME
    document = json.loads(manifest_path.read_text())
    document["schema"] = "biblios.shelf/2"
    manifest_path.write_text(json.dumps(document))
    with pytest.raises(BibliosError, match="unsupported shelf schema"):
        open_shelf(shelf.root)
    with pytest.raises(ValueError, match="shelf id"):
        ShelfManifest(shelf_id="", name="", description="")
    with pytest.raises(ValueError, match="shelf mode"):
        ShelfManifest(shelf_id="s", name="", description="", mode="frozen")
