"""Run a disposable, local consumer lifetime: python examples/consumer_binding.py."""

from __future__ import annotations

import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from castle.namespace import open_namespace
from castle.namespace_environment import LocalSession
from palace.index.embedder_config import load_embedder_config, save_embedder_config

from biblios import (
    ConsumerPipeline,
    ConversionRequest,
    ConversionResult,
    CreateFile,
    CurationRequest,
    DerivationPolicy,
    ShelfBinding,
    bind_shelf,
    init_shelf,
    literal_inputs,
    load_settings,
    render_escaped,
)
from biblios.testing import HashEmbedder, OverlapReranker


def text_converter(request: ConversionRequest) -> ConversionResult:
    """The consumer chooses this converter; no service or credential discovery."""
    return ConversionResult("converted", request.source.decode("utf-8"), "local-text", "1")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="biblios-consumer-") as directory:
        domain = Path(directory)
        config = domain / "settings.yaml"
        config.write_text(
            "palace:\n  embedding:\n    provider: ollama\n"
            "biblios:\n  max_file_bytes: 104857600\ndrawbridge: {}\nvellric: {}\n"
        )
        settings = load_settings([config], domain)
        root = domain / "shelf"
        seed = init_shelf(root, settings, name="Local consumer example", description="Disposable")
        shelf_id = seed.shelf_id
        seed.close()
        session = LocalSession(root / "namespace", root / "castle", root / "namespace.lock")
        with open_namespace(root / "namespace", root / "castle", session) as handle:
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
            with bind_shelf(binding, handle) as bound:
                request = CurationRequest(
                    str(uuid.uuid4()),
                    bound.view.revision,
                    CreateFile(
                        info.root_id,
                        "harbour.txt",
                        b"Lighthouse lens maintenance.",
                        "text/plain",
                        "2026-11-09T00:00:00+00:00",
                    ),
                )
                stored = bound.curate(request, settings=settings)
                replay = bound.curate(request, settings=settings)
                entry_id = stored.mutation.affected_entry_ids[0]
                print("Stored:", bound.download(entry_id).decode())
                print("Exact replay:", replay.replayed)
                print("Pending derivation:", bound.status().pending)
                policy = DerivationPolicy(
                    "local-text",
                    "1",
                    "escaped-v1",
                    "body-name-v1",
                    "local-v1",
                    "markdown-v1",
                    "filename-v1",
                )
                pipeline = ConsumerPipeline(
                    converter=text_converter,
                    renderer=render_escaped,
                    input_view=literal_inputs,
                    policy=policy,
                    embedder=HashEmbedder(),
                )
                selector = load_embedder_config(binding.palace_root)
                # Explicit initial representation, before any vector is indexed.
                save_embedder_config(
                    binding.palace_root, replace(selector, representation=pipeline.representation)
                )
                derived = bound.derive(pipeline, entry_id, operation_id=str(uuid.uuid4()))
                print("Derivation:", derived.outcome(entry_id).outcome)
                found = bound.search(
                    "Lighthouse lens", embedder=HashEmbedder(), reranker=OverlapReranker()
                )
                print("Strict search hits:", len(found.hits))
                print("Pending derivation:", bound.status().pending)


if __name__ == "__main__":
    main()
