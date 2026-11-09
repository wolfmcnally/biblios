"""The qualification lanes, run on the instance against shelves on the data volume.

Invoked through the staged checkout's own interpreter (``./bin/python qualification/lanes.py``)
so the product under test is the one the bundle carries. Every lane writes one JSON record
``{lane, outcome, counts, notes}``; the runner collects the document and renders it. A lane
never claims more than it ran: a lane that cannot run says why under ``not-run``, the
ingestion lane reports ``incomplete`` when a required tool or corpus kind is absent, and the
Bedrock lane stays ``not-run`` until the declared provider is Palace's ``bedrock`` and names
exactly the confirmed model and region.

Before anything runs, every shelf root is asserted to sit on the data volume, not the root
filesystem, so a pass here is a pass on EBS and not on ``/tmp``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from audit import AUDIT_NAME, audit_counts, audit_problems

HERMETIC_DEMOS = ("shelf_demo.py", "stack_demo.py", "seal_demo.py")
CORPUS_KINDS = {
    ".pdf": "pdf",
    ".docx": "office",
    ".xlsx": "office",
    ".pptx": "office",
    ".png": "image",
    ".jpg": "image",
    ".jpeg": "image",
    ".txt": "text",
    ".md": "text",
}


REQUIRED_KINDS = {"pdf", "office", "image", "text"}
BEDROCK_FIXED_TEXTS = 2  # one short document and one query: the lane's whole input
SETTINGS_TEXT = "palace:\n  embedding:\n    provider: ollama\ndrawbridge: {}\nvellric: {}\n"
NOTES = (
    "Lighthouse maintenance log\n\nThe lamp rotates every twelve seconds. Keepers polish the "
    "fresnel "
    "lens each spring and record the kerosene used.\n"
)
TIDE = (
    "Tide tables\n\nSpring tides follow the new moon and the full moon. Neap tides fall between "
    "them "
    "and have the smallest range.\n"
)


def deterministic_settings(directory: Path):
    """The hermetic lanes declare the default local identity and inject the deterministic
    embedder; no model is reached."""
    from biblios import load_settings

    directory.mkdir(parents=True, exist_ok=True)
    file = directory / "settings.yaml"
    file.write_text(SETTINGS_TEXT)
    return load_settings([file], directory)


def device_of(path: Path) -> int:
    return os.stat(path).st_dev


def assert_on_data_volume(root: Path, *, allow_root_filesystem: bool = False) -> dict[str, Any]:
    """The lane roots must be on a filesystem other than the root filesystem's.

    ``allow_root_filesystem`` exists for local smokes of the lane code itself; a run that
    uses it is recorded as waived and can never be reported as an EBS qualification.
    """
    root.mkdir(parents=True, exist_ok=True)
    marker = root / ".biblios-data-volume"
    data_device, root_device = device_of(root), device_of(Path("/"))
    waived = False
    if data_device == root_device:
        if not allow_root_filesystem:
            raise SystemExit(f"REFUSED: {root} is on the root filesystem, not the data volume")
        waived = True
    return {
        "root": str(root),
        "device_marker": marker.read_text().strip() if marker.is_file() else None,
        "st_dev": data_device,
        "data_volume_waived": waived,
    }


def run(
    cmd: list[str], *, cwd: Path, env: dict[str, str], stdin: str | None = None, timeout: int = 3600
) -> dict[str, Any]:
    started = time.monotonic()
    completed = subprocess.run(
        cmd,
        cwd=cwd,
        env=env,
        input=stdin,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    return {
        "argv": cmd,
        "exit": completed.returncode,
        "seconds": round(time.monotonic() - started, 1),
        "stdout_tail": completed.stdout[-4000:],
        "stderr_tail": completed.stderr[-4000:],
    }


def lane_hermetic(checkout: Path, root: Path, env: dict[str, str]) -> dict[str, Any]:
    """The product suite and the three demos, with every temporary shelf on the volume."""
    basetemp = root / "qualification" / "tmp"
    shutil.rmtree(basetemp, ignore_errors=True)
    basetemp.mkdir(parents=True)
    suite = run(["./bin/test", "project/tests", "--basetemp", str(basetemp)], cwd=checkout, env=env)
    demos = {}
    demo_root = root / "qualification" / "demos"
    shutil.rmtree(demo_root, ignore_errors=True)
    demo_root.mkdir(parents=True)
    sample = demo_root / "sample"
    sample.mkdir()
    (sample / "notes.txt").write_text(
        "The lamp rotates every twelve seconds; keepers polish the fresnel lens each spring.\n"
    )
    (sample / "guide.md").write_text(
        "# Field guide\n\n## Knots\n\nThe bowline makes a fixed loop that will not slip.\n"
    )
    demos["shelf_demo.py"] = run(
        [
            "./bin/python",
            "project/examples/shelf_demo.py",
            str(demo_root / "shelf"),
            str(sample / "notes.txt"),
            str(sample / "guide.md"),
        ],
        cwd=checkout,
        env=env,
    )
    second = demo_root / "sample-b"
    second.mkdir()
    (second / "tides.txt").write_text(TIDE)
    demos["stack_demo.py"] = run(
        [
            "./bin/python",
            "project/examples/stack_demo.py",
            str(demo_root / "stack"),
            str(sample),
            str(second),
        ],
        cwd=checkout,
        env=env,
        stdin="search fresnel lens\nsearch neap tides\nquit\n",
    )
    demos["seal_demo.py"] = run(
        ["./bin/python", "project/examples/seal_demo.py", str(demo_root / "seal"), str(sample)],
        cwd=checkout,
        env=env,
        stdin="seal\ncopy\nsearch fresnel lens\nverify\nunseal\nquit\n",
    )
    passed = suite["exit"] == 0 and all(d["exit"] == 0 for d in demos.values())
    return {
        "lane": "hermetic",
        "outcome": "passed" if passed else "failed",
        "counts": {
            "suite_exit": suite["exit"],
            "demos_passed": sum(d["exit"] == 0 for d in demos.values()),
            "demos": len(demos),
        },
        "notes": {"suite": suite, "demos": demos, "basetemp": str(basetemp)},
    }


def lane_ingestion(root: Path, corpus: Path | None) -> dict[str, Any]:
    """Real documents through Drawbridge as Biblios integrates it; outcomes per kind."""
    if corpus is None or not corpus.is_dir():
        return {
            "lane": "ingestion",
            "outcome": "not-run",
            "counts": {},
            "notes": {"reason": "no corpus directory supplied"},
        }
    from biblios import init_shelf
    from biblios.testing import HashEmbedder

    shelf_root = root / "qualification" / "ingestion"
    shutil.rmtree(shelf_root, ignore_errors=True)
    settings = deterministic_settings(shelf_root / "config")
    files = sorted(p for p in corpus.rglob("*") if p.is_file() and not p.name.startswith("."))
    per_kind: dict[str, dict[str, int]] = {}
    with init_shelf(shelf_root / "shelf", settings, name="ingestion") as shelf:
        for file in files:
            kind = CORPUS_KINDS.get(file.suffix.lower(), "other")
            per_kind.setdefault(kind, {"added": 0, "converted": 0, "unconverted": 0})
            shelf.add(str(file.relative_to(corpus)), file.read_bytes())
            per_kind[kind]["added"] += 1
        shelf.reconcile(settings, embedder=HashEmbedder())
        status = shelf.index_status()
        for entry in shelf.files():
            kind = CORPUS_KINDS.get(Path(entry.path).suffix.lower(), "other")
            metadata = entry.metadata
            if metadata is not None and metadata.converted:
                per_kind[kind]["converted"] += 1
            else:
                per_kind[kind]["unconverted"] += 1
        report = shelf.verify()
    tools = tools_present()
    missing_tools = sorted(name for name, present in tools.items() if not present)
    missing_kinds = sorted(REQUIRED_KINDS - {k for k, v in per_kind.items() if v["added"]})
    if not report.ok or status.lagging:
        outcome = "failed"
    elif missing_tools or missing_kinds:
        outcome = "incomplete"
    else:
        outcome = "passed"
    notes: dict[str, Any] = {"verify_problems": list(report.problems), "tools_present": tools}
    if outcome == "incomplete":
        notes["reason"] = "prerequisites unmet: " + "; ".join(
            filter(
                None,
                [
                    f"tools missing: {', '.join(missing_tools)}" if missing_tools else "",
                    f"corpus kinds missing: {', '.join(missing_kinds)}" if missing_kinds else "",
                ],
            )
        )
    return {
        "lane": "ingestion",
        "outcome": outcome,
        "counts": {
            "files": len(files),
            "searchable": status.searchable,
            "unconverted": status.unconverted,
            "pending": status.pending,
            "per_kind": per_kind,
        },
        "notes": notes,
    }


def tools_present() -> dict[str, bool]:
    """The conversion tools the ingestion lane depends on: `file`, Tesseract and Vellric
    (on PATH or named by DRAWBRIDGE_VELLRIC)."""
    vellric = os.environ.get("DRAWBRIDGE_VELLRIC")
    return {
        "file": shutil.which("file") is not None,
        "tesseract": shutil.which("tesseract") is not None,
        "vellric": bool(vellric and os.access(vellric, os.X_OK))
        or shutil.which("vellric") is not None,
    }


def lane_portability(root: Path) -> dict[str, Any]:
    """Seal, copy to another directory on the same volume, open read-only, verify, search,
    then unseal the copy and prove it is rebuilt at its new root."""
    from biblios import init_shelf, open_shelf, seal, unseal, verify_seal
    from biblios.progress import recorded_root
    from biblios.testing import HashEmbedder, OverlapReranker

    base = root / "qualification" / "portability"
    shutil.rmtree(base, ignore_errors=True)
    settings = deterministic_settings(base / "config")
    embedder, reranker = HashEmbedder(), OverlapReranker()

    with init_shelf(base / "original", settings, name="portable") as shelf:
        notes = shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
        shelf.add("tide.txt", TIDE.encode(), media_type="text/plain")
        shelf.reconcile(settings, embedder=embedder)
        record = seal(shelf)
    copy = base / "copy"
    shutil.copytree(base / "original", copy)
    if device_of(copy) != device_of(base / "original"):
        raise SystemExit("REFUSED: the copy left the data volume")
    notes_hits = 0
    with open_shelf(copy) as copied:
        verified = verify_seal(copied)
        found = copied.search("fresnel lens kerosene", embedder=embedder, reranker=reranker)
        notes_hits = sum(1 for hit in found.hits if hit.entry_id == notes.entry_id)
        sidecars = [
            p.name for p in copied.chunks_db.parent.iterdir() if p.name.endswith(("-wal", "-shm"))
        ]
    with unseal(copy) as moved:
        moved_root_before = recorded_root(moved)
        report = moved.reconcile(settings, embedder=embedder)
        rebuilt = report.built and recorded_root(moved) == str(moved.mirror_root.resolve())
        after = moved.search("fresnel lens kerosene", embedder=embedder, reranker=reranker)
        lagging = moved.index_status().lagging
    with open_shelf(base / "original") as original:
        original_ok = verify_seal(original).ok and original.sealed
    passed = (
        verified.ok
        and notes_hits > 0
        and not sidecars
        and rebuilt
        and not lagging
        and after.hits
        and original_ok
    )
    return {
        "lane": "portability",
        "outcome": "passed" if passed else "failed",
        "counts": {
            "copy_hits": notes_hits,
            "rebuilt": int(bool(rebuilt)),
            "sidecars": len(sidecars),
        },
        "notes": {
            "verify_problems": list(verified.problems),
            "sealed_revision": record.sealed_revision,
            "recorded_root_moved": moved_root_before != str(copy / "mirror"),
            "original_still_sealed": original_ok,
        },
    }


def lane_bedrock(
    root: Path,
    settings_file: Path | None,
    *,
    model: str | None,
    region: str | None,
    texts: int | None,
) -> dict[str, Any]:
    """One shelf through Palace's bedrock provider, only when the settings declare it and
    name exactly the confirmed model and region, and the confirmed text count covers the
    lane's fixed input (one document, one query)."""

    def not_run(reason: str) -> dict[str, Any]:
        return {"lane": "bedrock", "outcome": "not-run", "counts": {}, "notes": {"reason": reason}}

    if settings_file is None or not settings_file.is_file():
        return not_run("no Bedrock settings file supplied")
    from biblios import init_shelf, load_settings

    # The composed settings as Biblios and Palace read them (Conric includes and overrides
    # resolved, Palace's validation applied), never the entry file's raw text.
    try:
        settings = load_settings([settings_file], settings_file.parent)
    except Exception as error:  # noqa: BLE001 - reported, not raised, so the record is truthful
        return not_run(f"settings refused: {error}")
    declared = settings.palace.embedding
    if declared.provider != "bedrock":
        return not_run("provider not released: the declared provider is not bedrock")
    if model is None or region is None or texts is None:
        return not_run("the confirmed model, region and text count were not passed to the lane")
    # Palace refuses remote embedding without the operator's complete published-corpus
    # assertion; the lane refuses first, so the record says why and no request is attempted.
    if not (
        declared.published_corpus
        and (declared.published_corpus_note or "").strip()
        and (declared.published_corpus_asserted_at or "").strip()
    ):
        return not_run(
            "the settings carry no complete published-corpus assertion (published_corpus, "
            "published_corpus_note, published_corpus_asserted_at), which Palace requires "
            "before any remote embedding request"
        )
    mismatch = [
        f"{key}: declared {value!r}, confirmed {confirmed!r}"
        for key, value, confirmed in (
            ("model", declared.model, model),
            ("region", declared.region, region),
        )
        if value != confirmed
    ]
    if mismatch:
        return not_run("settings do not match the confirmation: " + "; ".join(mismatch))
    if texts < BEDROCK_FIXED_TEXTS:
        return not_run(
            f"confirmed text count {texts} is below the lane's fixed input of "
            f"{BEDROCK_FIXED_TEXTS} texts"
        )
    base = root / "qualification" / "bedrock"
    shutil.rmtree(base, ignore_errors=True)
    with init_shelf(base / "shelf", settings, name="bedrock") as shelf:
        # Palace appends an audit record for every request, including a refused one, before
        # it raises; the export therefore runs whatever happened once the store exists, so a
        # Bedrock refusal still leaves its records for the exit trap to upload.
        with exporting_audit(shelf.palace_store, root / AUDIT_NAME) as exported:
            shelf.add("notes.txt", NOTES.encode(), media_type="text/plain")
            shelf.reconcile(settings)
            status = shelf.index_status()
            # Palace's own reranker from the bundled model: the one search in these lanes
            # that exercises it, so its status is part of the verdict.
            found = shelf.search("fresnel lens")
    audit = exported["records"]
    outcome, problems = bedrock_verdict(
        lagging=status.lagging, hits=len(found.hits), rerank_status=found.rerank_status, audit=audit
    )
    if exported["error"]:
        problems.append(f"audit export failed: {exported['error']}")
        outcome = "failed"
    notes: dict[str, Any] = {
        "provider": "bedrock",
        "model": model,
        "region": region,
        "audit_artifact": AUDIT_NAME,
    }
    if problems:
        notes["reason"] = "; ".join(problems)
    return {
        "lane": "bedrock",
        "outcome": outcome,
        "counts": {
            "searchable": status.searchable,
            "hits": len(found.hits),
            "rerank_status": found.rerank_status,
            "texts_sent_at_most": BEDROCK_FIXED_TEXTS,
            "texts_confirmed": texts,
            **audit_counts(audit),
        },
        "notes": notes,
    }


def export_bedrock_audit(store: Path, destination: Path) -> list[dict[str, Any]]:
    """Copy Palace's per-request audit records (one JSON object per line under the store's
    events/cloud-egress directory; no text, no credential) into one result artifact on the
    data volume, before the store's volume is destroyed with the instance. Returns the
    records; an absent directory yields none, which the verdict then refuses."""
    records: list[dict[str, Any]] = []
    audit_dir = store / "events" / "cloud-egress"
    lines: list[str] = []
    if audit_dir.is_dir():
        for file in sorted(audit_dir.glob("*.jsonl")):
            for line in file.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    records.append(json.loads(line))
                    lines.append(line)
    destination.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return records


@contextmanager
def exporting_audit(store: Path, destination: Path) -> Iterator[dict[str, Any]]:
    """Run the body, then export whatever audit the store holds, whether the body returned
    or raised. The body's exception propagates unchanged; an export failure is reported in
    the yielded mapping (``error``) rather than masking it."""
    exported: dict[str, Any] = {"records": [], "error": None}
    try:
        yield exported
    finally:
        try:
            exported["records"] = export_bedrock_audit(store, destination)
        except Exception as error:  # noqa: BLE001 - reported beside the body's own outcome
            exported["error"] = repr(error)


def bedrock_verdict(
    *, lagging: bool, hits: int, rerank_status: str, audit: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """Passed only when the index matched the shelf, the search hit, Palace's reranker was
    applied (fused order is a fallback, not a qualification), and the exported audit holds
    at least one successful retention check and one embedding request; every other case
    names its problems."""
    problems: list[str] = []
    if lagging:
        problems.append("the index did not match the shelf")
    if not hits:
        problems.append("the search returned no hit")
    if rerank_status != "applied":
        problems.append(f"rerank status {rerank_status!r}: the search returned fused order")
    problems.extend(audit_problems(audit))
    return ("passed" if not problems else "failed"), problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the qualification lanes on the instance.")
    parser.add_argument("--root", type=Path, required=True, help="the data volume mount point")
    parser.add_argument(
        "--checkout", type=Path, required=True, help="the staged repository checkout"
    )
    parser.add_argument("--corpus", type=Path, default=None)
    parser.add_argument("--bedrock-settings", type=Path, default=None)
    parser.add_argument("--bedrock-model", default=None, help="the confirmed model id")
    parser.add_argument("--bedrock-region", default=None, help="the confirmed region")
    parser.add_argument("--bedrock-texts", type=int, default=None, help="the confirmed count")
    parser.add_argument("--lanes", default="hermetic,ingestion,portability,bedrock")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-root-filesystem",
        action="store_true",
        help="local smoke of the lane code only; the record says waived",
    )
    args = parser.parse_args(argv)
    mount = assert_on_data_volume(args.root, allow_root_filesystem=args.allow_root_filesystem)
    env = dict(os.environ)
    env.setdefault("UV_OFFLINE", "1")
    records: list[dict[str, Any]] = []
    wanted = {name.strip() for name in args.lanes.split(",") if name.strip()}
    for name, runner in (
        ("hermetic", lambda: lane_hermetic(args.checkout, args.root, env)),
        ("ingestion", lambda: lane_ingestion(args.root, args.corpus)),
        ("portability", lambda: lane_portability(args.root)),
        (
            "bedrock",
            lambda: lane_bedrock(
                args.root,
                args.bedrock_settings,
                model=args.bedrock_model,
                region=args.bedrock_region,
                texts=args.bedrock_texts,
            ),
        ),
    ):
        if name not in wanted:
            continue
        try:
            records.append(runner())
        except SystemExit:
            raise
        except Exception as error:  # noqa: BLE001 - a lane failure is a recorded outcome
            records.append(
                {"lane": name, "outcome": "failed", "counts": {}, "notes": {"error": repr(error)}}
            )
    document = {"schema": "biblios.qualification-lanes/1", "mount": mount, "lanes": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(document, indent=1, sort_keys=True))
    print(json.dumps({r["lane"]: r["outcome"] for r in records}))
    return 0 if all(r["outcome"] in {"passed", "not-run"} for r in records) else 1


if __name__ == "__main__":
    sys.exit(main())
