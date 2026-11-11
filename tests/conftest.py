"""Synthetic qualifying runs use persistent storage; disposable fixtures clean up normally."""

import os
import tempfile
from pathlib import Path

import pytest

# Synthetic fixture repositories commit and tag; those commits must never invoke the
# operator's personal signing (an inherited `commit.gpgsign` made fixture setup fail
# whenever the signing agent refused). Appended as command-scope Git configuration, which
# outranks every configuration file, so global, user and repository settings are untouched.
UNSIGNED_FIXTURE_GIT = (("commit.gpgsign", "false"), ("tag.gpgsign", "false"))


def unsigned_git_overlay(environ) -> dict[str, str]:
    """The variables that append the unsigned settings after any existing entries."""
    start = int(environ.get("GIT_CONFIG_COUNT", "0"))
    overlay = {"GIT_CONFIG_COUNT": str(start + len(UNSIGNED_FIXTURE_GIT))}
    for offset, (key, value) in enumerate(UNSIGNED_FIXTURE_GIT):
        overlay[f"GIT_CONFIG_KEY_{start + offset}"] = key
        overlay[f"GIT_CONFIG_VALUE_{start + offset}"] = value
    return overlay


@pytest.fixture(autouse=True)
def _unsigned_fixture_git(monkeypatch):
    for name, value in unsigned_git_overlay(os.environ).items():
        monkeypatch.setenv(name, value)


@pytest.fixture
def tmp_path():
    root = Path(__file__).resolve().parents[1] / ".kickoff" / "proof-runtime"
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="case-", dir=root) as directory:
        yield Path(directory)
