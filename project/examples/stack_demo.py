"""Two shelves that catch up while you watch, searched as one stack.

Usage, from the repository root::

    ./bin/python project/examples/stack_demo.py DIRECTORY FOLDER_A FOLDER_B

DIRECTORY must not exist yet or must be empty. One shelf is built per folder, named after
it, and every regular file in the folder is added. A thread of the demo's own drives each
shelf's ``reconcile`` with progress reporting; the library ships no thread. A status table
is printed every second until a command arrives on standard input:

    search PHRASE        search the stack; names the shelves still indexing
    add SHELF FILE       add a file to the named shelf (a running step yields to it)
    remove SHELF PATH    remove a path from the named shelf
    watch SHELF | all    narrow the table to one shelf, or widen it
    mixed                declare a third shelf with another embedder selector: refused
    quit                 stop the threads and exit

Everything is indexed with the deterministic stand-in embedder from ``biblios.testing``,
so nothing contacts a service. That embedder has no retrieval quality; a real shelf
declares a real embedder.
"""

from __future__ import annotations

import selectors
import sys
import threading
from pathlib import Path

from biblios import (
    DuplicateContentError,
    EntryProgress,
    MixedIdentityError,
    Shelf,
    init_shelf,
    load_settings,
    open_stack,
)
from biblios.testing import HashEmbedder, OverlapReranker

SETTINGS = "palace:\n  embedding:\n    provider: ollama\ndrawbridge: {}\nvellric: {}\n"
OTHER = (
    "palace:\n  embedding:\n    provider: openrouter\n    model: other/embedder\n    dim: 4096\n"
    "    upstream: https://embeddings.example.invalid/v1\ndrawbridge: {}\nvellric: {}\n"
)


class Worker:
    """Drives one shelf's reconcile in a thread until nothing is pending; a curation call
    sets ``changed`` so a running step yields and the loop goes round again."""

    def __init__(self, shelf: Shelf, settings, embedder: HashEmbedder) -> None:
        self.shelf = shelf
        self.settings = settings
        self.embedder = embedder
        self.changed = threading.Event()
        self.stop = threading.Event()
        self.live: dict[str, EntryProgress] = {}
        self.lock = threading.Lock()
        self.error: str | None = None
        self.thread = threading.Thread(target=self.run, name=shelf.manifest.name, daemon=True)

    def progress(self, row: EntryProgress) -> None:
        with self.lock:
            if row.stage in ("searchable", "unconverted"):
                self.live.pop(row.entry_id, None)
            else:
                self.live[row.entry_id] = row

    def run(self) -> None:
        while not self.stop.is_set():
            self.changed.clear()
            try:
                report = self.shelf.reconcile(
                    self.settings,
                    embedder=self.embedder,
                    progress=self.progress,
                    should_yield=lambda: self.changed.is_set() or self.stop.is_set(),
                )
            except Exception as error:  # noqa: BLE001 - shown in the table, retried
                self.error = str(error)
                self.changed.wait(5)
                continue
            self.error = None
            if not report.yielded and not self.changed.is_set():
                self.changed.wait(1)

    def rows(self) -> dict[str, EntryProgress]:
        with self.lock:
            return dict(self.live)


def table(workers: dict[str, Worker], watch: str | None) -> str:
    lines = []
    for name, worker in workers.items():
        if watch not in (None, name):
            continue
        status = worker.shelf.index_status()
        state = "catching up" if status.lagging else "current"
        lines.append(
            f"[{name}] {state}: {status.searchable}/{status.files} searchable, "
            f"{status.pending} pending, {status.unconverted} unconverted, "
            f"revision {status.revision}, index at {status.searchable_revision}"
            + (f"  ERROR {worker.error}" if worker.error else "")
        )
        live = worker.rows()
        for row in status.entries:
            active = live.get(row.entry_id)
            if active is not None and active.total:
                eta = f", about {active.eta_seconds} s left" if active.eta_seconds else ""
                lines.append(f"    {row.path}: {active.stage} {active.done}/{active.total}{eta}")
            else:
                lines.append(f"    {row.path}: {(active or row).stage}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__)
        return 2
    directory = Path(argv[0]).expanduser()
    folders = [Path(name).expanduser() for name in argv[1:]]
    config = directory / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "settings.yaml").write_text(SETTINGS)
    settings = load_settings([config / "settings.yaml"], config)
    embedder, reranker = HashEmbedder(), OverlapReranker()
    workers: dict[str, Worker] = {}
    for folder in folders:
        shelf = init_shelf(directory / folder.name, settings, name=folder.name)
        for file in sorted(p for p in folder.iterdir() if p.is_file()):
            try:
                shelf.add(file.name, file.read_bytes())
            except DuplicateContentError as duplicate:
                print(f"[{folder.name}] {file.name}: {duplicate}")
        workers[folder.name] = Worker(shelf, settings, embedder)
        print(f"[{folder.name}] shelf {shelf.shelf_id}: {len(list(shelf.files()))} files stored")
    for worker in workers.values():
        worker.thread.start()
    stack = open_stack([worker.shelf for worker in workers.values()])
    watch: str | None = None
    selector = selectors.DefaultSelector()
    selector.register(sys.stdin, selectors.EVENT_READ)
    print(
        "\ncommands: search PHRASE | add SHELF FILE | remove SHELF PATH | watch SHELF|all | mixed | quit"
    )
    try:
        while True:
            print("\n" + table(workers, watch), flush=True)
            if not selector.select(timeout=1.0):
                continue
            line = sys.stdin.readline()
            if not line:
                break
            words = line.split()
            if not words:
                continue
            command, rest = words[0], words[1:]
            if command == "quit":
                break
            elif command == "search":
                result = stack.search(" ".join(rest), embedder=embedder, reranker=reranker)
                print(
                    f"indexing: {[n for n, w in workers.items() if w.shelf.shelf_id in result.indexing]}"
                )
                for hit in result.hits:
                    shelf_name = next(
                        n for n, w in workers.items() if w.shelf.shelf_id == hit.shelf_id
                    )
                    print(
                        f"  [{shelf_name}] {hit.path} heading={hit.heading!r} rerank={hit.rerank_status}"
                    )
                    print("     ", hit.chunk[:120].replace("\n", " "))
                if result.refused:
                    print(f"refused (not live or not current): {result.refused}")
            elif command == "add" and len(rest) == 2 and rest[0] in workers:
                file = Path(rest[1]).expanduser()
                worker = workers[rest[0]]
                try:
                    worker.shelf.add(file.name, file.read_bytes())
                except Exception as error:  # noqa: BLE001 - shown, the loop goes on
                    print(f"add refused: {error}")
                worker.changed.set()
            elif command == "remove" and len(rest) == 2 and rest[0] in workers:
                try:
                    workers[rest[0]].shelf.remove(rest[1])
                except Exception as error:  # noqa: BLE001 - shown, the loop goes on
                    print(f"remove refused: {error}")
                workers[rest[0]].changed.set()
            elif command == "watch" and len(rest) == 1:
                watch = None if rest[0] == "all" else rest[0]
            elif command == "mixed":
                other = directory / "other-config"
                other.mkdir(exist_ok=True)
                (other / "settings.yaml").write_text(OTHER)
                third = init_shelf(
                    directory / "other",
                    load_settings([other / "settings.yaml"], other),
                    name="other",
                )
                try:
                    open_stack([worker.shelf for worker in workers.values()] + [third])
                except MixedIdentityError as refused:
                    print(f"refused: {refused}")
                finally:
                    third.close()
            else:
                print("unknown command")
    finally:
        for worker in workers.values():
            worker.stop.set()
            worker.changed.set()
        unfinished = []
        for worker in workers.values():
            worker.thread.join(30)
            if worker.thread.is_alive():
                # The step finishes its current entry before it stops; a shelf still in
                # use is not closed from under its worker, and the exit says so.
                unfinished.append(worker.shelf.manifest.name)
                print(
                    f"[{worker.shelf.manifest.name}] still finishing its current entry; not closed"
                )
            else:
                worker.shelf.close()
    if unfinished:
        print(f"incomplete shutdown: {', '.join(unfinished)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
