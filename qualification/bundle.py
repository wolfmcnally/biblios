"""Build the staged bundle on a Linux builder and prove it installs and runs offline.

The instance in the private shape has no internet, so everything the lanes need travels in
one archive: the repository at the pinned commit, the managed Python runtime, the `uv`
executable, a cache pre-seeded by `uv sync --locked` for the product and for the Vellric
sub-project under `qualification/vellric` (the git-sourced dependencies resolve into it),
Palace's pinned cross-encoder reranker model provisioned by Palace's own tool (so search on
the private instance reranks instead of falling back to fused order),
the operator's corpus and, when supplied, a tools directory of prebuilt executables for the
instance's distribution (git, which uv needs for its git-pinned sources; `file`; Tesseract,
which Amazon Linux 2023 does not package; each with its libraries). The archive
preserves modes and links, which per-file object uploads would not; the instance extracts
it and runs the same install lines and lane script the proof ran.

`build` seeds the staging directory in a container of the instance's architecture with
network access, then packs it into `bundle.tar` beside a manifest. `prove` extracts that
archive in a container of the instance's own distribution (Amazon Linux 2023 plus only the
archiver the image also ships) with the network disabled, installs the product and Vellric
environments from the staged cache and runs the hermetic and portability lanes through
`lanes.py`, plus the ingestion lane when the bundle carries a corpus. The proof venue is
the instance's distribution on purpose: a host tool the bundle forgot (git, which uv needs
to materialize its git-pinned sources even from the cache; `file`; Tesseract) fails the
proof here rather than the run there. The proof is an acceptance item and a prerequisite
of the first deploy; it makes no AWS call. Both need Docker on the builder.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parent.parent
# The project lockfile requires uv 0.9.26 or newer and git for its git sources; the
# non-slim image carries both.
BUILDER_IMAGE = "ghcr.io/astral-sh/uv:0.9.26-python3.12-bookworm"
# The proof venue: the instance's distribution, bare, plus the archiver the AL2023 image
# ships and the container image lacks. Built once per builder from this recipe, with network.
PROOF_BASE_IMAGE = "public.ecr.aws/amazonlinux/amazonlinux:2023"
PROOF_IMAGE = "biblios-qualification-proof:al2023"
PROOF_IMAGE_RECIPE = f"FROM {PROOF_BASE_IMAGE}\nRUN dnf install -y -q tar gzip && dnf clean all\n"
PLATFORMS = {"arm64": "linux/arm64", "x86_64": "linux/amd64"}
CACHE = "cache"
RUNTIME = "python"
MODELS = "models"
CHECKOUT = "checkout"
ARCHIVE = "bundle.tar"
TOOLS = "tools"
VELLRIC_PROJECT = "qualification/vellric"
PROOF_LANES = "hermetic,portability"
# The offline reranker probe the proof executes from the extracted bundle: constructs
# Palace's reranker from the bundled model directory, probes it and scores two texts, so a
# bundle whose model is absent or unloadable fails the proof before any instance exists.
RERANK_PROBE = """\
from palace.rerank import CrossEncoderReranker
from palace.retrieval_config import rerank_model_dir

reranker = CrossEncoderReranker(model_dir=rerank_model_dir())
reranker.probe()
scores = reranker.score("lighthouse lens", ["a fresnel lens", "a tide table"])
print("reranker loaded offline from", rerank_model_dir(), scores)
"""
# Shims the bundle writes into bin/: each resolves its target relative to the bundle root,
# so the archive works wherever it is extracted. Vellric's target exists only after the
# instance (or the proof) has synced its locked sub-project from the staged cache.
SHIM = '#!/bin/sh\nexec "$(dirname "$0")/../{target}" "$@"\n'

SEED = r"""
set -euo pipefail
export UV_CACHE_DIR=/bundle/{cache}
export UV_PYTHON_INSTALL_DIR=/bundle/{runtime}
cd /bundle/{checkout}
uv python install --managed-python "$(cat project/.python-version)"
uv sync --project project --locked --managed-python
uv sync --project {vellric} --locked --managed-python
PALACE_MODELS_DIR=/bundle/{models} ./bin/python -m palace.rerank_model
mkdir -p /bundle/bin
cp "$(command -v uv)" /bundle/bin/uv
rm -rf project/.venv {vellric}/.venv
"""

# The same route the instance takes (see qualification/runner.py lane_commands): extract
# with modes kept but ownership taken by the extracting user (git refuses a repository
# owned by someone else, which would make uv try to fetch), install the product and
# Vellric environments from the staged cache, report the tools, run the lanes. Every tool
# named here must come from the bundle's own bin/: the venue has none of them.
PROVE = r"""
set -euo pipefail
mkdir -p /work && tar --no-same-owner -xpf /bundle/{archive} -C /work
cd /work
export PATH=/work/bin:$PATH UV_OFFLINE=1 UV_CACHE_DIR=/work/{cache}
export UV_PYTHON_INSTALL_DIR=/work/{runtime} PALACE_MODELS_DIR=/work/{models}
[ -x bin/vellric ] && export DRAWBRIDGE_VELLRIC=/work/bin/vellric || true
for tool in git file tesseract; do command -v "$tool" || echo "$tool absent"; done
git --version && file --version | head -1 && tesseract --version 2>&1 | head -1
cd {checkout}
uv sync --project project --locked --managed-python
uv sync --project {vellric} --locked --managed-python
[ -n "${{DRAWBRIDGE_VELLRIC:-}}" ] && "$DRAWBRIDGE_VELLRIC" --version || echo "vellric absent"
./bin/python - <<'RERANK'
{probe}
RERANK
./bin/python qualification/lanes.py --root /work/volume --checkout . --lanes {lanes} \
  {corpus} --allow-root-filesystem --output /work/lanes.json
cat /work/lanes.json
"""


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def git_head() -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPOSITORY, capture_output=True, text=True, check=True
    ).stdout.strip()


def worktree_tree() -> str:
    """A tree object of the working tree as it stands (tracked and unignored files), written
    through a throwaway index so the repository's own index is untouched. For development
    proofs of the bundle route only; a qualification run refuses such a bundle."""
    index = Path(tempfile.mkdtemp(prefix="biblios-bundle-index-")) / "index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index))
    subprocess.run(["git", "read-tree", "HEAD"], cwd=REPOSITORY, env=env, check=True)
    subprocess.run(["git", "add", "-A", "."], cwd=REPOSITORY, env=env, check=True)
    tree = subprocess.run(
        ["git", "write-tree"], cwd=REPOSITORY, env=env, capture_output=True, text=True, check=True
    ).stdout.strip()
    return tree


def export_checkout(target: Path, *, source: str) -> str:
    """The repository as git holds it: at HEAD (`commit`), so the working tree's uncommitted
    state does not travel, or as a tree of the working tree (`worktree`) for a development
    proof. Returns the id the manifest records."""
    revision = git_head() if source == "commit" else worktree_tree()
    target.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", revision],
        cwd=REPOSITORY,
        capture_output=True,
        check=True,
    )
    subprocess.run(["tar", "-x", "-C", str(target)], input=archive.stdout, check=True)
    return revision


def docker(args: list[str], *, log: Path, stdin: str | None = None) -> int:
    with log.open("ab") as handle:
        handle.write(("$ docker " + " ".join(args) + "\n").encode())
        handle.flush()
        completed = subprocess.run(
            ["docker", *args],
            input=stdin.encode() if stdin is not None else None,
            stdout=handle,
            stderr=subprocess.STDOUT,
            check=False,
        )
    return completed.returncode


def ensure_proof_image(arch: str, *, log: Path) -> None:
    """The proof venue image, built from its recipe when the builder does not hold it yet;
    the one step of the proof that reaches the network, and never during the proof itself."""
    if docker(["image", "inspect", PROOF_IMAGE], log=log) == 0:
        return
    code = docker(
        ["build", "--platform", PLATFORMS[arch], "-t", PROOF_IMAGE, "-"],
        log=log,
        stdin=PROOF_IMAGE_RECIPE,
    )
    if code != 0:
        raise SystemExit(
            f"REFUSED: the proof venue image could not be built (exit {code}); see {log}"
        )


def pack(stage: Path, archive: Path) -> None:
    """One tar of the staging directory, modes and long links preserved (GNU format: no
    pax extended headers for the instance's tar to complain about), no platform sidecars."""
    env = dict(os.environ, COPYFILE_DISABLE="1")
    subprocess.run(
        ["tar", "--format=gnutar", "-cf", str(archive), "-C", str(stage), "."],
        env=env,
        check=True,
    )


def write_manifest(
    bundle: Path,
    stage: Path,
    archive: Path,
    commit: str,
    arch: str,
    corpus_files: int,
    *,
    source: str,
) -> Path:
    files = {}
    for path in sorted(p for p in stage.rglob("*") if p.is_file()):
        files[str(path.relative_to(stage))] = {
            "sha256": sha256_of(path),
            "bytes": path.stat().st_size,
        }
    manifest = {
        "schema": "biblios.qualification-bundle/2",
        "commit": commit,
        "source": source,
        "architecture": arch,
        "builder_image": BUILDER_IMAGE,
        "archive": {
            "name": archive.name,
            "sha256": sha256_of(archive),
            "bytes": archive.stat().st_size,
        },
        "corpus_files": corpus_files,
        "tools": sorted(
            p.name for p in (stage / "bin").iterdir() if p.is_file() and p.name != "uv"
        ),
        "files": files,
    }
    out = bundle / "manifest.json"
    out.write_text(json.dumps(manifest, indent=1, sort_keys=True))
    return out


def write_shim(stage: Path, name: str, target: str) -> None:
    """A `bin/<name>` that execs `<bundle root>/<target>`, resolved from its own location."""
    shim = stage / "bin" / name
    shim.write_text(SHIM.format(target=target))
    shim.chmod(0o755)


def build(
    bundle: Path,
    *,
    arch: str,
    corpus: Path | None,
    tools: Path | None,
    log: Path,
    source: str = "commit",
) -> int:
    if bundle.exists() and any(bundle.iterdir()):
        raise SystemExit(f"REFUSED: bundle directory is not empty: {bundle}")
    bundle.mkdir(parents=True, exist_ok=True)
    stage = bundle / "stage"
    stage.mkdir()
    commit = export_checkout(stage / CHECKOUT, source=source)
    corpus_files = 0
    if corpus is not None:
        shutil.copytree(corpus, stage / "corpus")
        corpus_files = sum(1 for p in (stage / "corpus").rglob("*") if p.is_file())
    (stage / "bin").mkdir(exist_ok=True)
    write_shim(stage, "vellric", f"{CHECKOUT}/{VELLRIC_PROJECT}/.venv/bin/vellric")
    if tools is not None:
        shutil.copytree(tools, stage / TOOLS, symlinks=True)
        for executable in sorted((stage / TOOLS).glob("*/bin/*")):
            if executable.is_file() and os.access(executable, os.X_OK):
                write_shim(stage, executable.name, str(executable.relative_to(stage)))
    script = SEED.format(
        cache=CACHE, runtime=RUNTIME, checkout=CHECKOUT, vellric=VELLRIC_PROJECT, models=MODELS
    )
    code = docker(
        [
            "run",
            "--rm",
            "--platform",
            PLATFORMS[arch],
            "-v",
            f"{stage}:/bundle",
            BUILDER_IMAGE,
            "bash",
            "-c",
            script,
        ],
        log=log,
    )
    if code != 0:
        print(f"BUNDLE BUILD FAILED (exit {code}); see {log}", file=sys.stderr)
        return code
    archive = bundle / ARCHIVE
    pack(stage, archive)
    manifest = write_manifest(bundle, stage, archive, commit, arch, corpus_files, source=source)
    shutil.rmtree(stage)
    print(
        f"BUNDLE BUILT {bundle} commit {commit[:12]} archive {archive.name} "
        f"({archive.stat().st_size} bytes) manifest {manifest.name}"
    )
    return 0


def prove(bundle: Path, *, arch: str, log: Path) -> int:
    """Extract the archive in the instance's distribution, install and run the lanes from it
    alone with the network disabled; the lane exit status is the proof's."""
    manifest = json.loads((bundle / "manifest.json").read_text())
    if manifest.get("architecture") != arch:
        raise SystemExit("REFUSED: bundle architecture does not match the requested proof")
    archive = bundle / ARCHIVE
    if sha256_of(archive) != manifest["archive"]["sha256"]:
        raise SystemExit("REFUSED: bundle.tar does not match its manifest")
    with_corpus = bool(manifest.get("corpus_files"))
    script = PROVE.format(
        archive=ARCHIVE,
        cache=CACHE,
        runtime=RUNTIME,
        checkout=CHECKOUT,
        vellric=VELLRIC_PROJECT,
        models=MODELS,
        probe=RERANK_PROBE.rstrip("\n"),
        lanes=PROOF_LANES + (",ingestion" if with_corpus else ""),
        corpus="--corpus /work/corpus" if with_corpus else "",
    )
    ensure_proof_image(arch, log=log)
    code = docker(
        [
            "run",
            "--rm",
            "--network",
            "none",
            "--platform",
            PLATFORMS[arch],
            "-v",
            f"{bundle}:/bundle:ro",
            PROOF_IMAGE,
            "bash",
            "-c",
            script,
        ],
        log=log,
    )
    print("BUNDLE PROOF PASSED" if code == 0 else f"BUNDLE PROOF FAILED (exit {code}); see {log}")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build and prove the offline qualification bundle."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    b = sub.add_parser("build")
    b.add_argument("--bundle", type=Path, required=True)
    b.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    b.add_argument("--corpus", type=Path, default=None)
    b.add_argument(
        "--tools",
        type=Path,
        default=None,
        help="a directory of prebuilt tool trees for the instance's distribution; every "
        "executable under <tools>/*/bin/ gets a bin/ shim in the bundle",
    )
    b.add_argument("--log", type=Path, required=True)
    b.add_argument(
        "--source",
        choices=("commit", "worktree"),
        default="commit",
        help="commit: the repository at HEAD (the only source a qualification run accepts); "
        "worktree: the working tree as it stands, for a development proof of the route",
    )
    p = sub.add_parser("prove")
    p.add_argument("--bundle", type=Path, required=True)
    p.add_argument("--arch", choices=sorted(PLATFORMS), default="arm64")
    p.add_argument("--log", type=Path, required=True)
    args = parser.parse_args(argv)
    if shutil.which("docker") is None:
        raise SystemExit("REFUSED: docker is required on the builder")
    if args.command == "build":
        return build(
            args.bundle,
            arch=args.arch,
            corpus=args.corpus,
            tools=args.tools,
            log=args.log,
            source=args.source,
        )
    return prove(args.bundle, arch=args.arch, log=args.log)


if __name__ == "__main__":
    sys.exit(main())
