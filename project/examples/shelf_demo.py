"""Walk one shelf through its life: create, add, reconcile, search, read, rename, remove.

Usage, from the repository root::

    ./bin/python project/examples/shelf_demo.py DIRECTORY FILE [FILE ...]

DIRECTORY must not exist yet or must be empty. The script writes a settings file beside the
shelf, converts the files with Drawbridge's fidelity path, and indexes them with the
deterministic stand-in embedder from ``biblios.testing``, so nothing contacts a service.
That embedder has no retrieval quality; a real shelf declares a real embedder.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from castle import cas

from biblios import DuplicateContentError, Entry, init_shelf, load_settings
from biblios.testing import HashEmbedder, OverlapReranker

SETTINGS = "palace:\n  embedding:\n    provider: ollama\ndrawbridge: {}\nvellric: {}\n"


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    directory = Path(argv[0]).expanduser()
    files = [Path(name).expanduser() for name in argv[1:]]
    config = directory / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.yaml").write_text(SETTINGS)
    settings = load_settings([config / "settings.yaml"], config)
    embedder, reranker = HashEmbedder(), OverlapReranker()

    with init_shelf(directory / "shelf", settings, name="demo shelf") as shelf:
        print(f"shelf {shelf.shelf_id} at {shelf.root}")
        entries: list[Entry] = []
        for file in files:
            try:
                added = shelf.add(file.name, file.read_bytes())
            except DuplicateContentError as duplicate:
                print(f"{file.name}: duplicate reported: {duplicate}")
                continue
            entry = shelf.resolve(added.entry_id)
            entries.append(entry)
            assert entry.metadata is not None
            print(f"added {entry.path} as entry {entry.entry_id} ({entry.metadata.media_type})")
        if entries:
            try:
                shelf.add(entries[0].path + ".again", shelf.download(entries[0].entry_id))
            except DuplicateContentError as duplicate:
                print(f"adding the same bytes again: {duplicate}")

        report = shelf.reconcile(settings, embedder=embedder)
        print(
            f"reconcile: built={report.built} converted={len(report.converted)} "
            f"unconverted={len(report.unconverted)}"
        )
        for entry_id in report.unconverted:
            unconverted = shelf.resolve(entry_id)
            assert unconverted.metadata is not None
            print(f"  unconverted {unconverted.path}: {unconverted.metadata.conversion}")
        print(f"verify: {shelf.verify()}")

        converted = [shelf.resolve(e.entry_id) for e in entries]
        converted = [e for e in converted if e.conversion_current]
        if not converted:
            print("\nno file converted, so there is nothing to search; the reasons are above")
            return 0
        first = converted[0]
        words = re.findall(r"[A-Za-z]{5,}", shelf.read(first.entry_id).text)[:4]
        query = " ".join(words) or first.path
        print(f"\nsearch {query!r}")
        for hit in shelf.search(query, embedder=embedder, reranker=reranker).hits:
            print(
                f"  {hit.path} entry={hit.entry_id} heading={hit.heading!r} "
                f"rerank={hit.rerank_status}"
            )
            print("   ", hit.chunk[:120].replace("\n", " "))
        page = shelf.read(first.entry_id)
        if page.page_count:
            page = shelf.read(first.entry_id, page=1)
            print(f"\npage 1 of {page.page_count}:\n{page.text[:300]}")

        shelf.rename(first.path, "renamed-" + first.path)
        print(f"\nrenamed to {shelf.resolve(first.entry_id).path}")
        print(f"reconcile: {shelf.reconcile(settings, embedder=embedder)}")
        for hit in shelf.search(query, embedder=embedder, reranker=reranker).hits[:1]:
            print(f"  hit now at {hit.path} (same entry: {hit.entry_id == first.entry_id})")

        if len(converted) > 1:
            victim = converted[1]
            shelf.remove(victim.path)
            print(f"\nremoved {victim.path}")
            assert victim.cas_id is not None
            kept = cas.require_object(shelf.castle_root, victim.cas_id).read_bytes()
            print(f"its {len(kept)} bytes remain in castle/")
            still = shelf.search(query, embedder=embedder, reranker=reranker)
            print(f"search still answers {len(still.hits)} hit(s); withheld={still.withheld}")
        print(f"verify: {shelf.verify()}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
