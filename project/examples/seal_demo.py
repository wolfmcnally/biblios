"""Seal a shelf, carry it as a directory, and unseal it.

Usage, from the repository root::

    ./bin/python project/examples/seal_demo.py DIRECTORY FOLDER

DIRECTORY must not exist yet or must be empty. One shelf is built from every regular file
in FOLDER and reconciled with the deterministic stand-in embedder from ``biblios.testing``
(no service is contacted; that embedder has no retrieval quality). Then commands are read
from standard input:

    add FILE          add a file to the original (then `seal` is refused until `reconcile`)
    reconcile         bring the original's index up to date
    seal              seal the original; prints the record or the refusal
    copy              copy the sealed directory to DIRECTORY/copy and open it read-only
    search PHRASE     search the copy when it is open, else the original
    add-copy FILE     try to add a file to the copy: the refusal
    verify            verify the copy when it is open, else the original
    tamper            copy to DIRECTORY/tampered, flip a byte of its index, verify it
    unseal            unseal the original (closes the copy first)
    quit
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from biblios import (
    BibliosError,
    Shelf,
    init_shelf,
    load_settings,
    open_shelf,
    seal,
    unseal,
    verify_seal,
)
from biblios.testing import HashEmbedder, OverlapReranker

SETTINGS = "palace:\n  embedding:\n    provider: ollama\ndrawbridge: {}\nvellric: {}\n"


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    directory = Path(argv[0]).expanduser()
    folder = Path(argv[1]).expanduser()
    config = directory / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.yaml").write_text(SETTINGS)
    settings = load_settings([config / "settings.yaml"], config)
    embedder, reranker = HashEmbedder(), OverlapReranker()
    original: Shelf = init_shelf(directory / "shelf", settings, name=folder.name)
    for file in sorted(p for p in folder.iterdir() if p.is_file()):
        try:
            original.add(file.name, file.read_bytes())
        except BibliosError as error:
            print(f"{file.name}: {error}")
    report = original.reconcile(settings, embedder=embedder)
    status = original.index_status()
    print(
        f"shelf {original.shelf_id}: {status.files} files, {status.searchable} searchable, "
        f"{status.unconverted} unconverted; built={report.built}"
    )
    copy: Shelf | None = None
    print(
        "\ncommands: add FILE | reconcile | seal | copy | search PHRASE | add-copy FILE | verify | tamper | unseal | quit"
    )
    try:
        for line in sys.stdin:
            words = line.split()
            if not words:
                continue
            command, rest = words[0], words[1:]
            target = copy or original
            try:
                if command == "quit":
                    break
                elif command == "add" and len(rest) == 1:
                    file = Path(rest[0]).expanduser()
                    original.add(file.name, file.read_bytes())
                    print(
                        f"added {file.name}; the index now lags: {original.index_status().lagging}"
                    )
                elif command == "reconcile":
                    r = original.reconcile(settings, embedder=embedder)
                    print(
                        f"reconciled: indexed={len(r.indexed)} lagging={original.index_status().lagging}"
                    )
                elif command == "seal":
                    record = seal(original)
                    print(
                        f"sealed generation {record.generation} at revision {record.sealed_revision}"
                    )
                    print(f"  snapshot {record.snapshot.snapshot_id} ({record.snapshot.cas_id})")
                    for name, digest in record.digests.items():
                        print(f"  {name}: {digest[:16]}…")
                    index_dir = original.chunks_db.parent
                    print(f"  index directory: {sorted(p.name for p in index_dir.iterdir())}")
                elif command == "copy":
                    if copy is not None:
                        copy.close()
                    destination = directory / "copy"
                    if destination.exists():
                        shutil.rmtree(destination)
                    shutil.copytree(original.root, destination)
                    copy = open_shelf(destination)
                    print(f"copy opened read-only at {destination}; sealed={copy.sealed}")
                elif command == "search":
                    result = target.search(" ".join(rest), embedder=embedder, reranker=reranker)
                    which = "copy" if copy is not None else "original"
                    print(f"{which}: {len(result.hits)} hit(s); indexing={result.indexing}")
                    for hit in result.hits:
                        print(f"  {hit.path} heading={hit.heading!r} rerank={hit.rerank_status}")
                        print("     ", hit.chunk[:120].replace("\n", " "))
                elif command == "add-copy" and len(rest) == 1:
                    if copy is None:
                        print("no copy open; type copy first")
                        continue
                    file = Path(rest[0]).expanduser()
                    copy.add(file.name, file.read_bytes())
                    print("THE COPY ACCEPTED A WRITE (this must not happen)")
                elif command == "verify":
                    report_v = verify_seal(target) if target.sealed else target.verify()
                    print(
                        f"verify {'copy' if copy is not None else 'original'}: ok={report_v.ok} {list(report_v.problems)}"
                    )
                elif command == "tamper":
                    tampered = directory / "tampered"
                    if tampered.exists():
                        shutil.rmtree(tampered)
                    shutil.copytree(original.root, tampered)
                    db = tampered / "palace" / "index" / "chunks.sqlite"
                    data = bytearray(db.read_bytes())
                    data[-1] ^= 0xFF
                    db.write_bytes(bytes(data))
                    with open_shelf(tampered) as bad:
                        print(f"tampered copy verify: {list(verify_seal(bad).problems)}")
                elif command == "unseal":
                    if copy is not None:
                        copy.close()
                        copy = None
                    original.close()
                    original = unseal(original.root)
                    print(
                        f"unsealed: sealed={original.sealed} generation={original.manifest.generation}"
                    )
                else:
                    print("unknown command")
            except BibliosError as error:
                print(f"refused: {type(error).__name__}: {error}")
    finally:
        if copy is not None:
            copy.close()
        original.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
