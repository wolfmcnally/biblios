"""Synthetic fixture repositories never invoke personal commit or tag signing."""

import subprocess
from pathlib import Path

import pytest

from conftest import unsigned_git_overlay


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def test_fixture_commit_and_tag_never_call_the_signing_program(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    marker = tmp_path / "signing-was-invoked"
    signer = tmp_path / "refusing-signer"
    signer.write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
    signer.chmod(0o755)
    assert _git(root, "init", "-q", "-b", "master").returncode == 0
    # Repository-level settings demand signing through a program that always refuses;
    # the shared overlay must outrank them.
    for key, value in (
        ("user.email", "fixture@example.invalid"),
        ("user.name", "Fixture"),
        ("commit.gpgsign", "true"),
        ("tag.gpgsign", "true"),
        ("gpg.program", str(signer)),
    ):
        assert _git(root, "config", key, value).returncode == 0
    (root / "file.txt").write_text("synthetic\n")
    assert _git(root, "add", "file.txt").returncode == 0
    commit = _git(root, "commit", "-q", "-m", "fixture")
    assert commit.returncode == 0, f"fixture commit tried to sign: {commit.stderr}"
    tag = _git(root, "tag", "-a", "v0", "-m", "fixture")
    assert tag.returncode == 0, f"fixture tag tried to sign: {tag.stderr}"
    assert not marker.exists(), "the signing program was invoked"


def test_overlay_without_existing_entries_adds_exactly_the_unsigned_settings():
    assert unsigned_git_overlay({}) == {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": "commit.gpgsign",
        "GIT_CONFIG_VALUE_0": "false",
        "GIT_CONFIG_KEY_1": "tag.gpgsign",
        "GIT_CONFIG_VALUE_1": "false",
    }, "the overlay does not disable commit and tag signing exactly"


def test_overlay_appends_after_existing_command_scope_entries():
    overlay = unsigned_git_overlay({"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.directory"})
    assert overlay == {
        "GIT_CONFIG_COUNT": "3",
        "GIT_CONFIG_KEY_1": "commit.gpgsign",
        "GIT_CONFIG_VALUE_1": "false",
        "GIT_CONFIG_KEY_2": "tag.gpgsign",
        "GIT_CONFIG_VALUE_2": "false",
    }, "existing GIT_CONFIG entries were not preserved"


def test_malformed_existing_count_fails_loudly():
    with pytest.raises(ValueError):
        unsigned_git_overlay({"GIT_CONFIG_COUNT": "not-a-number"})
