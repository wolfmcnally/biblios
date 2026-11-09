"""The infra lane really runs the offline synthesis check and fails closed.

`./bin/check-infra` is the gate's `policy-infra` lane. This proof runs it as the gate does
and requires its pass line; the check's own planted mutants and planted context lookup are
the falsifiers behind that line. It also proves the lane refuses, with no fallback, when the
pinned Node cannot be found or reports another version.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_offline_synthesis_check_passes_with_credentials_withheld() -> None:
    """Failure mode: a stack set loses a safety property, or the lane stops running the check."""
    completed = subprocess.run(
        [str(REPO_ROOT / "bin" / "check-infra")],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output[-600:]
    assert "QUALIFICATION CHECK PASSED" in output, output[-600:]
    assert "cli-synthesis (external --output, templates present)" in output
    assert "planted-lookup in-process refused (StackAccountRegionNotSpecified)" in output
    assert "planted-lookup cli refused (no credentials for the fictitious account)" in output
    assert not (REPO_ROOT / "infra" / "cdk.out").exists()


def test_lane_refuses_an_absent_or_wrong_version_node(tmp_path: Path) -> None:
    """Failure mode: the lane falls back to an ambient Node when the pinned one is absent, or
    accepts a Node that exists but is not the pinned version."""
    environment = os.environ.copy()
    environment["BIBLIOS_NODE_BIN"] = str(tmp_path / "no-node")
    completed = subprocess.run(
        [str(REPO_ROOT / "bin" / "check-infra")],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "CHECK-INFRA ERROR BIBLIOS_NODE_BIN must be an absolute directory holding node" in (
        completed.stderr
    )
    assert "QUALIFICATION CHECK" not in completed.stdout
    wrong = tmp_path / "wrong-node"
    wrong.mkdir()
    (wrong / "node").write_text("#!/bin/sh\necho v1.0.0\n")
    (wrong / "node").chmod(0o755)
    environment["BIBLIOS_NODE_BIN"] = str(wrong)
    completed = subprocess.run(
        [str(REPO_ROOT / "bin" / "check-infra")],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert "CHECK-INFRA ERROR node at" in completed.stderr
    assert "reports 'v1.0.0', not the pinned" in completed.stderr
    assert "QUALIFICATION CHECK" not in completed.stdout
