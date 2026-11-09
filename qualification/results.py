"""Render the sanitized results brief from a run's private evidence directory.

The evidence directory holds everything a run observed: the runner's journal (one JSON line
per call with its arguments and the identifiers the response carried), the outputs files,
the lane document and the inventory. None of that reaches the tree. This module reads the
journal's identifiers into a denylist, renders the brief from the lane document and the
journal's venue classes, and refuses to write a brief in which any denylisted string
survives. The denylist file is kept beside the evidence so a later check can read it.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from audit import AUDIT_NAME, audit_problems, read_audit

IDENTIFIER_KEYS = {
    "Account",
    "AccountId",
    "Arn",
    "BucketName",
    "InstanceId",
    "InstanceProfileName",
    "InstanceRoleName",
    "SubnetId",
    "InstanceGroupId",
    "VpcId",
    "ImageId",
    "StackId",
    "RunId",
    "Region",
    "UserId",
    "CommandId",
    # the runner's packets use lower-case keys
    "account",
    "region",
    "profile",
    "run_id",
}
IDENTIFIER_SHAPES = (
    re.compile(r"\b\d{12}\b"),
    re.compile(r"\b(?:i|ami|subnet|sg|vpc|vpce|vol|igw|rtb|eni)-[0-9a-f]{8,17}\b"),
    re.compile(r"\barn:aws[a-z-]*:[^\s\"']+"),
    re.compile(r"\b(?:us|eu|ap|sa|ca|me|af|il)-[a-z]+-\d\b"),
)


def _walk(value: Any) -> Iterable[tuple[str | None, Any]]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def denylist_from(journal: Path) -> set[str]:
    """Every identifier the journal recorded, by key name and by shape."""
    found: set[str] = set()
    if not journal.is_file():
        return found
    for line in journal.read_text().splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        for key, value in _walk(record):
            if key in IDENTIFIER_KEYS and isinstance(value, str) and value.strip():
                found.add(value.strip())
        text = json.dumps(record)
        for shape in IDENTIFIER_SHAPES:
            found.update(shape.findall(text))
    return found


def sanitize(text: str, denylist: set[str]) -> str:
    for item in sorted(denylist, key=len, reverse=True):
        text = text.replace(item, "<redacted>")
    for shape in IDENTIFIER_SHAPES:
        text = shape.sub("<redacted>", text)
    return text


def audit_retained(evidence: Path) -> str:
    """What the run actually holds off the instance: ``complete`` when the collector recorded
    the audit artifact as retained, the file is here and it passes the same judgment the
    runner applies; ``incomplete`` when it is here but fails that judgment; ``absent``
    otherwise. The lane's own counts never decide this."""
    collected_file = evidence / "collected.json"
    collected = json.loads(collected_file.read_text()) if collected_file.is_file() else {}
    artifact = evidence / AUDIT_NAME
    if not (collected.get(AUDIT_NAME) and artifact.is_file()):
        return "absent"
    return "complete" if not audit_problems(read_audit(artifact)) else "incomplete"


def audit_retention_sentence(evidence: Path) -> str:
    state = audit_retained(evidence)
    if state == "complete":
        records = read_audit(evidence / AUDIT_NAME)
        return (
            f"Retained with the run: yes, {len(records)} records in {AUDIT_NAME}, judged "
            "complete (a successful retention check and an embedding request present)."
        )
    if state == "incomplete":
        problems = "; ".join(audit_problems(read_audit(evidence / AUDIT_NAME)))
        return (
            f"Retained with the run: {AUDIT_NAME} is here but incomplete ({problems}); the "
            "runner's judge refuses the run and this is not request-level evidence."
        )
    return (
        f"Retained with the run: no; {AUDIT_NAME} was not collected off the instance, the "
        "runner's judge refuses the run, and no request-level evidence exists off the volume."
    )


def render(evidence: Path, *, as_of: str | None = None) -> str:
    """The brief body: venue classes and measurements only."""
    lanes = json.loads((evidence / "lanes.json").read_text())
    journal = evidence / "journal.jsonl"
    denylist = denylist_from(journal)
    (evidence / "denylist.txt").write_text("\n".join(sorted(denylist)) + ("\n" if denylist else ""))
    shape = "private (no internet, Session Manager over interface endpoints)"
    if journal.is_file() and '"example"' in journal.read_text():
        shape = "private, with the low-security example also exercised"
    date = as_of or datetime.now(UTC).date().isoformat()
    lines = [
        "---",
        'title: "AWS qualification results"',
        "status: results",
        f"as_of: {date}",
        'scope: "What one approved run of the qualification lanes on EC2 with the shelves on EBS '
        "established, in venue classes and measurements only; no account, region or resource "
        'identifier."',
        "---",
        "",
        "# AWS qualification results",
        "",
        f"As of {date}. Rendered by `qualification/results.py` from a private evidence directory "
        f"outside the tree; every identifier the run observed is on that directory's denylist "
        f"and absent here by construction. Venue: one current-generation instance in the "
        f"{shape} shape, one gp3 data volume at the account's default encryption setting "
        f"mounted as the shelves' filesystem.",
        "",
        "## Mount",
        "",
        f"- Data volume device marker present: "
        f"{'yes' if lanes['mount'].get('device_marker') else 'no'}; assertion waived: "
        f"{'yes' if lanes['mount'].get('data_volume_waived') else 'no'}. A waived assertion "
        f"means the lanes did not run on the data volume and this document is not an EBS "
        f"qualification.",
        "",
        "## Lanes",
        "",
    ]
    for lane in lanes["lanes"]:
        lines.append(f"### {lane['lane']}: {lane['outcome']}")
        lines.append("")
        counts = lane.get("counts") or {}
        if counts:
            lines.append(
                "- Counts: " + ", ".join(f"{k} {json.dumps(v)}" for k, v in counts.items()) + "."
            )
        notes = lane.get("notes") or {}
        reason = notes.get("reason")
        if reason:
            lines.append(f"- Not run: {reason}.")
        error = notes.get("error")
        if error:
            lines.append(f"- Recorded failure: {error}.")
        problems = notes.get("verify_problems")
        if problems:
            lines.append("- Verification problems: " + "; ".join(problems) + ".")
        if lane["lane"] == "bedrock" and "audit_records" in counts:
            lines.append(
                f"- Exported on the instance ({notes.get('audit_artifact', AUDIT_NAME)}, the "
                f"lane's own count): {counts['audit_records']} records, "
                f"{counts['audit_retention_checks']} retention checks, "
                f"{counts['audit_embed_calls']} embedding requests; rerank status "
                f"{counts.get('rerank_status', 'unrecorded')}."
            )
            lines.append("- " + audit_retention_sentence(evidence))
        tools = notes.get("tools_present")
        if tools:
            lines.append(
                "- Tools present on the instance: "
                + ", ".join(f"{k} {'yes' if v else 'no'}" for k, v in tools.items())
                + "."
            )
        lines.append("")
    lines.append("## Limits")
    lines.append("")
    lines.append(
        "- A `not-run` lane establishes nothing. The Bedrock lane reports `not-run` until the "
        "shelf's declared provider is Palace's `bedrock`, and a passed Bedrock lane records "
        "that the account's retention mode read `none` at that moment, not that nothing was "
        "retained."
    )
    if audit_retained(evidence) == "complete":
        lines.append(
            "- A passed Bedrock lane's request-level evidence is the exported audit artifact "
            "retained with the run and judged complete (Palace's per-request records: kind, "
            "status, model, dimension, timing; no text, no credential; at least one successful "
            "retention check and one embedding request); the confirmed text count remains the "
            "bound Palace enforces from the lane's fixed input."
        )
    else:
        lines.append(
            "- A passed Bedrock lane's evidence is its lane record: the confirmed model and "
            "region, the searchable count and hits, and the confirmed text count, which is the "
            "bound Palace enforces from the lane's fixed input, not an observed request count. "
            "Palace's per-request audit records stay in the store on the data volume and are "
            "not collected."
        )
    lane_log = evidence / "lanes.log"
    if lane_log.is_file() and "rerank failed" in lane_log.read_text():
        lines.append(
            "- Reranking is not qualified. Palace could not fetch its cross-encoder reranker "
            "model on the instance (no network) and every search in these lanes returned the "
            "fused keyword-plus-vector order; a passed search lane speaks for fused order only."
        )
    lines.append(
        "- Teardown is recorded in the evidence directory's inventory, not here; absence is proved "
        "by the tag-scoped inventory read back after destroy."
    )
    body = "\n".join(lines) + "\n"
    rendered = sanitize(body, denylist)
    for item in denylist:
        if item and item in rendered:
            raise SystemExit("REFUSED: a denylisted identifier survived sanitization")
    return rendered


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render the sanitized results brief.")
    parser.add_argument("--evidence", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--as-of", default=None)
    args = parser.parse_args(argv)
    args.output.write_text(render(args.evidence, as_of=args.as_of))
    print(f"rendered {args.output}")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
