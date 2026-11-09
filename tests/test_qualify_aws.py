"""The qualification runner's boundaries: nothing touches the account before its typed
confirmation, a dry run renders every packet and calls nothing, deployment state is recorded
one stack at a time, run and teardown verify the recorded target, teardown deletes exactly
the run's prefixes through every listing page and retains the results first, a failed run
is reported as one, and the results renderer lets no observed identifier through.

The runner is driven in-process through a recording transport, so every AWS, CDK and
model-path call is observed; the real transport is never constructed here.
"""

from __future__ import annotations

import importlib.util
import io
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
QUALIFICATION = REPO_ROOT / "qualification"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, QUALIFICATION / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


audit_module = _load("audit")
runner = _load("runner")
results = _load("results")

ACCOUNT = "123456789012"
OUTPUTS = {
    "biblios-qual-network": {
        "SubnetId": "subnet-0fixture000000001",
        "InstanceGroupId": "sg-0fixture0000000001",
    },
    "biblios-qual-artifacts": {"BucketName": "biblios-qual-artifacts-fixture"},
    "biblios-qual-boundary": {"BoundaryArn": "arn:aws:iam::123456789012:policy/biblios/fixture-b"},
    "biblios-qual-custody": {
        "InstanceProfileName": "fixture-profile",
        "InstanceRoleName": "fixture-role",
    },
    "biblios-qual-instance": {"InstanceId": "i-0fixture00000000001"},
    "biblios-example-network": {
        "SubnetId": "subnet-0fixture000000002",
        "InstanceGroupId": "sg-0fixture0000000002",
    },
    "biblios-example-instance": {"InstanceId": "i-0fixture00000000002"},
}


class Recording:
    """Observes every transport call and answers like the account would: a bucket of
    objects with paginated listings, a command whose status is configurable, and CDK
    deploys that write their outputs files."""

    def __init__(
        self,
        *,
        account: str = ACCOUNT,
        objects: dict[str, bytes] | None = None,
        page_size: int = 1000,
        command_status: str = "Success",
        tagged: list[dict[str, Any]] | None = None,
    ) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.argv: list[list[str]] = []
        self.account = account
        self.objects = dict(objects or {})
        self.page_size = page_size
        self.command_status = command_status
        self.deleted: list[str] = []
        self.tagged = tagged or []
        self.native: dict[str, str] = {}

    def aws(self, service: str, operation: str, **params: Any) -> dict[str, Any]:
        self.calls.append(("aws", service, operation))
        if (service, operation) == ("sts", "get_caller_identity"):
            return {
                "Account": self.account,
                "Arn": f"arn:aws:iam::{self.account}:user/fixture",
                "UserId": "AIDAFIXTURE",
            }
        if (service, operation) == ("ec2", "describe_managed_prefix_lists"):
            return {"PrefixLists": [{"PrefixListId": "pl-0fixture0000000001"}]}
        if (service, operation) == ("ssm", "get_parameter"):
            return {"Parameter": {"Value": "ami-0fixture0000000001"}}
        if (service, operation) == ("s3", "upload_file"):
            self.objects[params["Key"]] = Path(params["Filename"]).read_bytes()
            return {}
        if (service, operation) == ("s3", "get_object"):
            return {"Body": io.BytesIO(self.objects[params["Key"]])}
        if (service, operation) == ("s3", "list_objects_v2"):
            keys = sorted(k for k in self.objects if k.startswith(params.get("Prefix", "")))
            start = int(params.get("ContinuationToken") or 0)
            page = keys[start : start + self.page_size]
            truncated = start + self.page_size < len(keys)
            return {
                "Contents": [{"Key": k, "Size": len(self.objects[k])} for k in page],
                "KeyCount": len(page),
                "IsTruncated": truncated,
                **({"NextContinuationToken": str(start + self.page_size)} if truncated else {}),
            }
        if (service, operation) == ("s3", "delete_objects"):
            assert len(params["Delete"]["Objects"]) <= 1000
            for item in params["Delete"]["Objects"]:
                self.deleted.append(item["Key"])
                self.objects.pop(item["Key"], None)
            return {}
        if (service, operation) == ("ssm", "send_command"):
            return {"Command": {"CommandId": "cmd-fixture"}}
        if (service, operation) == ("ssm", "get_command_invocation"):
            return {"Status": self.command_status, "StandardOutputContent": "ran"}
        if (service, operation) == ("resourcegroupstaggingapi", "get_resources"):
            return {"ResourceTagMappingList": self.tagged}
        if service == "ec2" and operation.startswith("describe_"):
            identifier = next(iter(params.values()))[0]
            state = self.native.get(identifier)
            if state is None:
                raise RuntimeError(f"InvalidID.NotFound: {identifier} does not exist")
            if operation == "describe_instances":
                return {"Reservations": [{"Instances": [{"State": {"Name": state}}]}]}
            key = {
                "describe_volumes": "Volumes",
                "describe_vpc_endpoints": "VpcEndpoints",
                "describe_vpcs": "Vpcs",
                "describe_subnets": "Subnets",
                "describe_security_groups": "SecurityGroups",
            }[operation]
            return {key: [{"State": state}]}
        raise AssertionError(f"unexpected call {service}.{operation}")

    def cdk(self, argv: list[str], *, env: dict[str, str]) -> int:
        self.calls.append(("cdk", argv[0], argv[1]))
        self.argv.append(list(argv))
        if argv[0] == "deploy":
            outputs = Path(argv[argv.index("--outputs-file") + 1])
            outputs.write_text(json.dumps({argv[1]: OUTPUTS.get(argv[1], {})}))
        return 0


BASE = ["--run-id", "run-1", "--profile", "fixture", "--region", "xx-fixture-1"]
DEPLOY = ["deploy"]


def _main(tmp_path: Path, *argv: str, transport: Any, confirm: Any, dry_run: bool = False) -> int:
    args = ["--evidence", str(tmp_path / "evidence"), *BASE]
    if dry_run:
        args.append("--dry-run")
    return runner.main([*args, *argv], transport=transport, confirm=confirm)


def _evidence(tmp_path: Path) -> Path:
    return tmp_path / "evidence" / "run-1"


def _journal(tmp_path: Path) -> list[dict[str, Any]]:
    return _journal_at(_evidence(tmp_path))


def _journal_at(evidence: Path) -> list[dict[str, Any]]:
    text = (evidence / "journal.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def _bundle(tmp_path: Path, *, corpus: bool = True) -> Path:
    """A real archive and manifest, the shape bundle.py build produces."""
    stage = tmp_path / "stage"
    (stage / "checkout" / "bin").mkdir(parents=True)
    (stage / "checkout" / "bin" / "python").write_text("#!/bin/sh\n")
    (stage / "checkout" / "bin" / "python").chmod(0o755)
    if corpus:
        (stage / "corpus").mkdir()
        (stage / "corpus" / "a.txt").write_text("SECRET-CORPUS-BYTES\n")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    subprocess.run(["tar", "-cf", str(bundle / "bundle.tar"), "-C", str(stage), "."], check=True)
    manifest = {
        "commit": "0" * 40,
        "files": {"checkout/bin/python": {"bytes": 10}},
        "archive": {"sha256": runner.sha256_of(bundle / "bundle.tar"), "bytes": 1},
        "corpus_files": 1 if corpus else 0,
    }
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def _deployed(tmp_path: Path, transport: Recording | None = None) -> Recording:
    transport = transport or Recording()
    assert _main(tmp_path, *DEPLOY, transport=transport, confirm=lambda p: True) == 0
    return transport


def _lanes_document(outcomes: dict[str, str], *, waived: bool = False) -> bytes:
    return json.dumps(
        {
            "schema": "biblios.qualification-lanes/1",
            "mount": {"root": "/mnt/biblios", "data_volume_waived": waived, "st_dev": 2},
            "lanes": [
                {"lane": lane, "outcome": outcome, "counts": {}, "notes": {"reason": "x"}}
                for lane, outcome in outcomes.items()
            ],
        }
    ).encode()


def _remote_results(outcomes: dict[str, str]) -> dict[str, bytes]:
    return {
        "results/run-1/lanes.json": _lanes_document(outcomes),
        "results/run-1/lanes.log": b"log\n",
        "results/run-1/lanes.exit": b"0\n",
        "results/run-1/install.log": b"installed\n",
        "results/run-1/bedrock-audit.jsonl": _audit_lines(checks=1, calls=1),
    }


def _audit_lines(*, checks: int, calls: int, check_status: str = "ok") -> bytes:
    records = [
        {"kind": "bedrock_retention_check", "status": check_status, "region": "xx-fixture-1"}
        for _ in range(checks)
    ] + [{"kind": "cloud_embed_call", "status": "ok", "model": "m"} for _ in range(calls)]
    return ("\n".join(json.dumps(r) for r in records) + "\n").encode()


def test_dry_run_renders_every_packet_in_order_and_reaches_no_transport(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Failure mode: a dry run reads the account, synthesizes through the CLI, stages objects
    or sends a command; or it stops at the first packet instead of showing the whole walk."""
    transport = Recording()
    bundle = _bundle(tmp_path)
    fail = lambda packet: pytest.fail("a dry run asked for confirmation")  # noqa: E731
    for argv in (
        DEPLOY,
        ["run", "--bundle", str(bundle)],
        ["example", "deploy"],
        ["teardown"],
        ["example", "teardown"],
    ):
        assert _main(tmp_path, *argv, transport=transport, confirm=fail, dry_run=True) == 0
    assert transport.calls == [], f"a dry run reached the transport: {transport.calls}"
    tokens = [r["packet"]["token"] for r in _journal(tmp_path) if r["kind"] == "packet"]
    expected = [
        "yes-read-account",
        *[f"yes-deploy-{s}" for s in runner.PRIVATE_STACKS],
        "yes-verify-identity",
        "yes-stage-bundle",
        "yes-run-lanes",
        "yes-verify-identity",
        *[f"yes-deploy-{s}" for s in runner.EXAMPLE_STACKS],
        "yes-verify-identity",
        "yes-list-objects",
        "yes-delete-objects",
        *[f"yes-destroy-{s}" for s in reversed(runner.PRIVATE_STACKS)],
        "yes-verify-identity",
        *[f"yes-destroy-{s}" for s in reversed(runner.EXAMPLE_STACKS)],
    ]
    assert tokens == expected
    printed = capsys.readouterr().out
    assert "account's default encryption" in printed and "32 GiB" in printed
    assert "tar --no-same-owner -xpf bundle.tar" in printed
    assert not (_evidence(tmp_path) / "state.json").exists()


def test_a_declined_confirmation_stops_before_any_call_including_teardowns_listing(
    tmp_path: Path,
) -> None:
    """Failure mode: the preflight reads, the first stack deploys, or teardown lists the
    bucket before anyone typed a token."""
    transport = Recording()
    assert _main(tmp_path, *DEPLOY, transport=transport, confirm=lambda p: False) == 2
    assert transport.calls == []
    tokens: list[str] = []

    def first_only(packet: dict[str, Any]) -> bool:
        tokens.append(packet["token"])
        return packet["token"] == "yes-read-account"

    assert _main(tmp_path, *DEPLOY, transport=transport, confirm=first_only) == 2
    assert [c for c in transport.calls if c[0] == "cdk"] == []
    assert tokens == ["yes-read-account", "yes-deploy-biblios-qual-network"]
    deployed = _deployed(tmp_path)
    before = len(deployed.calls)
    assert _main(tmp_path, "teardown", transport=deployed, confirm=lambda p: False) == 2
    assert deployed.calls[before:] == []
    identity_only = lambda p: p["token"] == "yes-verify-identity"  # noqa: E731
    assert _main(tmp_path, "teardown", transport=deployed, confirm=identity_only) == 2
    assert deployed.calls[before:] == [("aws", "sts", "get_caller_identity")]
    with pytest.raises(runner.Refused, match="dry run: sts.get_caller_identity"):
        runner.RefusingTransport().aws("sts", "get_caller_identity")


def test_confirmed_deploy_records_target_and_state_per_stack_with_an_external_output(
    tmp_path: Path,
) -> None:
    """Failure mode: a stack deploys out of order, the CDK writes cdk.out inside the
    repository, the custody packet hides the role, or a declined later stack leaves no record
    of the stacks already created."""
    packets: list[dict[str, Any]] = []
    transport = _deployed(tmp_path)  # confirm=True records nothing; redo with a recorder
    assert [c[2] for c in transport.calls if c[0] == "cdk"] == list(runner.PRIVATE_STACKS)
    for argv in transport.argv:
        output = Path(argv[argv.index("--output") + 1])
        assert output == _evidence(tmp_path) / "cdk.out"
        assert not output.is_relative_to(REPO_ROOT / "infra")
    target = json.loads((_evidence(tmp_path) / "target.json").read_text())
    assert target["account"] == ACCOUNT and target["image_id"] == "ami-0fixture0000000001"
    state = json.loads((_evidence(tmp_path) / "state.json").read_text())
    assert state["order"] == list(runner.PRIVATE_STACKS)
    assert all(s["status"] == "created" for s in state["stacks"].values())

    def record(packet: dict[str, Any]) -> bool:
        packets.append(packet)
        return packet["token"] != "yes-deploy-biblios-qual-custody"

    partial = tmp_path / "partial"
    args = ["--evidence", str(partial), *BASE, *DEPLOY]
    assert runner.main(args, transport=Recording(), confirm=record) == 2
    custody = next(p for p in packets if p["token"] == "yes-deploy-biblios-qual-custody")
    assert "IAM role" in custody["creates"] and "permissions boundary" in custody["creates"]
    assert custody["parameters"]["WorkloadBoundaryArn"] == "(as supplied)"
    assert custody["account"] == ACCOUNT
    boundary = next(p for p in packets if p["token"] == "yes-deploy-biblios-qual-boundary")
    assert "permissions boundary" in boundary["creates"]
    assert boundary["parameters"]["ArtifactBucket"] == "biblios-qual-artifacts-fixture"
    state = json.loads((partial / "run-1" / "state.json").read_text())
    assert state["order"] == [
        "biblios-qual-network",
        "biblios-qual-artifacts",
        "biblios-qual-boundary",
    ]


def test_staged_deploy_continues_from_recorded_state_and_refuses_redeploy_or_gaps(
    tmp_path: Path,
) -> None:
    """Failure mode: a second invocation redeploys a stack that already exists, deploys
    custody before the boundary it needs is recorded, re-reads the account instead of
    verifying the recorded target, or forgets the stacks the first invocation created."""
    transport = Recording()
    packets: list[str] = []

    def record(packet: dict[str, Any]) -> bool:
        packets.append(packet["token"])
        return True

    first = ["deploy", "--stacks", "biblios-qual-network,biblios-qual-artifacts"]
    assert _main(tmp_path, *first, transport=transport, confirm=record) == 0
    state = json.loads((_evidence(tmp_path) / "state.json").read_text())
    assert state["order"] == ["biblios-qual-network", "biblios-qual-artifacts"]
    assert "BucketName" in state["stacks"]["biblios-qual-artifacts"]["outputs"]
    # Custody without a boundary stack or a supplied ARN is refused before any call; a stack
    # already created is refused; a gap and an out-of-order selection are refused.
    assert (
        _main(
            tmp_path,
            "deploy",
            "--stacks",
            "biblios-qual-custody",
            transport=transport,
            confirm=record,
        )
        == 2
    )
    assert _main(tmp_path, *first, transport=transport, confirm=record) == 2
    assert (
        _main(
            tmp_path,
            "deploy",
            "--stacks",
            "biblios-qual-instance",
            transport=transport,
            confirm=record,
        )
        == 2
    )
    assert (
        _main(
            tmp_path,
            "deploy",
            "--stacks",
            "biblios-qual-artifacts,biblios-qual-network",
            transport=transport,
            confirm=record,
        )
        == 2
    )
    cdk_before = [c for c in transport.calls if c[0] == "cdk"]
    assert [c[2] for c in cdk_before] == ["biblios-qual-network", "biblios-qual-artifacts"]
    packets.clear()
    second = [
        "deploy",
        "--stacks",
        "biblios-qual-boundary,biblios-qual-custody,biblios-qual-instance",
    ]
    assert _main(tmp_path, *second, transport=transport, confirm=record) == 0
    assert packets == [
        "yes-verify-identity",
        "yes-deploy-biblios-qual-boundary",
        "yes-deploy-biblios-qual-custody",
        "yes-deploy-biblios-qual-instance",
    ]
    assert [c[2] for c in transport.calls if c[0] == "cdk"] == list(runner.PRIVATE_STACKS)
    assert (
        "biblios-qual-boundary:ArtifactBucket=biblios-qual-artifacts-fixture" in transport.argv[-3]
    )
    assert (
        "biblios-qual-custody:ArtifactBucket=biblios-qual-artifacts-fixture" in transport.argv[-2]
    )
    expected_boundary = OUTPUTS["biblios-qual-boundary"]["BoundaryArn"]
    assert f"biblios-qual-custody:WorkloadBoundaryArn={expected_boundary}" in transport.argv[-2]
    state = json.loads((_evidence(tmp_path) / "state.json").read_text())
    assert state["order"] == list(runner.PRIVATE_STACKS)
    assert all(s["status"] == "created" for s in state["stacks"].values())
    assert state["stacks"]["biblios-qual-instance"]["parameters"]["DataVolumeGiB"] == "32"
    assert "WorkloadBoundaryArn" not in state["stacks"]["biblios-qual-custody"]["parameters"]
    # Teardown of the staged deployment destroys all five in reverse, and its packets describe
    # the deployed shape from the recorded parameters (the live run printed "? GiB" before).
    packets: list[dict] = []

    def confirm(packet: dict) -> bool:
        packets.append(packet)
        return True

    assert _main(tmp_path, "teardown", transport=transport, confirm=confirm) == 0
    destroys = [c[2] for c in transport.calls if c[0] == "cdk" and c[1] == "destroy"]
    assert destroys == list(reversed(runner.PRIVATE_STACKS))
    instance = next(p for p in packets if p["token"] == "yes-destroy-biblios-qual-instance")
    assert "one m7g.xlarge with IMDSv2" in instance["destroys"]
    assert "root plus one 32 GiB gp3 data volume" in instance["destroys"]
    assert "?" not in instance["destroys"]


def _fixture(tmp_path: Path) -> tuple[Path, Path]:
    archive = tmp_path / "palace-qual.tar.gz"
    archive.write_bytes(b"not really a tarball, but bytes\n")
    script = tmp_path / "run.sh"
    script.write_text('/opt/palace-qual/run-instance-test.sh "$1"\n')
    return archive, script


def test_fixture_route_stages_the_owners_archive_runs_its_script_and_collects_every_result(
    tmp_path: Path,
) -> None:
    """Failure mode: the fixture script is not the one shown in the packet, the results are
    only partly collected, or a nonzero fixture exit reads as a pass."""
    archive, script = _fixture(tmp_path)
    remote = {
        "results/run-1/fixture.exit": b"0\n",
        "results/run-1/fixture.log": b"ran\n",
        "results/run-1/palace/summary.json": b"{}",
        "results/run-1/palace/audit.jsonl": b"{}\n",
    }
    transport = _deployed(tmp_path, Recording(objects=remote))
    packets: list[dict[str, Any]] = []

    def record(packet: dict[str, Any]) -> bool:
        packets.append(packet)
        return True

    argv = ["run", "--fixture", str(archive), "--fixture-command", str(script)]
    assert _main(tmp_path, *argv, transport=transport, confirm=record) == 0
    tokens = [p["token"] for p in packets]
    assert tokens == ["yes-verify-identity", "yes-stage-fixture", "yes-run-fixture"]
    command = "\n".join(packets[2]["commands"])
    assert '/opt/palace-qual/run-instance-test.sh "$1"' in command
    assert "tar --no-same-owner -xaf palace-qual.tar.gz -C /mnt/biblios/fixture" in command
    assert command.endswith('exit "$(cat /mnt/biblios/results/fixture.exit)"')
    assert "inputs/run-1/fixture/palace-qual.tar.gz" in transport.objects
    for relative in ("fixture.exit", "fixture.log", "palace/summary.json", "palace/audit.jsonl"):
        assert (_evidence(tmp_path) / "results" / relative).is_file(), relative
    assert (
        _main(tmp_path, "run", "--fixture", str(archive), transport=transport, confirm=record) == 2
    )
    failing = Recording(objects={"results/run-1/fixture.exit": b"3\n"})
    other = tmp_path / "failing"
    args = ["--evidence", str(other), *BASE]
    assert runner.main([*args, *DEPLOY], transport=failing, confirm=lambda p: True) == 0
    assert runner.main([*args, *argv], transport=failing, confirm=lambda p: True) == 3


def test_qualify_runs_the_whole_lifecycle_and_enters_scoped_teardown_on_failure(
    tmp_path: Path,
) -> None:
    """Failure mode: the integrated verb leaves billing resources up after a failed run,
    tears down something it did not create, or reports a failed run as a pass."""
    archive, script = _fixture(tmp_path)
    transport = Recording(objects={"results/run-1/fixture.exit": b"0\n"})
    tokens: list[str] = []

    def record(packet: dict[str, Any]) -> bool:
        tokens.append(packet["token"])
        return True

    argv = ["qualify", "--fixture", str(archive), "--fixture-command", str(script)]
    assert (
        _main(tmp_path, *argv, "--instance-type", "m7g.large", transport=transport, confirm=record)
        == 0
    )
    assert tokens[0] == "yes-read-account"
    assert tokens[1:6] == [f"yes-deploy-{s}" for s in runner.PRIVATE_STACKS]
    assert tokens[6:9] == ["yes-verify-identity", "yes-stage-fixture", "yes-run-fixture"]
    assert tokens[9:12] == ["yes-verify-identity", "yes-list-objects", "yes-delete-objects"]
    assert tokens[12:] == [f"yes-destroy-{s}" for s in reversed(runner.PRIVATE_STACKS)]
    assert "biblios-qual-instance:InstanceType=m7g.large" in transport.argv[4]
    state = json.loads((_evidence(tmp_path) / "state.json").read_text())
    assert all(s["status"] == "destroyed" for s in state["stacks"].values())
    # A failure after three stacks: the run stops, and exactly those three are destroyed.
    failing = Recording()
    other = tmp_path / "failing"
    args = ["--evidence", str(other), *BASE]
    seen: list[str] = []

    def decline_custody(packet: dict[str, Any]) -> bool:
        seen.append(packet["token"])
        return packet["token"] != "yes-deploy-biblios-qual-custody"

    assert runner.main([*args, *argv], transport=failing, confirm=decline_custody) == 2
    destroys = [c[2] for c in failing.calls if c[0] == "cdk" and c[1] == "destroy"]
    assert destroys == ["biblios-qual-boundary", "biblios-qual-artifacts", "biblios-qual-network"]
    assert "yes-run-fixture" not in seen
    # Nothing created, nothing torn down: a refusal before the first stack is just a refusal.
    none = Recording()
    third = tmp_path / "none"
    assert (
        runner.main(
            ["--evidence", str(third), *BASE, *argv], transport=none, confirm=lambda p: False
        )
        == 2
    )
    assert none.calls == []


def test_example_reuses_the_runs_bucket_and_profile_or_refuses(tmp_path: Path) -> None:
    """Failure mode: the example deploys its network and then fails for want of an instance
    profile, or deploys without verifying the caller."""
    transport = Recording()
    assert _main(tmp_path, "example", "deploy", transport=transport, confirm=lambda p: True) == 2
    assert transport.calls == []
    transport = _deployed(tmp_path)
    before = len(transport.calls)
    assert _main(tmp_path, "example", "deploy", transport=transport, confirm=lambda p: True) == 0
    new = transport.calls[before:]
    assert new[0] == ("aws", "sts", "get_caller_identity")
    assert [c[2] for c in new if c[0] == "cdk"] == list(runner.EXAMPLE_STACKS)
    instance_argv = transport.argv[-1]
    assert "biblios-example-instance:InstanceProfileName=fixture-profile" in instance_argv
    state = json.loads((_evidence(tmp_path) / "example-state.json").read_text())
    assert state["order"] == list(runner.EXAMPLE_STACKS)


def test_run_validates_before_staging_binds_bedrock_and_judges_the_outcome(
    tmp_path: Path,
) -> None:
    """Failure mode: the default run demands Bedrock inputs, an invalid request uploads data
    first, the journal carries the archive's bytes, a failed command or lane reads as a pass,
    or the run proceeds against an account other than the recorded one."""
    bundle = _bundle(tmp_path)
    transport = _deployed(
        tmp_path,
        Recording(
            objects=_remote_results(
                {"hermetic": "passed", "ingestion": "passed", "portability": "passed"}
            )
        ),
    )
    before = len(transport.calls)
    assert (
        _main(
            tmp_path,
            "run",
            "--bundle",
            str(bundle),
            "--lane",
            "bedrock",
            transport=transport,
            confirm=lambda p: True,
        )
        == 2
    )
    assert (
        _main(
            tmp_path,
            "run",
            "--bundle",
            str(bundle),
            "--bedrock-model",
            "m",
            transport=transport,
            confirm=lambda p: True,
        )
        == 2
    )
    assert transport.calls[before:] == []
    packets: list[dict[str, Any]] = []

    def record(packet: dict[str, Any]) -> bool:
        packets.append(packet)
        return True

    assert _main(tmp_path, "run", "--bundle", str(bundle), transport=transport, confirm=record) == 0
    tokens = [p["token"] for p in packets]
    assert tokens == ["yes-verify-identity", "yes-stage-bundle", "yes-run-lanes"]
    assert "--lanes hermetic,ingestion,portability" in "\n".join(packets[2]["commands"])
    uploads = [k for k in transport.objects if k.startswith("inputs/run-1/")]
    assert sorted(uploads) == ["inputs/run-1/bundle.tar", "inputs/run-1/manifest.json"]
    journal_text = (_evidence(tmp_path) / "journal.jsonl").read_text()
    assert "SECRET-CORPUS-BYTES" not in journal_text
    kinds = [r["kind"] for r in _journal(tmp_path)]
    dispatched = kinds.index("lanes-dispatched")
    send = next(
        i
        for i, r in enumerate(_journal(tmp_path))
        if r["kind"] == "aws" and r["operation"] == "send_command"
    )
    assert dispatched < send
    collected = json.loads((_evidence(tmp_path) / "collected.json").read_text())
    assert collected == {name: True for name in runner.RESULT_ARTIFACTS}
    assert (_evidence(tmp_path) / "lanes.log").read_bytes() == b"log\n"
    # A lane that did not pass, or a command that did not succeed, is exit 3.
    failing = Recording(
        objects=_remote_results(
            {"hermetic": "passed", "ingestion": "incomplete", "portability": "passed"}
        )
    )
    other = tmp_path / "failing"
    args = ["--evidence", str(other), *BASE]
    assert runner.main([*args, *DEPLOY], transport=failing, confirm=lambda p: True) == 0
    for name in runner.RESULT_ARTIFACTS:
        (other / "run-1" / name).unlink(missing_ok=True)
    assert (
        runner.main(
            [*args, "run", "--bundle", str(bundle)], transport=failing, confirm=lambda p: True
        )
        == 3
    )
    unknown = Recording(
        command_status="Failed",
        objects=_remote_results(
            {"hermetic": "passed", "ingestion": "passed", "portability": "passed"}
        ),
    )
    third = tmp_path / "unknown"
    args = ["--evidence", str(third), *BASE]
    assert runner.main([*args, *DEPLOY], transport=unknown, confirm=lambda p: True) == 0
    assert (
        runner.main(
            [*args, "run", "--bundle", str(bundle)], transport=unknown, confirm=lambda p: True
        )
        == 3
    )
    # The Bedrock packet binds model, region and text count into the instance command.
    bedrock = Recording(objects=_remote_results({"hermetic": "passed", "bedrock": "passed"}))
    fourth = tmp_path / "bedrock"
    args = ["--evidence", str(fourth), *BASE]
    assert runner.main([*args, *DEPLOY], transport=bedrock, confirm=lambda p: True) == 0
    packets.clear()
    assert (
        runner.main(
            [
                *args,
                "run",
                "--bundle",
                str(bundle),
                "--lane",
                "hermetic",
                "--lane",
                "bedrock",
                "--bedrock-settings",
                "/mnt/biblios/bundle/bedrock.yaml",
                "--bedrock-model",
                "cohere.embed-v4:0",
                "--bedrock-texts",
                "2",
                "--bedrock-cost-cap",
                "0.01 USD",
            ],
            transport=bedrock,
            confirm=record,
        )
        == 0
    )
    calls = next(p for p in packets if p["token"] == "yes-bedrock-calls")
    assert "at most 2 texts" in calls["calls"] and "not an enforced meter" in calls["cost"]
    # A Bedrock lane that passed on the instance is not evidence without its exported audit
    # (attempt 3 lost those records with the volume), nor with an audit whose retention
    # check did not succeed.
    bedrock_args = [
        "run",
        "--bundle",
        str(bundle),
        "--lane",
        "bedrock",
        "--bedrock-settings",
        "/mnt/biblios/bundle/bedrock.yaml",
        "--bedrock-model",
        "cohere.embed-v4:0",
        "--bedrock-texts",
        "2",
        "--bedrock-cost-cap",
        "0.01 USD",
    ]
    for label, objects in (
        (
            "missing",
            {k: v for k, v in _remote_results({"bedrock": "passed"}).items() if "audit" not in k},
        ),
        (
            "unchecked",
            _remote_results({"bedrock": "passed"})
            | {
                "results/run-1/bedrock-audit.jsonl": _audit_lines(
                    checks=1, calls=1, check_status="error"
                )
            },
        ),
    ):
        unaudited = Recording(objects=objects)
        evidence = tmp_path / f"bedrock-{label}"
        unaudited_args = ["--evidence", str(evidence), *BASE]
        assert (
            runner.main([*unaudited_args, *DEPLOY], transport=unaudited, confirm=lambda p: True)
            == 0
        )
        assert (
            runner.main(
                [*unaudited_args, *bedrock_args], transport=unaudited, confirm=lambda p: True
            )
            == 3
        )
        verdict = next(r for r in _journal_at(evidence / "run-1") if r["kind"] == "verdict")
        assert verdict["passed"] is False and any(
            "bedrock-audit.jsonl" in p for p in verdict["problems"]
        )
    command = "\n".join(next(p for p in packets if p["token"] == "yes-run-lanes")["commands"])
    assert (
        "--bedrock-model cohere.embed-v4:0 --bedrock-region xx-fixture-1 --bedrock-texts 2"
        in command
    )
    # The recorded target binds the run: another account is refused before staging.
    wrong = Recording(account="999999999999")
    (fourth / "run-1" / "lanes.json").unlink()
    before = len(wrong.calls)
    assert (
        runner.main(
            [*args, "run", "--bundle", str(bundle)], transport=wrong, confirm=lambda p: True
        )
        == 2
    )
    assert wrong.calls[before:] == [("aws", "sts", "get_caller_identity")]


def test_teardown_retains_results_deletes_only_the_runs_prefixes_in_pages_and_keeps_a_shared_bucket(
    tmp_path: Path,
) -> None:
    """Failure mode: teardown deletes another run's objects, stops after one listing page,
    destroys the bucket with foreign objects in it, or deletes the only copy of a result log."""
    objects = {f"inputs/run-1/cache/{i:04d}": b"x" for i in range(2300)}
    objects |= _remote_results({"hermetic": "passed"})
    objects |= {"inputs/other-run/keep": b"theirs", "results/other-run/lanes.json": b"{}"}
    transport = _deployed(tmp_path, Recording(objects=objects, page_size=1000))
    transport.tagged = [
        {
            "ResourceARN": "arn:aws:s3:::other",
            "Tags": [{"Key": "aws:cloudformation:stack-name", "Value": "biblios-example-network"}],
        },
    ]
    runner.Journal(_evidence(tmp_path) / "journal.jsonl").append(
        "lanes-dispatched", lanes=["hermetic"]
    )
    (_evidence(tmp_path) / "lanes.json").write_bytes(_lanes_document({"hermetic": "passed"}))
    transport.objects["results/run-1/palace/summary.json"] = b'{"unique": true}'
    tokens: list[str] = []

    def confirm(packet: dict[str, Any]) -> bool:
        tokens.append(packet["token"])
        if packet["token"] == "yes-delete-objects":
            assert packet["scope"] == ["inputs/run-1/", "results/run-1/"]
            assert packet["action"].startswith("delete 2306 objects")
            assert "2 objects outside" in packet["outside_scope"]
        return True

    before = len(transport.calls)
    assert _main(tmp_path, "teardown", transport=transport, confirm=confirm) == 0
    new = transport.calls[before:]
    assert new[0] == ("aws", "sts", "get_caller_identity")
    assert new[1] == ("aws", "s3", "list_objects_v2")
    assert (_evidence(tmp_path) / "lanes.log").read_bytes() == b"log\n"
    unique = _evidence(tmp_path) / "results" / "palace" / "summary.json"
    assert unique.read_bytes() == b'{"unique": true}'
    assert not any(
        k.startswith(("inputs/other-run/", "results/other-run/")) for k in transport.deleted
    )
    assert len(transport.deleted) == 2306 and "inputs/other-run/keep" in transport.objects
    deletes = [c for c in new if c == ("aws", "s3", "delete_objects")]
    assert len(deletes) == math.ceil(2304 / 1000)
    destroys = [c[2] for c in new if c[0] == "cdk" and c[1] == "destroy"]
    assert destroys == [
        "biblios-qual-instance",
        "biblios-qual-custody",
        "biblios-qual-boundary",
        "biblios-qual-network",
    ]
    assert tokens[:3] == ["yes-verify-identity", "yes-list-objects", "yes-delete-objects"]
    assert new[-1] == ("aws", "resourcegroupstaggingapi", "get_resources")
    inventory = json.loads((_evidence(tmp_path) / "inventory.json").read_text())
    assert inventory["live"] == [] and inventory["listed"] == []
    state = json.loads((_evidence(tmp_path) / "state.json").read_text())
    assert state["stacks"]["biblios-qual-artifacts"]["status"] == "created"
    assert state["stacks"]["biblios-qual-instance"]["status"] == "destroyed"
    # A partial deployment tears down exactly what was created.
    partial = Recording()
    other = tmp_path / "partial"
    args = ["--evidence", str(other), *BASE]
    assert (
        runner.main(
            [*args, *DEPLOY],
            transport=partial,
            confirm=lambda p: p["token"] != "yes-deploy-biblios-qual-artifacts",
        )
        == 2
    )
    assert runner.main([*args, "teardown"], transport=partial, confirm=lambda p: True) == 0
    assert [c[2] for c in partial.calls if c[0] == "cdk" and c[1] == "destroy"] == [
        "biblios-qual-network"
    ]
    assert ("aws", "s3", "list_objects_v2") not in partial.calls


def test_teardown_verifies_the_tag_inventory_natively_and_fails_on_live_resources(
    tmp_path: Path,
) -> None:
    """Failure mode: a stale tag-index entry for a terminated or deleted resource reads as a
    leftover, or a resource that is verifiably still live lets teardown exit 0."""
    stale = [
        {"ResourceARN": "arn:aws:ec2:xx-fixture-1:123456789012:instance/i-0dead", "Tags": []},
        {"ResourceARN": "arn:aws:ec2:xx-fixture-1:123456789012:volume/vol-0gone", "Tags": []},
        {
            "ResourceARN": "arn:aws:ec2:xx-fixture-1:123456789012:vpc-endpoint/vpce-0gone",
            "Tags": [],
        },
    ]
    transport = _deployed(tmp_path, Recording(tagged=stale))
    transport.native = {"i-0dead": "terminated"}  # the volume and endpoint do not exist
    assert _main(tmp_path, "teardown", transport=transport, confirm=lambda p: True) == 0
    inventory = json.loads((_evidence(tmp_path) / "inventory.json").read_text())
    assert inventory["live"] == [] and len(inventory["stale"]) == 3
    assert ("aws", "ec2", "describe_instances") in transport.calls
    live = Recording(
        tagged=[
            {
                "ResourceARN": "arn:aws:ec2:xx-fixture-1:123456789012:vpc-endpoint/vpce-0live",
                "Tags": [],
            },
            {"ResourceARN": "arn:aws:ec2:xx-fixture-1:123456789012:instance/i-0dead", "Tags": []},
        ]
    )
    live.native = {"vpce-0live": "available", "i-0dead": "terminated"}
    other = tmp_path / "live"
    args = ["--evidence", str(other), *BASE]
    assert runner.main([*args, *DEPLOY], transport=live, confirm=lambda p: True) == 0
    assert runner.main([*args, "teardown"], transport=live, confirm=lambda p: True) == 3
    inventory = json.loads((other / "run-1" / "inventory.json").read_text())
    assert inventory["live"] == [
        "arn:aws:ec2:xx-fixture-1:123456789012:vpc-endpoint/vpce-0live (available)"
    ]
    assert len(inventory["stale"]) == 1


def test_results_brief_carries_no_observed_identifier_and_names_a_waived_mount(
    tmp_path: Path,
) -> None:
    """Failure mode: an account id, region, instance id or bucket name from the journal reaches
    the brief, a run whose lanes did not sit on the data volume reads as an EBS result, a
    recorded lane exception is dropped, a lane log showing Palace fell back to fused order
    leaves the brief implying reranking was exercised, or the brief lets a passed Bedrock lane
    read as request-level evidence."""
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "lanes.log").write_text(
        "palace search: rerank failed: failed to download model.onnx: <urlopen error "
        "[Errno 101] Network is unreachable> \u2014 returning fused order\n"
    )
    journal = runner.Journal(evidence / "journal.jsonl")
    journal.append(
        "aws",
        service="sts",
        operation="get_caller_identity",
        params={},
        response={"Account": "123456789012", "Arn": "arn:aws:iam::123456789012:user/fixture"},
    )
    journal.append(
        "outputs",
        stack="biblios-qual-instance",
        outputs={
            "InstanceId": "i-0abc123def4567890",
            "BucketName": "biblios-qual-artifacts-fixture",
            "SubnetId": "subnet-0abc123def4567890",
        },
    )
    journal.append("packet", packet={"region": "xx-fixture-1", "token": "yes-run-lanes"})
    (evidence / "lanes.json").write_text(
        json.dumps(
            {
                "mount": {
                    "root": "/mnt/biblios",
                    "device_marker": None,
                    "st_dev": 1,
                    "data_volume_waived": True,
                },
                "lanes": [
                    {
                        "lane": "hermetic",
                        "outcome": "passed",
                        "counts": {"suite_exit": 0},
                        "notes": {
                            "verify_problems": [
                                "instance i-0abc123def4567890 in xx-fixture-1 "
                                "reported account 123456789012"
                            ]
                        },
                    },
                    {
                        "lane": "ingestion",
                        "outcome": "failed",
                        "counts": {},
                        "notes": {"error": "RuntimeError('conversion crashed')"},
                    },
                    {
                        "lane": "bedrock",
                        "outcome": "not-run",
                        "counts": {},
                        "notes": {
                            "reason": "provider not released: the declared provider is not bedrock"
                        },
                    },
                ],
            }
        )
    )
    brief = results.render(evidence, as_of="2026-11-07")
    for identifier in (
        "123456789012",
        "i-0abc123def4567890",
        "biblios-qual-artifacts-fixture",
        "subnet-0abc123def4567890",
        "xx-fixture-1",
        "arn:aws:iam",
    ):
        assert identifier not in brief, identifier
    assert "assertion waived: yes" in brief and "not an EBS qualification" in brief
    assert "bedrock: not-run" in brief and "provider not released" in brief
    assert "Recorded failure: RuntimeError('conversion crashed')" in brief
    assert "Reranking is not qualified" in brief and "fused keyword-plus-vector order" in brief
    assert "not an observed request count" in brief and "are not collected" in brief
    audited = json.loads((evidence / "lanes.json").read_text())
    audited["lanes"] = [lane for lane in audited["lanes"] if lane["lane"] != "bedrock"]
    audited["lanes"].append(
        {
            "lane": "bedrock",
            "outcome": "passed",
            "counts": {
                "hits": 1,
                "rerank_status": "applied",
                "audit_records": 3,
                "audit_retention_checks": 1,
                "audit_embed_calls": 2,
            },
            "notes": {"audit_artifact": "bedrock-audit.jsonl"},
        }
    )
    (evidence / "lanes.json").write_text(json.dumps(audited))
    # Lane counts alone: the lane exported on the instance, nothing was collected off it.
    uncollected = results.render(evidence, as_of="2026-11-07")
    assert (
        "Exported on the instance (bedrock-audit.jsonl, the lane's own count): 3 records, "
        "1 retention checks, 2 embedding requests; rerank status applied." in uncollected
    )
    assert "Retained with the run: no; bedrock-audit.jsonl was not collected" in uncollected
    assert "request-level evidence is the exported audit artifact" not in uncollected
    assert "are not collected" in uncollected
    # Collected and complete: the wording follows the artifact, not the lane.
    (evidence / "collected.json").write_text(json.dumps({"bedrock-audit.jsonl": True}))
    (evidence / "bedrock-audit.jsonl").write_bytes(_audit_lines(checks=1, calls=2))
    with_audit = results.render(evidence, as_of="2026-11-07")
    assert "Retained with the run: yes, 3 records in bedrock-audit.jsonl, judged complete" in (
        with_audit
    )
    assert "request-level evidence is the exported audit artifact" in with_audit
    assert "are not collected" not in with_audit
    # Collected but incomplete: said so, and no evidence claim.
    (evidence / "bedrock-audit.jsonl").write_bytes(_audit_lines(checks=0, calls=2))
    incomplete = results.render(evidence, as_of="2026-11-07")
    assert "is here but incomplete (no successful retention check" in incomplete
    assert "request-level evidence is the exported audit artifact" not in incomplete
    (evidence / "lanes.log").write_text("palace search: reranked 5 hits\n")
    assert "Reranking is not qualified" not in results.render(evidence, as_of="2026-11-07")
    denylist = (evidence / "denylist.txt").read_text().split()
    assert "123456789012" in denylist and "biblios-qual-artifacts-fixture" in denylist


def test_bundle_proof_runs_in_the_instances_distribution_with_the_network_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Failure mode: the offline proof runs in the builder image, whose host tools (git,
    file) hide a bundle that forgot them, which is how the first live run failed at
    `uv sync` on an instance without git; or the proof reaches the network, or mounts the
    bundle writable."""
    bundle_module = _load("bundle")
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    (bundle / "bundle.tar").write_bytes(b"tar bytes")
    (bundle / "manifest.json").write_text(
        json.dumps(
            {
                "architecture": "arm64",
                "archive": {"sha256": bundle_module.sha256_of(bundle / "bundle.tar")},
                "corpus_files": 3,
            }
        )
    )
    calls: list[list[str]] = []

    def fake_docker(args, *, log, stdin=None):
        calls.append(list(args))
        return 1 if args[:2] == ["image", "inspect"] else 0

    monkeypatch.setattr(bundle_module, "docker", fake_docker)
    assert bundle_module.prove(bundle, arch="arm64", log=tmp_path / "proof.log") == 0
    build = next(c for c in calls if c[0] == "build")
    assert build[-2:] == [bundle_module.PROOF_IMAGE, "-"]
    run = next(c for c in calls if c[0] == "run")
    assert bundle_module.PROOF_IMAGE in run and bundle_module.BUILDER_IMAGE not in run
    assert run[run.index("--network") + 1] == "none"
    assert f"{bundle}:/bundle:ro" in run
    script = run[-1]
    assert 'for tool in git file tesseract; do command -v "$tool"' in script
    assert "--lanes hermetic,portability,ingestion" in script and "--corpus /work/corpus" in script
    assert "PALACE_MODELS_DIR=/work/models" in script
    assert bundle_module.RERANK_PROBE.rstrip("\n") in script
    # The probe itself: executed here against stub Palace modules, it must construct the
    # reranker from the configured model directory, probe it and score; a failing probe
    # propagates (text-only inspection let a deleted probe pass, the critic's E3).
    import types

    calls: list[tuple[str, Any]] = []

    class StubReranker:
        def __init__(self, *, model_dir):
            calls.append(("init", model_dir))

        def probe(self):
            calls.append(("probe", None))
            if getattr(StubReranker, "fail", False):
                raise RuntimeError("model unloadable")

        def score(self, query, texts):
            calls.append(("score", (query, tuple(texts))))
            return [0.5 for _ in texts]

    palace = types.ModuleType("palace")
    rerank = types.ModuleType("palace.rerank")
    rerank.CrossEncoderReranker = StubReranker
    config = types.ModuleType("palace.retrieval_config")
    config.rerank_model_dir = lambda: Path("/work/models/ms-marco-MiniLM-L-6-v2")
    monkeypatch.setitem(sys.modules, "palace", palace)
    monkeypatch.setitem(sys.modules, "palace.rerank", rerank)
    monkeypatch.setitem(sys.modules, "palace.retrieval_config", config)
    exec(compile(bundle_module.RERANK_PROBE, "<rerank-probe>", "exec"), {})
    assert [c[0] for c in calls] == ["init", "probe", "score"]
    assert calls[0][1] == Path("/work/models/ms-marco-MiniLM-L-6-v2")
    assert calls[2][1][1] == ("a fresnel lens", "a tide table")
    StubReranker.fail = True
    with pytest.raises(RuntimeError, match="model unloadable"):
        exec(compile(bundle_module.RERANK_PROBE, "<rerank-probe>", "exec"), {})
    assert bundle_module.PROOF_BASE_IMAGE.startswith("public.ecr.aws/amazonlinux/amazonlinux:2023")


def test_bedrock_lane_settings_carry_the_assertion_palace_requires_and_the_lane_refuses_without_it(
    tmp_path: Path,
) -> None:
    """Failure mode: the committed Bedrock settings omit or blank Palace's published-corpus
    assertion (the second paid run reached Palace's refusal after a full deploy for exactly
    that), name another model, region or dimension than the packet confirms, or the lane
    proceeds toward a request with an incomplete assertion instead of recording why not."""
    from datetime import datetime

    from biblios import load_settings

    lanes = _load("lanes")
    committed = REPO_ROOT / "qualification" / "bedrock-settings.yaml"
    declared = load_settings([committed], committed.parent).palace.embedding
    assert (declared.provider, declared.model, declared.region, declared.dim) == (
        "bedrock",
        "cohere.embed-v4:0",
        "us-east-1",
        1536,
    )
    assert declared.published_corpus is True
    note = declared.published_corpus_note or ""
    assert "synthetic" in note.lower() and "qualification/lanes.py" in note
    assert "personal" in note and "third-party" in note
    asserted_at = declared.published_corpus_asserted_at or ""
    assert datetime.fromisoformat(asserted_at.replace("Z", "+00:00")).tzinfo is not None
    # Without the assertion the lane records a refusal naming the three fields and makes no
    # attempt to build a shelf.
    incomplete = tmp_path / "settings.yaml"
    incomplete.write_text(
        committed.read_text()
        .replace("published_corpus: true\n", "published_corpus: false\n")
        .split("    published_corpus_note:")[0]
        + "drawbridge: {}\nvellric: {}\n"
    )
    record = lanes.lane_bedrock(
        tmp_path / "root", incomplete, model="cohere.embed-v4:0", region="us-east-1", texts=2
    )
    assert record["outcome"] == "not-run"
    assert "published_corpus_asserted_at" in record["notes"]["reason"]
    assert not (tmp_path / "root").exists()


def test_bedrock_lane_verdict_requires_reranking_and_the_exported_audit(tmp_path: Path) -> None:
    """Failure mode: the Bedrock lane passes on fused order (the reranker model absent, as on
    attempt 3), or without a successful retention check or any embedding request in the audit
    it exports; or the export silently yields an empty artifact when the store has no audit
    directory and the lane still passes."""
    lanes = _load("lanes")
    good = [
        {"kind": "bedrock_retention_check", "status": "ok"},
        {"kind": "cloud_embed_call", "status": "ok"},
        {"kind": "cloud_embed_call", "status": "ok"},
    ]
    assert lanes.bedrock_verdict(lagging=False, hits=1, rerank_status="applied", audit=good) == (
        "passed",
        [],
    )
    outcome, problems = lanes.bedrock_verdict(
        lagging=False, hits=1, rerank_status="failed", audit=good
    )
    assert outcome == "failed" and any("fused order" in p for p in problems)
    outcome, problems = lanes.bedrock_verdict(
        lagging=False, hits=1, rerank_status="applied", audit=[good[1]]
    )
    assert outcome == "failed" and any("retention check" in p for p in problems)
    outcome, problems = lanes.bedrock_verdict(
        lagging=False,
        hits=1,
        rerank_status="applied",
        audit=[{"kind": "bedrock_retention_check", "status": "error"}],
    )
    assert outcome == "failed"
    assert any("retention check" in p for p in problems)
    assert any("embedding request" in p for p in problems)
    # The export copies every record of every day file, in order, and an absent directory
    # yields an empty artifact that the verdict then refuses.
    store = tmp_path / "store"
    (store / "events" / "cloud-egress").mkdir(parents=True)
    (store / "events" / "cloud-egress" / "2026-11-07.jsonl").write_text(
        "\n".join(json.dumps(r) for r in good[:2]) + "\n"
    )
    (store / "events" / "cloud-egress" / "2026-11-08.jsonl").write_text(json.dumps(good[2]) + "\n")
    exported = lanes.export_bedrock_audit(store, tmp_path / "bedrock-audit.jsonl")
    assert exported == good
    assert (tmp_path / "bedrock-audit.jsonl").read_text().count("\n") == 3
    # The export runs even when the body raises (Palace audits a refused request before
    # raising), the body's exception propagates, and an export failure is reported rather
    # than masking the body's outcome.
    audit_dir = store / "events" / "cloud-egress"
    with pytest.raises(RuntimeError, match="bedrock refused"):
        with lanes.exporting_audit(store, tmp_path / "after-failure.jsonl") as exported:
            (audit_dir / "2026-11-09.jsonl").write_text(
                json.dumps({"kind": "cloud_embed_call", "status": "error"}) + "\n"
            )
            raise RuntimeError("bedrock refused")
    assert len(exported["records"]) == 4 and exported["error"] is None
    assert (tmp_path / "after-failure.jsonl").read_text().count("\n") == 4
    with lanes.exporting_audit(store, tmp_path / "unwritable" / "x.jsonl") as failed:
        pass
    assert failed["records"] == [] and "FileNotFoundError" in failed["error"]
    empty = lanes.export_bedrock_audit(tmp_path / "no-store", tmp_path / "empty.jsonl")
    assert empty == [] and (tmp_path / "empty.jsonl").read_text() == ""
    assert lanes.bedrock_verdict(lagging=False, hits=1, rerank_status="applied", audit=empty)[
        0
    ] == ("failed")


def test_lane_commands_extract_the_archive_install_the_product_and_propagate_the_exit() -> None:
    """Failure mode: the instance command omits a requested lane, reads another run's prefix,
    runs the development-only setup, asks the instance's package manager for a tool, uploads
    nothing when the install step fails before the lanes, or swallows the lanes' exit
    status."""
    commands = runner.lane_commands(
        "run-1", "bucket-x", ["hermetic", "portability"], None, corpus=False
    )
    joined = "\n".join(commands)
    assert "s3://bucket-x/inputs/run-1/bundle.tar" in joined
    assert "s3://bucket-x/results/run-1/lanes.log" in joined
    assert "tar --no-same-owner -xpf bundle.tar" in joined
    assert "uv sync --project project --locked" in joined
    assert "uv sync --project qualification/vellric --locked" in joined
    assert "./bin/setup" not in joined
    assert "dnf" not in joined
    # The upload runs from an exit trap armed before anything can fail, and the install
    # step's output lands in a result artifact (the first live run failed at `uv sync` with
    # nothing uploaded, so its diagnostics survived only in the command invocation record).
    trap = next(c for c in commands if c.startswith("trap '"))
    assert commands.index(trap) < commands.index(
        next(c for c in commands if "tar --no-same-owner" in c)
    )
    assert "s3://bucket-x/results/run-1/install.log" in trap and trap.endswith("' EXIT")
    assert all(f"/mnt/biblios/{name}" in trap for name in runner.RESULT_ARTIFACTS)
    assert "uv sync --project project --locked --managed-python >>/mnt/biblios/install.log" in (
        joined
    )
    assert 'for tool in git file tesseract; do command -v "$tool"' in joined
    assert "PALACE_MODELS_DIR=$PWD/models" in joined
    assert "s3://bucket-x/results/run-1/bedrock-audit.jsonl" in trap
    assert "--corpus" not in joined
    assert "--lanes hermetic,portability" in joined and "--bedrock-settings" not in joined
    assert joined.endswith('exit "$(cat /mnt/biblios/lanes.exit)"')
    assert "UV_OFFLINE=1" in joined
    with_bedrock = "\n".join(
        runner.lane_commands(
            "run-1",
            "bucket-x",
            ["bedrock"],
            {
                "settings": "/mnt/biblios/bundle/bedrock.yaml",
                "model": "m",
                "region": "r",
                "texts": 2,
            },
            corpus=True,
        )
    )
    assert "--bedrock-settings /mnt/biblios/bundle/bedrock.yaml --bedrock-model m" in with_bedrock
    assert (
        "--bedrock-region r --bedrock-texts 2" in with_bedrock
        and "--corpus ../corpus" in with_bedrock
    )
