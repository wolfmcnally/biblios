"""Public documentation boundaries: curated inputs, built links, and release custody.

Admission: no existing proof exercises publication. Fixtures have one chapter and
four generated affordances; each scenario invokes one in-process boundary, with
no network, compiler installation, persistent server, or external API call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))

from agentic_starter import documentation as docs


def project(root: Path) -> dict:
    settings = {
        "tools": {"mdbook": "0.4.50", "admonish": "1.19.0", "rust": "1.91.1"},
        "book": {"directory": "manual"},
        "preview": {"port": 3017},
        "deploy": {
            "repository": "example/public-product",
            "release_branch": "master",
            "workflow": "docs-pages.yml",
            "url": "https://example.github.io/public-product/",
        },
    }
    (root / "manual/src").mkdir(parents=True)
    (root / "manual/src/SUMMARY.md").write_text("# Summary\n\n- [Welcome](index.md)\n")
    (root / "manual/src/index.md").write_text("# Welcome\n\nPublic product instructions.\n")
    (root / "manual/book.toml").write_text(
        '[book]\nsrc="src"\n[build]\ncreate-missing=false\n'
        '[output.html]\nsite-url="/public-product/"\n'
        "[output.html.playground]\nrunnable=false\n"
    )
    return settings


def rendered(root: Path) -> None:
    root.mkdir(exist_ok=True)
    for name in ("index.html", "404.html", "print.html"):
        (root / name).write_text(
            '<h1 id="welcome">Welcome</h1><a href="index.html#welcome">Home</a>'
        )
    (root / "searchindex.js").write_text('window.search = "public instructions";')


def test_curated_source_refuses_escape_and_private_inputs(tmp_path: Path) -> None:
    """A tempting operational import cannot become publication input."""
    settings = project(tmp_path)
    assert docs.sources(tmp_path, settings) == tmp_path / "manual"
    chapter = tmp_path / "manual/src/index.md"
    for content in (
        "{{#include ../../LOG.md}}",
        "<script>alert(1)</script>",
        "Open file:///private/notes",
        "Saved under /Users/user/private",
    ):
        chapter.write_text(content)
        with pytest.raises(ValueError):
            docs.sources(tmp_path, settings)
    chapter.write_text("# Welcome\n")
    extra = tmp_path / "manual/src/.env"
    extra.write_text("not a public chapter")
    with pytest.raises(ValueError, match="unlisted publication input"):
        docs.sources(tmp_path, settings)
    extra.unlink()
    chapter.unlink()
    chapter.symlink_to(tmp_path / "LOG.md")
    with pytest.raises(ValueError, match="symlink refused"):
        docs.sources(tmp_path, settings)


def test_generated_output_checks_links_assets_anchors_and_private_paths(
    tmp_path: Path,
) -> None:
    """Generated search/print and nested assets remain inside the checked site."""
    rendered(tmp_path)
    docs.output_check(tmp_path, "/public-product/")
    not_found = tmp_path / "404.html"
    original_404 = not_found.read_text()
    not_found.write_text(
        '<base href="/public-product/"><h1 id="only404">Missing page</h1>'
        '<a href="#only404">Heading</a>'
    )
    with pytest.raises(ValueError, match="broken anchor"):
        docs.output_check(tmp_path, "/public-product/")
    not_found.write_text(original_404)
    index = tmp_path / "index.html"
    original = index.read_text()
    for link, expected in (
        ("missing.html", "broken local link"),
        ("index.html#absent", "broken anchor"),
        ("../outside.html", "escapes site prefix"),
        ("/other-site/", "escapes site prefix"),
    ):
        index.write_text(f'<h1 id="welcome">Welcome</h1><a href="{link}">Destination</a>')
        with pytest.raises(ValueError, match=expected):
            docs.output_check(tmp_path, "/public-product/")
    index.write_text(original)
    search = tmp_path / "searchindex.js"
    search.write_text('"/Users/user/private"')
    with pytest.raises(ValueError, match="private/local material"):
        docs.output_check(tmp_path, "/public-product/")
    search.write_text("public instructions")
    (tmp_path / "LOG.md").write_text("internal history")
    with pytest.raises(ValueError, match="unapproved output file"):
        docs.output_check(tmp_path, "/public-product/")


def test_deploy_binds_clean_latest_release_and_preserves_manual_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No dispatch or variable write occurs before release custody is proved."""
    settings = project(tmp_path)
    monkeypatch.setattr(docs, "config", lambda root: settings)
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / ".github/workflows/docs-pages.yml").write_text("approved workflow fixture")
    calls = []
    variables = {}
    answers = {
        ("git", "status", "--porcelain"): "",
        ("git", "branch", "--show-current"): "master",
        (
            "git",
            "remote",
            "get-url",
            "origin",
        ): "https://github.com/example/public-product.git",
        ("git", "rev-parse", "HEAD"): "candidate-a",
        (
            "git",
            "ls-remote",
            "--exit-code",
            "origin",
            "refs/heads/master",
        ): "candidate-a refs/heads/master",
    }

    def command(args: list[str], **kwargs: object) -> str:
        calls.append(args)
        if args[:3] == ["gh", "variable", "set"]:
            variables[args[3]] = args[-1]
        if args[:2] == ["gh", "api"]:
            return json.dumps({"value": variables.get(args[-1].split("/")[-1])})
        return answers.get(tuple(args), "")

    monkeypatch.setattr(docs, "run", command)
    monkeypatch.setattr(docs, "build", lambda root, config: tmp_path)
    for query, refusal in (
        (("git", "status", "--porcelain"), " M manual/src/index.md"),
        (("git", "branch", "--show-current"), "feature"),
        (
            ("git", "remote", "get-url", "origin"),
            "https://github.com/example/private-project.git",
        ),
        (
            ("git", "ls-remote", "--exit-code", "origin", "refs/heads/master"),
            "candidate-b refs/heads/master",
        ),
    ):
        original = answers[query]
        answers[query] = refusal
        calls.clear()
        assert docs.main(["deploy"], root=tmp_path) == 1
        assert not any(call[0] == "gh" for call in calls), "unsafe release reached GitHub"
        answers[query] = original
    assert docs.main(["deploy", "continuous", "off"], root=tmp_path) == 0
    assert any(
        call[:4] == ["gh", "variable", "set", "DOCS_CONTINUOUS"] and call[-1] == "off"
        for call in calls
    )
    calls.clear()
    assert docs.main(["deploy"], root=tmp_path) == 0
    assert calls[-1] == [
        "gh",
        "workflow",
        "run",
        "docs-pages.yml",
        "--repo",
        "example/public-product",
        "--ref",
        "master",
        "-f",
        "candidate=candidate-a",
    ]


def test_setup_installs_exact_versions_locally_without_global_upgrade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mismatched global binaries stay untouched; locked pins go to local storage."""
    settings = project(tmp_path)
    installed = {}
    calls = []
    monkeypatch.setattr(
        docs.shutil, "which", lambda name: "/usr/bin/cargo" if name == "cargo" else None
    )

    def command(args: list[str], **kwargs: object) -> str:
        calls.append(args)
        if "install" in args:
            assert args[1] == "+1.91.1"
            assert "--locked" in args
            install_root = Path(args[-1])
            assert install_root.is_relative_to(tmp_path / ".kickoff/docs-tools")
            assert install_root != tmp_path / ".kickoff/docs-tools", "platform tool caches collided"
            name = args[3]
            version = args[args.index("--version") + 1]
            assert version.startswith("="), "tool installation floated"
            local = install_root / "bin" / name
            local.parent.mkdir(parents=True, exist_ok=True)
            local.write_text("local binary fixture")
            installed[str(local)] = version[1:]
            return "installed"
        return Path(args[0]).name + " " + installed[args[0]]

    monkeypatch.setattr(docs, "run", command)
    selected = docs.tools(tmp_path, settings, install=True)
    assert set(selected) == {"mdbook", "mdbook-admonish"}
    assert len([call for call in calls if "install" in call]) == 2
    calls.clear()
    docs.tools(tmp_path, settings, install=True)
    assert not any("install" in call for call in calls), (
        "exact installed tools were unnecessarily rebuilt"
    )


def test_release_ci_requires_committed_publication_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A workflow dispatched on the wrong branch cannot publish a build."""
    settings = project(tmp_path)
    monkeypatch.setattr(docs, "config", lambda root: settings)
    builds = []
    monkeypatch.setattr(docs, "build", lambda *args: builds.append(args))
    monkeypatch.setenv("GITHUB_REPOSITORY", "example/public-product")
    monkeypatch.setenv("GITHUB_REF_NAME", "feature")
    assert docs.main(["check", "--release"], root=tmp_path) == 1
    assert builds == [], "wrong CI identity reached publication build"
    monkeypatch.setenv("GITHUB_REF_NAME", "master")
    assert docs.main(["check", "--release"], root=tmp_path) == 0
    assert len(builds) == 1
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("DOCS_VALIDATED_COMMIT", "expected-candidate")
    monkeypatch.setattr(docs, "run", lambda *args, **kwargs: "different-candidate")
    assert docs.main(["check", "--release"], root=tmp_path) == 1
    assert len(builds) == 1, "unvalidated dispatched commit reached publication build"
