"""Palace's cloud-egress audit records as the qualification reads them.

One home for the judgment three surfaces share: the Bedrock lane on the instance (its
verdict), the runner's judge on the operator's machine (the retained artifact) and the
results renderer (what the brief may claim). A record is one JSON object per line as
Palace appends it under the store's events/cloud-egress directory: kind
``bedrock_retention_check`` or ``cloud_embed_call``, a status, the model and dimension,
timing; never text and never a credential.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

AUDIT_NAME = "bedrock-audit.jsonl"


def read_audit(path: Path) -> list[dict[str, Any]]:
    """Every record in the artifact; an absent or empty file is no records. Malformed
    lines raise, so a corrupt artifact is never mistaken for an empty one."""
    if not path.is_file():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def audit_problems(records: list[dict[str, Any]]) -> list[str]:
    """What a complete audit must hold: at least one successful retention check and at
    least one embedding request. Each absence is one named problem."""
    problems: list[str] = []
    checks = [
        r for r in records if r.get("kind") == "bedrock_retention_check" and r.get("status") == "ok"
    ]
    calls = [r for r in records if r.get("kind") == "cloud_embed_call"]
    if not checks:
        problems.append("no successful retention check in the exported audit")
    if not calls:
        problems.append("no embedding request in the exported audit")
    return problems


def audit_counts(records: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "audit_records": len(records),
        "audit_retention_checks": sum(
            1 for r in records if r.get("kind") == "bedrock_retention_check"
        ),
        "audit_embed_calls": sum(1 for r in records if r.get("kind") == "cloud_embed_call"),
    }
