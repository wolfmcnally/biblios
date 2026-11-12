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


def test_an_older_castle_namespace_is_refused_typed_naming_the_migration_and_never_migrated(
    tmp_path: Path,
):
    """A shelf whose namespace carries Castle's v1 format marker (as Castle 0.3 wrote it)
    is refused at open with the typed error naming Castle's explicit migration, and the
    store is left exactly as it was: Biblios never migrates."""
    import sqlite3
    from contextlib import closing

    from castle import namespace_migrate_v1

    from biblios.errors import NamespaceFormatError

    shelf, _settings = make_shelf(tmp_path)
    root = shelf.root
    shelf.close()
    with closing(sqlite3.connect(root / "namespace" / "namespace.sqlite")) as db:
        db.execute("PRAGMA user_version=1")

    def stored() -> dict[str, bytes]:
        return {
            str(path.relative_to(root)): path.read_bytes()
            for path in sorted((root / "namespace").rglob("*"))
            if path.is_file()
        }

    before = stored()
    with pytest.raises(NamespaceFormatError) as raised:
        open_shelf(root)
    assert raised.value.__cause__.code == "namespace-format-unsupported"
    module, name = NamespaceFormatError.MIGRATION.rsplit(".", 1)
    assert module == namespace_migrate_v1.__name__ and callable(getattr(namespace_migrate_v1, name))
    assert NamespaceFormatError.MIGRATION in str(raised.value)
    assert stored() == before
