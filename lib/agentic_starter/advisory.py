"""Independent observations and primary decisions, without approval semantics."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
from typing import Any


class AdvisoryError(ValueError):
    """Incomplete or contradictory advisory evidence."""


def read(path: Path, default: Any = None) -> Any:
    if not path.exists() and default is not None:
        return default
    return json.loads(path.read_text())


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def check_report(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"summary", "findings"}:
        raise AdvisoryError("advice requires summary/findings, never a verdict")
    if not isinstance(value["summary"], str) or not isinstance(value["findings"], list):
        raise AdvisoryError("invalid advisory report")
    seen = set()
    for item in value["findings"]:
        if not isinstance(item, dict) or set(item) != {
            "id",
            "severity",
            "affected_paths",
            "evidence",
            "consequence",
            "suggestion",
        }:
            raise AdvisoryError("advisory finding has unknown or missing fields")
        for key in ("id", "evidence", "consequence", "suggestion"):
            if not isinstance(item[key], str) or not item[key].strip():
                raise AdvisoryError(f"advisory {key} must be nonempty")
        if item["id"] in seen:
            raise AdvisoryError("duplicate advisory finding")
        seen.add(item["id"])
        if item["severity"] not in {
            "critical",
            "high",
            "medium",
            "low",
            "informational",
        }:
            raise AdvisoryError("invalid advisory severity; blocking is not advisory")
        if not isinstance(item["affected_paths"], list) or any(
            not isinstance(p, str) or not p or Path(p).is_absolute() or ".." in Path(p).parts
            for p in item["affected_paths"]
        ):
            raise AdvisoryError("advisory paths must be repository-relative")
    return value


def budget_path(root: Path, phase: str) -> Path:
    return (
        root
        / ".kickoff"
        / "advice-budgets"
        / (hashlib.sha256(phase.encode()).hexdigest() + ".jsonl")
    )


def _records(text: str) -> list[dict[str, Any]]:
    try:
        rows = [json.loads(line) for line in text.splitlines() if line.strip()]
    except json.JSONDecodeError as exc:
        raise AdvisoryError("invalid advisory ledger JSON") from exc
    if any(not isinstance(row, dict) for row in rows):
        raise AdvisoryError("invalid advisory ledger record")
    return rows


def validate_budget(root: Path, phase: str, text: str) -> list[dict[str, Any]]:
    """Replay exceptions and claims against their original append-only prefix."""
    rows = _records(text)
    permits: dict[str, dict[str, Any]] = {}
    spent: set[str] = set()
    prefix = ""
    for row in rows:
        if "exception" in row:
            permit = row["exception"]
            if not isinstance(permit, dict):
                raise AdvisoryError("invalid exception receipt")
            identifier = digest(permit)
            if (
                set(row) != {"exception", "exception_sha256"}
                or row["exception_sha256"] != identifier
            ):
                raise AdvisoryError("exception receipt digest mismatch")
            expected = {
                "repository",
                "phase",
                "operation",
                "run",
                "registration_sha256",
                "candidate_id",
                "scope",
                "owner_evidence",
                "prefix_sha256",
            }
            evidence = permit.get("owner_evidence", {})
            if (
                set(permit) != expected
                or not isinstance(evidence, dict)
                or set(evidence) != {"source", "reference", "statement"}
                or any(not isinstance(v, str) or not v.strip() for v in evidence.values())
                or not isinstance(permit["scope"], str)
                or not permit["scope"].strip()
                or any(
                    not isinstance(permit[k], str)
                    or len(permit[k]) != 64
                    or any(c not in "0123456789abcdef" for c in permit[k])
                    for k in ("candidate_id", "registration_sha256")
                )
            ):
                raise AdvisoryError(
                    "exception requires attributable owner evidence and exact scope"
                )
            prior = active_launches(_records(prefix))
            matching = [r for r in prior if r["operation"] == "role.code-review"]
            if (
                permit["repository"] != str(root.resolve())
                or permit["phase"] != phase
                or permit["operation"] != "role.code-review"
                or len(matching) != 2
                or permit["prefix_sha256"] != hashlib.sha256(prefix.encode()).hexdigest()
            ):
                raise AdvisoryError("exception scope or original two-claim prefix mismatch")
            if permits:
                last = next(reversed(permits))
                claimed = [
                    r
                    for r in _records(prefix)
                    if r.get("exception_sha256") == last and "exception" not in r
                ]
                if any(
                    permit[k] != permits[last][k]
                    for k in ("candidate_id", "scope", "owner_evidence")
                ):
                    raise AdvisoryError(
                        "exception retry changed the approved candidate, scope or evidence"
                    )
                if not claimed or any(r in prior for r in claimed):
                    raise AdvisoryError("exception already pending or consumed")
            permits[identifier] = permit
        elif "not_started" not in row:
            if (
                row.get("operation") not in {"role.code-review", "role.plan-review"}
                or not isinstance(row.get("cause"), str)
                or not isinstance(row.get("run"), str)
                or type(row.get("attempt")) is not int
                or row["attempt"] < 1
                or type(row.get("pass")) is not int
            ):
                raise AdvisoryError("invalid advisory claim")
            prior = [
                r for r in active_launches(_records(prefix)) if r["operation"] == row["operation"]
            ]
            if row["pass"] != len(prior) + 1 or (prior and not row["cause"].strip()):
                raise AdvisoryError("advisory pass sequence or cause violated")
            if len(prior) >= 2:
                identifier = row.get("exception_sha256")
                permit = permits.get(identifier)
                if (
                    len(prior) != 2
                    or not permit
                    or identifier in spent
                    or row["operation"] != permit["operation"]
                    or row["run"] != permit["run"]
                    or row.get("candidate_id") != permit["candidate_id"]
                    or row.get("registration_sha256") != permit["registration_sha256"]
                ):
                    raise AdvisoryError("advisory exception missing, spent or mismatched")
                spent.add(identifier)
            elif "exception_sha256" in row:
                raise AdvisoryError("exception cannot replace a normal advisory pass")
        else:
            if set(row) != {"not_started"} or row["not_started"] not in active_launches(
                _records(prefix)
            ):
                raise AdvisoryError("unstarted advisory claim is missing")
        # Serialized records are canonical; a changed byte invalidates subsequent receipts.
        prefix += json.dumps(row, sort_keys=True) + "\n"
    if prefix != text:
        raise AdvisoryError("advisory ledger is not canonical append-only JSON")
    return rows


def grant_exception(
    root: Path,
    phase: str,
    run_dir: Path,
    registration: dict[str, Any],
    candidate: str,
    scope: str,
    evidence: dict[str, Any],
) -> str:
    """Record an already verified User instruction; this is not an authenticator."""
    file = budget_path(root, phase)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        prefix = handle.read()
        validate_budget(root, phase, prefix)
        permit = {
            "repository": str(root.resolve()),
            "phase": phase,
            "operation": registration["operation"],
            "run": str(run_dir.resolve()),
            "registration_sha256": digest(registration),
            "candidate_id": candidate,
            "scope": scope,
            "owner_evidence": evidence,
            "prefix_sha256": hashlib.sha256(prefix.encode()).hexdigest(),
        }
        identifier = digest(permit)
        row = {"exception": permit, "exception_sha256": identifier}
        updated = prefix + json.dumps(row, sort_keys=True) + "\n"
        validate_budget(root, phase, updated)
        handle.write(json.dumps(row, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        return identifier


def start(
    root: Path,
    phase: str,
    run_dir: Path,
    operation: str,
    attempt: int,
    cause: str,
    *,
    registration: dict[str, Any] | None = None,
    candidate: str | None = None,
) -> dict[str, Any]:
    """Claim a launch atomically across continuations, before candidate transmission."""
    file = budget_path(root, phase)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        prefix = handle.read()
        records = validate_budget(root, phase, prefix)
        matching = [r for r in active_launches(records) if r["operation"] == operation]
        if any(r["run"] == str(run_dir.resolve()) and r["attempt"] == attempt for r in matching):
            raise AdvisoryError("advisory invocation already launched; resume is another pass")
        extra = {}
        if len(matching) >= 2:
            permits = [r for r in records if "exception" in r]
            permit_row = permits[-1] if permits else None
            if (
                len(matching) != 2
                or not permit_row
                or registration is None
                or permit_row["exception"]["run"] != str(run_dir.resolve())
                or permit_row["exception"]["operation"] != operation
                or permit_row["exception"]["candidate_id"] != candidate
                or permit_row["exception"]["registration_sha256"] != digest(registration)
            ):
                raise AdvisoryError(
                    "maximum two advisory passes per stage across continuations; "
                    "no matching owner exception"
                )
            extra = {
                "exception_sha256": permit_row["exception_sha256"],
                "candidate_id": candidate,
                "registration_sha256": digest(registration),
            }
        if matching and not cause.strip():
            raise AdvisoryError("second advisory pass requires a recorded cause")
        row = {
            "run": str(run_dir.resolve()),
            "operation": operation,
            "attempt": attempt,
            "pass": len(matching) + 1,
            "cause": cause,
            **extra,
        }
        serialized = json.dumps(row, sort_keys=True) + "\n"
        validate_budget(root, phase, prefix + serialized)
        handle.write(serialized)
        handle.flush()
        os.fsync(handle.fileno())
        return row


def active_launches(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cancelled = {digest(r["not_started"]) for r in records if "not_started" in r}
    return [
        r
        for r in records
        if "not_started" not in r and "exception" not in r and digest(r) not in cancelled
    ]


def cancel_unstarted(root: Path, phase: str, claim: dict[str, Any]) -> None:
    """Only the watcher calls this after Popen fails to create any child."""
    with budget_path(root, phase).open("a+") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        rows = active_launches([json.loads(line) for line in handle if line.strip()])
        if claim not in rows:
            raise AdvisoryError("unstarted advisory claim is missing")
        handle.write(json.dumps({"not_started": claim}, sort_keys=True) + "\n")
        handle.flush()


def launches(root: Path, phase: str) -> list[dict[str, Any]]:
    file = budget_path(root, phase)
    if not file.exists():
        return []
    return active_launches(
        [json.loads(line) for line in file.read_text().splitlines() if line.strip()]
    )


def validate_decision(
    decision: Any, reports: list[dict[str, Any]], candidate: str, required: set[str]
) -> None:
    if not isinstance(decision, dict) or set(decision) != {
        "candidate_id",
        "delta_assessment",
        "requirements_checked",
        "dispositions",
        "reports_sha256",
    }:
        raise AdvisoryError("primary decision has unknown or missing fields")
    if decision["candidate_id"] != candidate or decision["reports_sha256"] != digest(reports):
        raise AdvisoryError("primary acceptance is stale for the candidate or advice")
    for key in ("delta_assessment", "requirements_checked"):
        if not isinstance(decision[key], str) or not decision[key].strip():
            raise AdvisoryError(f"primary acceptance requires {key}")
    if required - {r["kind"] for r in reports}:
        raise AdvisoryError("required independent advisory report is missing")
    expected = {}
    for row in reports:
        check_report(row["report"])
        for finding in row["report"]["findings"]:
            expected[row["report_id"] + ":" + finding["id"]] = finding
    dispositions = decision["dispositions"]
    if not isinstance(dispositions, list):
        raise AdvisoryError("primary dispositions must be a list")
    seen = set()
    for item in dispositions:
        if not isinstance(item, dict) or set(item) != {
            "finding",
            "action",
            "reason",
            "verification",
        }:
            raise AdvisoryError("invalid primary disposition shape")
        key = item["finding"]
        if (
            key not in expected
            or key in seen
            or item["action"] not in {"adopt", "decline", "defer"}
        ):
            raise AdvisoryError("invalid or duplicate primary disposition")
        seen.add(key)
        if not isinstance(item["reason"], str) or not isinstance(item["verification"], str):
            raise AdvisoryError("disposition evidence must be text")
        if (
            expected[key]["severity"] in {"critical", "high", "medium"}
            and not item["reason"].strip()
        ):
            raise AdvisoryError("material advice requires a primary rationale")
        if item["action"] == "adopt" and not item["verification"].strip():
            raise AdvisoryError("adopted correction requires verification")
    material = {
        key for key, f in expected.items() if f["severity"] in {"critical", "high", "medium"}
    }
    if material - seen:
        raise AdvisoryError("material advice lacks primary disposition")
