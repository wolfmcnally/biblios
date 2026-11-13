"""Finite synthetic consumer storage; real public Castle admission, no model service."""

from __future__ import annotations

import hashlib
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path

from castle.namespace import open_namespace
from castle.namespace_environment import LocalSession
from castle.namespace_store import NamespaceHandle
from castle.store_state import StoreIdentity

from biblios.binding import ShelfBinding, bind_shelf, open_bound_shelf
from biblios.config import Settings
from biblios.consumer import BoundShelf
from biblios.curation import CreateFile, CurationRequest
from tests.support import make_shelf

ADDED = "2026-11-09T00:00:00+00:00"


def identifier() -> str:
    return str(uuid.uuid4())


def binding_from_document(document: dict) -> ShelfBinding:
    values = dict(document)
    for key in (
        "root",
        "namespace_root",
        "castle_root",
        "mirror_root",
        "palace_root",
        "scratch_root",
    ):
        values[key] = Path(values[key])
    values["expected_cas"] = StoreIdentity(**values["expected_cas"])
    return ShelfBinding(**values)


def inventory(root: Path) -> dict[str, tuple[str, int, int]]:
    """Complete content and metadata oracle, including newly created sidecars."""
    found = {}
    for file in (root, *root.rglob("*")):
        metadata = file.lstat()
        if file.is_symlink():
            value = "link:" + str(file.readlink())
        elif file.is_dir():
            value = "directory"
        else:
            value = hashlib.sha256(file.read_bytes()).hexdigest()
        found[str(file.relative_to(root))] = (
            value,
            stat.S_IMODE(metadata.st_mode),
            metadata.st_mtime_ns,
        )
    return found


@dataclass
class ConsumerFixture:
    binding: ShelfBinding
    handle: NamespaceHandle
    bound: BoundShelf
    root_id: str
    settings: Settings

    def request(self, name: str = "note.txt", data: bytes = b"fixture body") -> CurationRequest:
        return CurationRequest(
            identifier(),
            self.bound.view.revision,
            CreateFile(self.root_id, name, data, "text/plain", ADDED),
        )

    def reopen(self, *, read_only: bool = False) -> BoundShelf:
        self.bound.close()
        self.handle.close()
        self.binding = replace(
            self.binding, ownership="owned", access="read-only" if read_only else "read-write"
        )
        session = LocalSession(
            self.binding.namespace_root,
            self.binding.castle_root,
            self.binding.root / "namespace.lock",
            read_only=read_only,
        )
        self.bound = open_bound_shelf(self.binding, session)
        return self.bound

    def close(self) -> None:
        self.bound.close()
        self.handle.close()


def consumer_fixture(directory: Path) -> ConsumerFixture:
    seed, settings = make_shelf(directory)
    root, shelf_id = seed.root, seed.shelf_id
    seed.close()
    session = LocalSession(root / "namespace", root / "castle", root / "namespace.lock")
    handle = open_namespace(root / "namespace", root / "castle", session)
    info = handle.info()
    binding = ShelfBinding(
        root,
        root / "namespace",
        root / "castle",
        root / "mirror",
        root / "palace",
        root / "scratch",
        shelf_id,
        info.namespace_id,
        info.cas,
    )
    return ConsumerFixture(binding, handle, bind_shelf(binding, handle), info.root_id, settings)


def indexed_fixture(directory: Path) -> ConsumerFixture:
    """Seed via the shipped directory pipeline; consumer behavior is the test boundary."""
    from biblios.testing import HashEmbedder

    fixture = consumer_fixture(directory)
    fixture.bound.curate(fixture.request(data=b"Lighthouse lens tide and harbour maintenance."))
    fixture.bound._shelf.reconcile(fixture.settings, embedder=HashEmbedder())
    return fixture


LANE_POLICY_FIELDS = ("injected", "1", "escaped/1", "palace", "palace", "palace", "filename/1")


def lane_pipeline(fixture, calls: list, *, embedder=None, reuse_converted_body: bool = False):
    """A consumer-lane pipeline over the fixture's store: an injected converter that
    records each source, a name-free renderer with two body sections, each
    chunk's literal body as its input, and the store's selector declaring the
    representation."""
    from palace.index.embedder_config import load_embedder_config, save_embedder_config

    from biblios.pipeline import (
        ConsumerPipeline,
        ConversionResult,
        DerivationPolicy,
        MirrorDocument,
    )
    from biblios.testing import HashEmbedder

    def converter(request):
        calls.append(request.source)
        return ConversionResult("converted", request.source.decode(), "injected", "1")

    def renderer(result):
        return MirrorDocument(
            "# Body\n\n" + result.body + "\n\n## Extra\n\nIndependent body row.\n"
        )

    pipeline = ConsumerPipeline(
        converter=converter,
        renderer=renderer,
        input_view=lambda plan: [row.body for row in plan.rows],
        policy=DerivationPolicy(*LANE_POLICY_FIELDS, reuse_converted_body=reuse_converted_body),
        embedder=embedder or HashEmbedder(),
    )
    root = fixture.binding.palace_root
    selector = load_embedder_config(root)
    save_embedder_config(root, replace(selector, representation=pipeline.representation))
    return pipeline
