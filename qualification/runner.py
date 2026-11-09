"""The qualification runner behind `bin/qualify-aws`: plan, deploy, run, results, teardown.

Every step that provisions, spends, writes to the account or reads it stands behind one
typed confirmation whose packet names the account, region, resources, calls, data to send
and the estimated cost. A dry run renders every packet in order and performs no AWS, model
or CDK call at all: `plan` synthesizes the stacks in-process under the same sanitized
environment and socket guard as `infra/check_qualification.py`, and the other commands walk
their packets with placeholders standing in for the account's answers.

AWS and the CDK are reached only through a `Transport`, so the proofs can observe every
call. `RealTransport` uses the AWS SDK (a named profile or the instance's role) and the
pinned CDK executable; the proofs supply a recording transport and assert that nothing is
called before its confirmation and that `--dry-run` calls nothing.

The run's target (account, region, profile) is recorded by the deploy preflight and
verified by a disclosed read before `run`, `example deploy` and `teardown` touch anything;
the stacks a deploy created are recorded one by one, so a teardown after a declined or
failed later stack removes exactly what exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from audit import AUDIT_NAME, audit_problems, read_audit

REPOSITORY = Path(__file__).resolve().parent.parent
INFRA = REPOSITORY / "infra"
sys.path.insert(0, str(INFRA))
import check_qualification as infra_check  # noqa: E402 - the offline check owns the guard

PRIVATE_STACKS = (
    "biblios-qual-network",
    "biblios-qual-artifacts",
    "biblios-qual-boundary",
    "biblios-qual-custody",
    "biblios-qual-instance",
)
EXAMPLE_STACKS = ("biblios-example-network", "biblios-example-instance")
RUN_TAG = "biblios:run"
STACK_TAG = "aws:cloudformation:stack-name"
AL2023_ARM64 = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
AL2023_X86 = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-x86_64"
LANE_SCRIPT = "qualification/lanes.py"
DEFAULT_LANES = ("hermetic", "ingestion", "portability")
ALL_LANES = (*DEFAULT_LANES, "bedrock")
RESULT_ARTIFACTS = ("lanes.json", "lanes.log", "lanes.exit", "install.log", AUDIT_NAME)
DELETE_BATCH = 1000
RUN_ID_PATTERN = __import__("re").compile(r"^[a-z0-9-]{1,64}$")
# Native states that mean the resource no longer bills or exists.
GONE_STATES = {"not-found", "terminated", "deleted", "deleting", "shutting-down"}
MOUNT = "/mnt/biblios"

# What a dry run shows where the account would have answered.
PLACEHOLDER_OUTPUTS: dict[str, dict[str, str]] = {
    "biblios-qual-network": {"SubnetId": "<SubnetId>", "InstanceGroupId": "<InstanceGroupId>"},
    "biblios-qual-artifacts": {"BucketName": "<BucketName>"},
    "biblios-qual-boundary": {"BoundaryArn": "<BoundaryArn>"},
    "biblios-qual-custody": {
        "InstanceProfileName": "<InstanceProfileName>",
        "InstanceRoleName": "<InstanceRoleName>",
    },
    "biblios-qual-instance": {"InstanceId": "<InstanceId>"},
    "biblios-example-network": {"SubnetId": "<SubnetId>", "InstanceGroupId": "<InstanceGroupId>"},
    "biblios-example-instance": {"InstanceId": "<InstanceId>"},
}
PLACEHOLDER_IDENTITY = {"Account": "<account>", "Arn": "<caller arn>", "UserId": "<user id>"}


class Refused(Exception):
    """A boundary the runner will not cross: missing confirmation, missing input, mismatch."""


class QualificationFailed(Refused):
    """The lanes ran and did not all pass, or the command never reached a known success."""


class TeardownIncomplete(QualificationFailed):
    """Resources of the run are verifiably still live after the destroys."""


# ---- transports ----------------------------------------------------------------------------


class Transport(Protocol):
    def aws(self, service: str, operation: str, **params: Any) -> dict[str, Any]: ...

    def cdk(self, argv: list[str], *, env: dict[str, str]) -> int: ...


class RefusingTransport:
    """Dry run: every call is a refusal. The runner never reaches it in a dry run; if a code
    path forgets its dry-run branch, this is what stops it."""

    def aws(self, service: str, operation: str, **params: Any) -> dict[str, Any]:
        raise Refused(f"dry run: {service}.{operation} not called")

    def cdk(self, argv: list[str], *, env: dict[str, str]) -> int:
        raise Refused(f"dry run: cdk {' '.join(argv)} not called")


class RealTransport:
    """The SDK through one named profile and region, and the pinned CDK executable."""

    def __init__(self, *, profile: str, region: str, cdk_executable: Path, node_bin: Path) -> None:
        import boto3  # imported here so the proofs never need it

        self.profile, self.region = profile, region
        self.session = boto3.Session(profile_name=profile, region_name=region)
        self.cdk_executable, self.node_bin = cdk_executable, node_bin

    def aws(self, service: str, operation: str, **params: Any) -> dict[str, Any]:
        client = self.session.client(service)
        return getattr(client, operation)(**params) or {}

    def cdk(self, argv: list[str], *, env: dict[str, str]) -> int:
        run_env = dict(env)
        run_env["PATH"] = f"{self.node_bin}{os.pathsep}{run_env.get('PATH', '')}"
        run_env["AWS_PROFILE"] = self.profile
        run_env["AWS_REGION"] = self.region
        run_env["CDK_DISABLE_VERSION_CHECK"] = "1"
        completed = subprocess.run(
            [str(self.cdk_executable), *argv], cwd=INFRA, env=run_env, check=False
        )
        return completed.returncode


# ---- journal and confirmation -------------------------------------------------------------


@dataclass
class Journal:
    path: Path

    def append(self, kind: str, **record: Any) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        row = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "kind": kind, **record}
        with self.path.open("a") as handle:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")

    def has(self, kind: str) -> bool:
        return self.path.is_file() and f'"kind": "{kind}"' in self.path.read_text()


Confirm = Callable[[dict[str, Any]], bool]


def render_packet(packet: dict[str, Any]) -> str:
    lines = []
    for key, value in packet.items():
        if key == "token":
            continue
        text = value if isinstance(value, str) else json.dumps(value, default=str, indent=1)
        lines.append(f"{key}: {text}")
    return "\n".join(lines)


def terminal_confirm(packet: dict[str, Any]) -> bool:
    """Print the packet and require its exact token typed back; anything else declines."""
    print("\n=== action-time confirmation ===")
    print(render_packet(packet))
    print(f"type exactly `{packet['token']}` to proceed, anything else to stop")
    try:
        answer = input("> ")
    except EOFError:
        return False
    return answer.strip() == packet["token"]


# ---- the runner ---------------------------------------------------------------------------


@dataclass
class Runner:
    evidence: Path
    transport: Transport
    confirm: Confirm
    dry_run: bool
    profile: str
    region: str
    run_id: str
    journal: Journal = field(init=False)
    identity: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not RUN_ID_PATTERN.fullmatch(self.run_id):
            raise Refused("run id must match [a-z0-9-]{1,64}")
        self.evidence.mkdir(parents=True, exist_ok=True)
        self.journal = Journal(self.evidence / "journal.jsonl")
        target = self._read("target.json")
        if target:
            self.identity = {"Account": target["account"], "Arn": target["arn"]}

    # -- evidence files --

    def _read(self, name: str) -> dict[str, Any] | None:
        path = self.evidence / name
        return json.loads(path.read_text()) if path.is_file() else None

    def _write(self, name: str, data: dict[str, Any]) -> None:
        if self.dry_run:
            self.journal.append("would-write", file=name)
            return
        (self.evidence / name).write_text(json.dumps(data, indent=1, sort_keys=True))

    # -- boundaries --

    def gate(self, packet: dict[str, Any]) -> None:
        """Every account-touching step passes here. A dry run renders the whole packet and
        walks on; a real run proceeds only on the exact token."""
        packet = {
            "account": self.identity.get("Account", "<unknown until the read-only preflight>"),
            "region": self.region,
            "profile": self.profile,
            "run_id": self.run_id,
            **packet,
        }
        self.journal.append("packet", packet=packet, dry_run=self.dry_run)
        if self.dry_run:
            print(f"\n=== dry run: would ask for `{packet['token']}` ===")
            print(render_packet(packet))
            return
        if not self.confirm(packet):
            self.journal.append("declined", token=packet["token"])
            raise Refused(f"not confirmed: {packet['token']}")
        self.journal.append("confirmed", token=packet["token"])

    def aws(
        self, service: str, operation: str, *, dry: Any = None, **params: Any
    ) -> dict[str, Any]:
        safe = _journal_params(params)
        if self.dry_run:
            self.journal.append("would-call", service=service, operation=operation, params=safe)
            return dry if dry is not None else {}
        response = self.transport.aws(service, operation, **params)
        self.journal.append(
            "aws",
            service=service,
            operation=operation,
            params=safe,
            response=_identifiers(response),
        )
        return response

    def cdk(self, argv: list[str]) -> None:
        full = [*argv, "--output", str(self.evidence / "cdk.out")]
        if self.dry_run:
            self.journal.append("would-run", argv=full)
            return
        env = dict(os.environ)
        env["CDK_OUTDIR"] = str(self.evidence / "cdk.out")
        self.journal.append("cdk", argv=full)
        code = self.transport.cdk(full, env=env)
        self.journal.append("cdk-exit", argv=full, exit=code)
        if code != 0:
            raise Refused(f"cdk {' '.join(argv[:2])} exited {code}")

    def _list(self, bucket: str, prefix: str) -> list[dict[str, Any]]:
        """Every object under a prefix, following continuation tokens."""
        if self.dry_run:
            self.journal.append(
                "would-call",
                service="s3",
                operation="list_objects_v2",
                params={"Bucket": bucket, "Prefix": prefix},
            )
            return []
        items: list[dict[str, Any]] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix}
            if token:
                params["ContinuationToken"] = token
            page = self.aws("s3", "list_objects_v2", **params)
            items.extend(page.get("Contents", []))
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token:
                return items

    # -- plan: offline, in-process synthesis --

    def plan(self, *, lanes: list[str], example: bool) -> dict[str, Any]:
        """What a deploy would create, from the synthesized templates; no call of any kind."""
        out = synthesize_offline()
        stacks = list(PRIVATE_STACKS) + (list(EXAMPLE_STACKS) if example else [])
        summary: dict[str, Any] = {"stacks": {}, "lanes": lanes, "cost": cost_estimate()}
        for name in stacks:
            doc = json.loads((out / f"{name}.template.json").read_text())
            kinds: dict[str, int] = {}
            for resource in doc["Resources"].values():
                kinds[resource["Type"]] = kinds.get(resource["Type"], 0) + 1
            summary["stacks"][name] = {
                "resources": kinds,
                "parameters": sorted(doc.get("Parameters", {})),
            }
        self.journal.append("plan", summary=summary)
        return summary

    # -- identity --

    def preflight(self, *, arch: str) -> dict[str, Any]:
        """The read-only calls a deploy needs, behind their own confirmation; records the
        run's target so every later command can verify it."""
        parameter = AL2023_ARM64 if arch == "arm64" else AL2023_X86
        self.gate(
            {
                "token": "yes-read-account",
                "action": "read-only preflight",
                "calls": [
                    "sts.get_caller_identity",
                    "ec2.describe_managed_prefix_lists (the region's S3 list)",
                    f"ssm.get_parameter {parameter}",
                ],
                "creates": "nothing",
                "cost": "none",
                "data_sent": "none",
            }
        )
        self.identity = self.aws("sts", "get_caller_identity", dry=PLACEHOLDER_IDENTITY)
        lists = self.aws(
            "ec2",
            "describe_managed_prefix_lists",
            dry={"PrefixLists": [{"PrefixListId": "<pl-s3>"}]},
            Filters=[{"Name": "prefix-list-name", "Values": [f"com.amazonaws.{self.region}.s3"]}],
        )
        prefix = lists["PrefixLists"][0]["PrefixListId"]
        image = self.aws(
            "ssm", "get_parameter", dry={"Parameter": {"Value": "<ami>"}}, Name=parameter
        )["Parameter"]["Value"]
        target = {
            "account": self.identity["Account"],
            "arn": self.identity.get("Arn", ""),
            "profile": self.profile,
            "region": self.region,
            "architecture": arch,
            "s3_prefix_list": prefix,
            "image_id": image,
        }
        self._write("target.json", target)
        return target

    def verify_identity(self) -> dict[str, Any]:
        """A disclosed read before anything that changes or spends: the caller now must be
        the account the preflight recorded, in the same region and profile."""
        target = self._read("target.json")
        if target is None:
            if not self.dry_run:
                raise Refused("no preflight recorded for this run; `deploy` records the target")
            target = {
                "account": "<account>",
                "arn": "<caller arn>",
                "profile": self.profile,
                "region": self.region,
                "architecture": "arm64",
                "s3_prefix_list": "<pl-s3>",
                "image_id": "<ami>",
            }
        self.gate(
            {
                "token": "yes-verify-identity",
                "action": "read-only identity check against the recorded target",
                "calls": ["sts.get_caller_identity"],
                "recorded_account": target["account"],
                "recorded_region": target["region"],
                "recorded_profile": target["profile"],
                "creates": "nothing",
                "cost": "none",
                "data_sent": "none",
            }
        )
        now = self.aws("sts", "get_caller_identity", dry=PLACEHOLDER_IDENTITY)
        if not self.dry_run:
            mismatch = [
                f"{name}: recorded {recorded}, now {current}"
                for name, recorded, current in (
                    ("account", target["account"], now.get("Account")),
                    ("region", target["region"], self.region),
                    ("profile", target["profile"], self.profile),
                )
                if recorded != current
            ]
            if mismatch:
                self.journal.append("target-mismatch", details=mismatch)
                raise Refused("target mismatch, refusing: " + "; ".join(mismatch))
        self.identity = now
        return target

    # -- deploy --

    def deploy(
        self,
        *,
        boundary_arn: str,
        role_path: str,
        instance_type: str,
        data_gib: int,
        arch: str,
        bedrock_model_arn: str | None,
        example: bool = False,
        stacks: tuple[str, ...] | None = None,
    ) -> None:
        """Deploy the private shape, or the example, one stack at a time.

        ``stacks`` selects a contiguous run of the private stacks in their fixed order, so a
        run can stop after the artifacts stack, create its run-specific boundary from the
        bucket's real name, and continue with custody and instance. A later invocation
        continues from the recorded state: a stack already recorded as created is refused
        rather than redeployed, a selected stack whose predecessor is not recorded as
        created is refused, and the recorded target is verified instead of re-read.
        """
        if example:
            target = self.verify_identity()
            base = self._outputs(self._state("state.json"))
            if not {"BucketName", "InstanceProfileName"} <= set(base):
                raise Refused(
                    "the example reuses the run's bucket and instance profile; deploy the "
                    "private shape first"
                )
            outputs = {k: base[k] for k in ("BucketName", "InstanceProfileName")}
            selected, state_name = EXAMPLE_STACKS, "example-state.json"
            state: dict[str, Any] = {
                "schema": "biblios.qualification-state/1",
                "order": [],
                "stacks": {},
            }
        else:
            selected = stacks or PRIVATE_STACKS
            unknown = [s for s in selected if s not in PRIVATE_STACKS]
            if unknown:
                raise Refused(f"unknown stacks: {', '.join(unknown)}")
            positions = [PRIVATE_STACKS.index(s) for s in selected]
            if positions != list(range(positions[0], positions[0] + len(positions))):
                raise Refused(
                    "stacks must be a contiguous run in order: " + ", ".join(PRIVATE_STACKS)
                )
            existing = self._read("state.json")
            if existing is None:
                if positions[0] != 0:
                    raise Refused(
                        f"no deployed environment recorded; the first invocation must start "
                        f"with {PRIVATE_STACKS[0]}"
                    )
                target = self.preflight(arch=arch)
                outputs = {}
                state = {"schema": "biblios.qualification-state/1", "order": [], "stacks": {}}
            else:
                created = [
                    s for s in existing["order"] if existing["stacks"][s]["status"] == "created"
                ]
                already = [s for s in selected if s in created]
                if already:
                    raise Refused(
                        "already recorded as created, refusing to redeploy: " + ", ".join(already)
                    )
                expected = list(PRIVATE_STACKS[: positions[0]])
                if created != expected:
                    raise Refused(
                        f"recorded created stacks {created} do not match the predecessors "
                        f"{expected} of the selected stacks"
                    )
                target = self.verify_identity()
                outputs = self._outputs(existing)
                state = existing
            state_name = "state.json"
        cost = cost_estimate()
        for name in selected:
            params = self._parameters(
                name,
                target,
                outputs,
                boundary_arn=boundary_arn,
                role_path=role_path,
                instance_type=instance_type,
                data_gib=data_gib,
                bedrock_model_arn=bedrock_model_arn,
            )
            shown = dict(params)
            if "WorkloadBoundaryArn" in shown:
                shown["WorkloadBoundaryArn"] = "(as supplied)"
            self.gate(
                {
                    "token": f"yes-deploy-{name}",
                    "action": f"cdk deploy {name}",
                    "creates": _creates(name, params),
                    "parameters": shown,
                    "cost": cost["per_stack"].get(name, "no assumption recorded"),
                    "data_sent": "the synthesized template and these parameters",
                    "rollback": f"cdk destroy {name}",
                }
            )
            outputs_file = self.evidence / f"{name}.outputs.json"
            argv = [
                "deploy",
                name,
                "--require-approval",
                "never",
                "--outputs-file",
                str(outputs_file),
            ]
            for key, value in params.items():
                argv += ["--parameters", f"{name}:{key}={value}"]
            self.cdk(argv)
            produced = (
                dict(PLACEHOLDER_OUTPUTS[name])
                if self.dry_run
                else json.loads(outputs_file.read_text()).get(name, {})
                if outputs_file.is_file()
                else {}
            )
            outputs.update(produced)
            state["order"].append(name)
            state["stacks"][name] = {
                "status": "created",
                "outputs": produced,
                "parameters": {k: v for k, v in params.items() if k != "WorkloadBoundaryArn"},
            }
            self.journal.append("outputs", stack=name, outputs=produced)
            self._write(state_name, state)

    def _state(self, name: str) -> dict[str, Any]:
        state = self._read(name)
        if state is None:
            if not self.dry_run:
                raise Refused(f"no deployed environment recorded at {self.evidence / name}")
            stacks = EXAMPLE_STACKS if name.startswith("example") else PRIVATE_STACKS
            state = {
                "schema": "biblios.qualification-state/1",
                "order": list(stacks),
                "stacks": {
                    s: {"status": "created", "outputs": dict(PLACEHOLDER_OUTPUTS[s])}
                    for s in stacks
                },
            }
        return state

    @staticmethod
    def _outputs(state: dict[str, Any]) -> dict[str, str]:
        merged: dict[str, str] = {}
        for name in state["order"]:
            entry = state["stacks"][name]
            if entry["status"] == "created":
                merged.update(entry["outputs"])
        return merged

    def _parameters(
        self,
        name: str,
        target: dict[str, Any],
        outputs: dict[str, str],
        *,
        boundary_arn: str,
        role_path: str,
        instance_type: str,
        data_gib: int,
        bedrock_model_arn: str | None,
    ) -> dict[str, str]:
        params = {"RunId": self.run_id}
        if name == "biblios-qual-network":
            params |= {
                "S3PrefixList": target["s3_prefix_list"],
                "BedrockEndpoints": "true" if bedrock_model_arn else "false",
            }
        elif name == "biblios-qual-boundary":
            params |= {
                "ArtifactBucket": outputs["BucketName"],
                "BedrockModelArn": bedrock_model_arn or "unset",
            }
        elif name == "biblios-qual-custody":
            boundary = boundary_arn or outputs.get("BoundaryArn")
            if not boundary:
                raise Refused(
                    "no permissions boundary: deploy biblios-qual-boundary first or pass "
                    "--boundary-arn"
                )
            params |= {
                "ArtifactBucket": outputs["BucketName"],
                "WorkloadBoundaryArn": boundary,
                "RolePath": role_path,
                "BedrockModelArn": bedrock_model_arn or "unset",
            }
        elif name in {"biblios-qual-instance", "biblios-example-instance"}:
            params |= {
                "SubnetId": outputs["SubnetId"],
                "InstanceGroupId": outputs["InstanceGroupId"],
                "ImageId": target["image_id"],
                "InstanceProfileName": outputs["InstanceProfileName"],
                "InstanceType": instance_type,
                "DataVolumeGiB": str(data_gib),
            }
        return params

    # -- run --

    def run(
        self,
        *,
        bundle: Path | None,
        lanes: list[str],
        keep: bool,
        bedrock: dict[str, Any] | None,
        fixture: Path | None = None,
        fixture_command: Path | None = None,
    ) -> None:
        """Stage and run either Biblios's own lanes (``bundle``) or a foreign fixture: one
        archive and one shell script supplied by its owner, staged under the run's inputs
        prefix and executed through Session Manager with the script shown in the packet.
        Everything the instance leaves under the run's results prefix is collected."""
        if fixture is not None:
            if bundle is not None or fixture_command is None:
                raise Refused("--fixture needs --fixture-command and excludes --bundle")
            return self._run_fixture(fixture=fixture, script=fixture_command, keep=keep)
        if bundle is None:
            raise Refused("run needs --bundle, or --fixture with --fixture-command")
        if "bedrock" in lanes and bedrock is None:
            raise Refused(
                "the Bedrock lane needs --bedrock-settings, --bedrock-model, --bedrock-texts "
                "and --bedrock-cost-cap"
            )
        if bedrock is not None and "bedrock" not in lanes:
            raise Refused("Bedrock options were given without `--lane bedrock`")
        manifest_file, archive = bundle / "manifest.json", bundle / "bundle.tar"
        if manifest_file.is_file() and archive.is_file():
            manifest = json.loads(manifest_file.read_text())
        elif self.dry_run:
            manifest = {
                "commit": "<commit>",
                "files": {},
                "archive": {"bytes": 0},
                "corpus_files": 0,
            }
        else:
            raise Refused(f"bundle incomplete: {bundle} needs bundle.tar and manifest.json")
        if manifest["archive"].get("sha256") and manifest["archive"]["sha256"] != sha256_of(
            archive
        ):
            raise Refused("bundle.tar does not match its manifest")
        if manifest.get("source", "commit") != "commit":
            raise Refused(
                "the bundle was built from the working tree (a development proof); a "
                "qualification run stages only a bundle built from a commit"
            )
        outputs = self._outputs(self._state("state.json"))
        if not {"BucketName", "InstanceId"} <= set(outputs):
            raise Refused("the instance and the bucket are not both recorded as created")
        self.verify_identity()
        bucket, prefix = outputs["BucketName"], f"inputs/{self.run_id}"
        self.gate(
            {
                "token": "yes-stage-bundle",
                "action": "upload the bundle archive and its manifest to the artifacts bucket",
                "calls": [
                    f"s3.upload_file bundle.tar -> s3://{bucket}/{prefix}/bundle.tar",
                    f"s3.upload_file manifest.json -> s3://{bucket}/{prefix}/manifest.json",
                ],
                "data_sent": (
                    f"one archive of {manifest['archive'].get('bytes', 0)} bytes holding "
                    f"{len(manifest['files'])} files: the checkout at {manifest['commit'][:12]}, "
                    f"the managed runtime, uv, the dependency cache, "
                    f"{manifest.get('corpus_files', 0)} corpus files"
                    + (
                        f", prebuilt tools: {', '.join(manifest['tools'])}"
                        if manifest.get("tools")
                        else ""
                    )
                ),
                "cost": cost_estimate()["storage_note"],
                "rollback": "teardown removes the run's objects",
            }
        )
        for path in (archive, manifest_file):
            self.aws(
                "s3", "upload_file", Filename=str(path), Bucket=bucket, Key=f"{prefix}/{path.name}"
            )
        if bedrock is not None:
            self.gate(
                {
                    "token": "yes-bedrock-calls",
                    "action": "invoke the embedding model from the instance role",
                    "model": bedrock["model"],
                    "model_region": bedrock["region"],
                    "calls": (
                        "bedrock.get_account_data_retention x1 (refuses unless `none`); "
                        f"bedrock-runtime.invoke_model for at most {bedrock['texts']} texts, "
                        "documents and queries together, in requests of at most 96"
                    ),
                    "bound": (
                        "the lane refuses settings naming another provider, model or region, "
                        "and a text count below what its fixed input needs; it sends its one "
                        "short document and one query and nothing else"
                    ),
                    "cost": (
                        f"operator's figure for {bedrock['texts']} texts: {bedrock['cost_cap']} "
                        "(a stated bound, not an enforced meter)"
                    ),
                    "data_sent": "the lane's fixed document text and query",
                    "rollback": "none needed; no resource is created",
                }
            )
        commands = lane_commands(
            self.run_id, bucket, lanes, bedrock, corpus=bool(manifest.get("corpus_files"))
        )
        self.gate(
            {
                "token": "yes-run-lanes",
                "action": "run the lanes on the instance through Session Manager",
                "calls": [
                    "ssm.send_command (AWS-RunShellScript)",
                    "ssm.get_command_invocation until done",
                    f"s3.get_object for {', '.join(RESULT_ARTIFACTS)}",
                ],
                "commands": commands,
                "cost": "instance hours already stated at deploy",
                "data_sent": "the command text above",
                "keep_environment": keep,
            }
        )
        self.journal.append("lanes-dispatched", lanes=lanes, keep=keep)
        sent = self.aws(
            "ssm",
            "send_command",
            dry={"Command": {"CommandId": "<command>"}},
            InstanceIds=[outputs["InstanceId"]],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands, "executionTimeout": ["7200"]},
            TimeoutSeconds=600,
        )
        status, invocation = self._wait_command(sent["Command"]["CommandId"], outputs["InstanceId"])
        self._write(
            "command-invocation.json",
            {
                "status": status,
                "stdout": invocation.get("StandardOutputContent", ""),
                "stderr": invocation.get("StandardErrorContent", ""),
            },
        )
        self.journal.append("lanes-command", status=status, keep=keep)
        collected = self._collect(bucket)
        if keep:
            self.journal.append("keep", chosen_by="operator", environment="left up")
        self._judge(status, lanes, collected)

    def _run_fixture(self, *, fixture: Path, script: Path, keep: bool) -> None:
        if not self.dry_run and not fixture.is_file():
            raise Refused(f"fixture archive not found: {fixture}")
        if not self.dry_run and not script.is_file():
            raise Refused(f"fixture command file not found: {script}")
        script_text = script.read_text() if script.is_file() else "<fixture command>"
        size = fixture.stat().st_size if fixture.is_file() else 0
        outputs = self._outputs(self._state("state.json"))
        if not {"BucketName", "InstanceId"} <= set(outputs):
            raise Refused("the instance and the bucket are not both recorded as created")
        self.verify_identity()
        bucket, key = outputs["BucketName"], f"inputs/{self.run_id}/fixture/{fixture.name}"
        self.gate(
            {
                "token": "yes-stage-fixture",
                "action": "upload the foreign fixture archive to the artifacts bucket",
                "calls": [f"s3.upload_file {fixture.name} -> s3://{bucket}/{key}"],
                "data_sent": f"one archive of {size} bytes supplied by its owner ({fixture})",
                "cost": cost_estimate()["storage_note"],
                "rollback": "teardown removes the run's objects",
            }
        )
        self.aws("s3", "upload_file", Filename=str(fixture), Bucket=bucket, Key=key)
        commands = fixture_commands(self.run_id, bucket, fixture.name, script_text)
        self.gate(
            {
                "token": "yes-run-fixture",
                "action": "run the owner's fixture script on the instance through Session "
                "Manager; its model calls and their bound are the owner's confirmation",
                "calls": [
                    "ssm.send_command (AWS-RunShellScript)",
                    "ssm.get_command_invocation until done",
                    f"s3.list_objects_v2 and s3.get_object under results/{self.run_id}/",
                ],
                "commands": commands,
                "cost": "instance hours already stated at deploy; model calls per the owner's "
                "bound",
                "data_sent": "the command text above, including the owner's script",
                "keep_environment": keep,
            }
        )
        self.journal.append("fixture-dispatched", archive=fixture.name, keep=keep)
        sent = self.aws(
            "ssm",
            "send_command",
            dry={"Command": {"CommandId": "<command>"}},
            InstanceIds=[outputs["InstanceId"]],
            DocumentName="AWS-RunShellScript",
            Parameters={"commands": commands, "executionTimeout": ["7200"]},
            TimeoutSeconds=600,
        )
        status, invocation = self._wait_command(sent["Command"]["CommandId"], outputs["InstanceId"])
        self._write(
            "command-invocation.json",
            {
                "status": status,
                "stdout": invocation.get("StandardOutputContent", ""),
                "stderr": invocation.get("StandardErrorContent", ""),
            },
        )
        self.journal.append("fixture-command", status=status, keep=keep)
        collected = self._collect_results(bucket)
        if keep:
            self.journal.append("keep", chosen_by="operator", environment="left up")
        if self.dry_run:
            return
        exit_file = self.evidence / "results" / "fixture.exit"
        exit_text = exit_file.read_text().strip() if exit_file.is_file() else "missing"
        problems = []
        if status != "Success":
            problems.append(f"command status {status}")
        if exit_text != "0":
            problems.append(f"fixture exit {exit_text}")
        self.journal.append("verdict", passed=not problems, problems=problems, collected=collected)
        if problems:
            raise QualificationFailed("fixture did not pass: " + "; ".join(problems))
        print(f"FIXTURE RUN PASSED: {len(collected)} result objects collected")

    def _collect_results(self, bucket: str) -> list[str]:
        """Every object under the run's results prefix, downloaded under evidence/results/
        (existing local copies are kept); the lane artifacts are mirrored at the evidence
        root for the renderer. Returns the keys now retained locally."""
        prefix = f"results/{self.run_id}/"
        retained: list[str] = []
        for item in self._list(bucket, prefix):
            key = item["Key"]
            relative = key[len(prefix) :]
            if not relative:
                continue
            local = self.evidence / "results" / relative
            if not local.is_file():
                try:
                    response = self.aws("s3", "get_object", Bucket=bucket, Key=key)
                    local.parent.mkdir(parents=True, exist_ok=True)
                    local.write_bytes(response["Body"].read())
                except Exception as error:  # noqa: BLE001 - recorded, and refused below
                    self.journal.append("not-collected", artifact=key, error=repr(error))
                    raise Refused(f"result object exists remotely but could not be retained: {key}")
            retained.append(key)
            if relative in RESULT_ARTIFACTS and not (self.evidence / relative).is_file():
                (self.evidence / relative).write_bytes(local.read_bytes())
        self.journal.append("results-collected", keys=retained)
        return retained

    def _wait_command(self, command_id: str, instance_id: str) -> tuple[str, dict[str, Any]]:
        if self.dry_run:
            return "Success", {}
        for _ in range(720):
            time.sleep(10)
            response = self.aws(
                "ssm", "get_command_invocation", CommandId=command_id, InstanceId=instance_id
            )
            if response["Status"] in {"Success", "Failed", "Cancelled", "TimedOut"}:
                return response["Status"], response
        return "Unknown", {}

    def _collect(self, bucket: str) -> dict[str, bool]:
        """Fetch every result artifact the instance uploaded; record what is retained."""
        collected: dict[str, bool] = {}
        for name in RESULT_ARTIFACTS:
            local = self.evidence / name
            if local.is_file():
                collected[name] = True
                continue
            if self.dry_run:
                collected[name] = False
                continue
            try:
                response = self.aws(
                    "s3", "get_object", Bucket=bucket, Key=f"results/{self.run_id}/{name}"
                )
                local.write_bytes(response["Body"].read())
                collected[name] = True
            except Exception as error:  # noqa: BLE001 - a missing artifact is a recorded fact
                self.journal.append("not-collected", artifact=name, error=repr(error))
                collected[name] = False
        self._write("collected.json", collected)
        return collected

    def _judge(self, status: str, lanes: list[str], collected: dict[str, bool]) -> None:
        """Success is the command's known success plus every requested lane passed."""
        if self.dry_run:
            return
        problems: list[str] = []
        if status != "Success":
            problems.append(f"command status {status}")
        if not collected.get("lanes.json"):
            problems.append("lanes.json was not produced")
            outcomes: dict[str, dict[str, Any]] = {}
        else:
            document = json.loads((self.evidence / "lanes.json").read_text())
            outcomes = {r["lane"]: r for r in document.get("lanes", [])}
        for lane in lanes:
            record = outcomes.get(lane)
            if record is None:
                if collected.get("lanes.json"):
                    problems.append(f"{lane}: no record")
            elif record["outcome"] != "passed":
                reason = (record.get("notes") or {}).get("reason") or (
                    record.get("notes") or {}
                ).get("error")
                problems.append(f"{lane}: {record['outcome']}" + (f" ({reason})" if reason else ""))
            elif lane == "bedrock":
                problems.extend(self._bedrock_audit_problems(collected))
        self.journal.append("verdict", passed=not problems, problems=problems)
        if problems:
            raise QualificationFailed("qualification did not pass: " + "; ".join(problems))
        print("QUALIFICATION RUN PASSED: " + ", ".join(lanes))

    def _bedrock_audit_problems(self, collected: dict[str, bool]) -> list[str]:
        """A passed Bedrock lane is evidence only with its exported audit retained: at least
        one successful retention check and one embedding request, read from the artifact
        itself, never from the lane's own counts."""
        if not collected.get(AUDIT_NAME):
            return [f"bedrock: passed on the instance but {AUDIT_NAME} was not retained"]
        records = read_audit(self.evidence / AUDIT_NAME)
        return [f"bedrock: {AUDIT_NAME} holds {p}" for p in audit_problems(records)]

    # -- qualify: one integrated workflow --

    def qualify(
        self,
        *,
        deploy_options: dict[str, Any],
        run_options: dict[str, Any],
        keep: bool,
    ) -> None:
        """Set up, run, collect and tear down in one invocation, every step still behind
        its own confirmation. A failure after any stack exists enters the scoped teardown
        of exactly what was recorded; the original failure is raised afterwards. ``keep``
        leaves the environment up and says so."""
        failure: BaseException | None = None
        try:
            self.deploy(**deploy_options)
            self.run(keep=keep, **run_options)
        except BaseException as error:  # noqa: BLE001 - cleanup first, then the truth
            failure = error
            self.journal.append("qualify-failed", error=repr(error))
            print(f"qualify: {error}; entering scoped teardown", file=sys.stderr)
        if keep and failure is None:
            return
        if self._read("state.json") is None and not self.dry_run:
            if failure is not None:
                raise failure
            return
        try:
            self.teardown()
        except BaseException as cleanup:  # noqa: BLE001 - both failures are reported
            self.journal.append("qualify-teardown-failed", error=repr(cleanup))
            if failure is not None:
                raise Refused(f"{failure}; and teardown stopped: {cleanup}") from cleanup
            raise
        if failure is not None:
            raise failure

    # -- teardown --

    def teardown(self, *, example: bool = False) -> None:
        state_name = "example-state.json" if example else "state.json"
        state = self._state(state_name)
        created = [n for n in state["order"] if state["stacks"][n]["status"] == "created"]
        if not created:
            raise Refused("nothing recorded as created; nothing to tear down")
        self.verify_identity()
        outputs = self._outputs(state)
        skip: dict[str, str] = {}
        if not example and "BucketName" in outputs:
            foreign = self._empty_run_prefixes(outputs["BucketName"])
            if foreign:
                skip["biblios-qual-artifacts"] = (
                    f"the bucket still holds {foreign} objects outside this run's prefixes; "
                    "it is retained, not destroyed"
                )
        for name in reversed(created):
            if name in skip:
                self.journal.append("retained", stack=name, reason=skip[name])
                print(f"RETAINED {name}: {skip[name]}")
                continue
            self.gate(
                {
                    "token": f"yes-destroy-{name}",
                    "action": f"cdk destroy {name}",
                    "destroys": _creates(name, state["stacks"][name].get("parameters", {})),
                    "cost": "stops the hourly charges of that stack",
                    "data_sent": "the stack name",
                    "rollback": "redeploy creates new resources; nothing is restored",
                }
            )
            self.cdk(["destroy", name, "--force"])
            state["stacks"][name]["status"] = "destroyed"
            self._write(state_name, state)
        inventory = self._inventory(set(created) - set(skip))
        self._write("example-inventory.json" if example else "inventory.json", inventory)
        live = inventory["live"]
        if live:
            print(f"TEARDOWN INCOMPLETE: {len(live)} resources are still live; see the inventory")
            raise TeardownIncomplete(
                "teardown left live resources: "
                + ", ".join(live)
                + (f"; {len(skip)} stack retained by design" if skip else "")
            )
        stale = len(inventory["stale"])
        print(
            "TEARDOWN COMPLETE: no resource of the destroyed stacks is live"
            + (f" ({stale} stale tag-index entries verified gone)" if stale else "")
        )

    def _empty_run_prefixes(self, bucket: str) -> int:
        """Retain the run's results locally, then delete exactly the run's two prefixes.
        Returns how many objects outside them the bucket still holds."""
        prefixes = (f"inputs/{self.run_id}/", f"results/{self.run_id}/")
        self.gate(
            {
                "token": "yes-list-objects",
                "action": "read-only listing of the bucket and retrieval of any result artifact "
                "not yet retained locally",
                "calls": [
                    f"s3.list_objects_v2 under {prefixes[0]}, {prefixes[1]} and the whole bucket "
                    "(paginated)",
                    f"s3.get_object for missing {', '.join(RESULT_ARTIFACTS)}",
                ],
                "creates": "nothing",
                "cost": "none",
                "data_sent": "none",
            }
        )
        owned = [item for prefix in prefixes for item in self._list(bucket, prefix)]
        if not self.dry_run:
            self._collect_results(bucket)
        everything = self._list(bucket, "")
        foreign = [i["Key"] for i in everything if not i["Key"].startswith(prefixes)]
        keys = [item["Key"] for item in owned]
        total = sum(int(item.get("Size", 0)) for item in owned)
        self.gate(
            {
                "token": "yes-delete-objects",
                "action": f"delete {len(keys) if not self.dry_run else '<count>'} objects "
                f"({total} bytes) under exactly {prefixes[0]} and {prefixes[1]}",
                "calls": [f"s3.delete_objects in batches of {DELETE_BATCH}", "s3.list_objects_v2"],
                "scope": list(prefixes),
                "keys_sample": keys[:20],
                "outside_scope": (
                    f"{len(foreign)} objects outside this run's prefixes stay untouched"
                    + ("; the bucket then cannot be destroyed" if foreign else "")
                ),
                "cost": "none",
                "data_sent": "the object keys",
                "rollback": "none; the objects are this run's staged inputs and results, "
                "retained locally",
            }
        )
        for start in range(0, len(keys), DELETE_BATCH):
            batch = keys[start : start + DELETE_BATCH]
            response = self.aws(
                "s3",
                "delete_objects",
                Bucket=bucket,
                Delete={"Objects": [{"Key": k} for k in batch], "Quiet": True},
            )
            if response.get("Errors"):
                raise Refused(f"delete_objects reported errors: {response['Errors'][:5]}")
        left = [item for prefix in prefixes for item in self._list(bucket, prefix)]
        if left:
            raise Refused(f"{len(left)} objects remain under the run's prefixes")
        self.journal.append("emptied", deleted=len(keys), foreign=len(foreign))
        return len(foreign)

    def _inventory(self, stacks: set[str]) -> dict[str, Any]:
        """Every resource still carrying the run tag that belonged to the destroyed stacks,
        each then verified against its own service: the tag index lags deletion, so a listed
        ARN is only counted live when the service says it exists and is not terminated or
        deleted. A type this runner cannot verify is counted live; absence is never assumed."""
        listed: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"TagFilters": [{"Key": RUN_TAG, "Values": [self.run_id]}]}
            if token:
                params["PaginationToken"] = token
            page = self.aws("resourcegroupstaggingapi", "get_resources", **params)
            for item in page.get("ResourceTagMappingList", []):
                tags = {t["Key"]: t["Value"] for t in item.get("Tags", [])}
                if tags.get(STACK_TAG, "") in stacks or STACK_TAG not in tags:
                    listed.append(item["ResourceARN"])
            token = page.get("PaginationToken") or None
            if not token:
                break
        live: list[str] = []
        stale: list[str] = []
        for arn in listed:
            state = self._native_state(arn)
            (stale if state in GONE_STATES else live).append(f"{arn} ({state})")
        inventory = {"listed": listed, "live": live, "stale": stale, "stacks": sorted(stacks)}
        self.journal.append("inventory", **inventory)
        return inventory

    def _native_state(self, arn: str) -> str:
        """What the owning service says about one tagged ARN: a state name, `not-found`, or
        `unverified` for a type this runner does not resolve."""
        if self.dry_run:
            return "not-found"
        tail = arn.rsplit(":", 1)[-1]
        kind, _, identifier = tail.partition("/")
        try:
            if kind == "instance":
                page = self.aws("ec2", "describe_instances", InstanceIds=[identifier])
                instances = [
                    i for r in page.get("Reservations", []) for i in r.get("Instances", [])
                ]
                return instances[0]["State"]["Name"] if instances else "not-found"
            if kind == "volume":
                page = self.aws("ec2", "describe_volumes", VolumeIds=[identifier])
                return page["Volumes"][0]["State"] if page.get("Volumes") else "not-found"
            if kind == "vpc-endpoint":
                page = self.aws("ec2", "describe_vpc_endpoints", VpcEndpointIds=[identifier])
                return page["VpcEndpoints"][0]["State"] if page.get("VpcEndpoints") else "not-found"
            if kind == "vpc":
                page = self.aws("ec2", "describe_vpcs", VpcIds=[identifier])
                return page["Vpcs"][0]["State"] if page.get("Vpcs") else "not-found"
            if kind == "subnet":
                page = self.aws("ec2", "describe_subnets", SubnetIds=[identifier])
                return page["Subnets"][0]["State"] if page.get("Subnets") else "not-found"
            if kind == "security-group":
                page = self.aws("ec2", "describe_security_groups", GroupIds=[identifier])
                return "available" if page.get("SecurityGroups") else "not-found"
        except Exception as error:  # noqa: BLE001 - NotFound is the expected answer here
            text = repr(error)
            return "not-found" if "NotFound" in text or "does not exist" in text else "unverified"
        return "unverified"


# ---- helpers --------------------------------------------------------------------------------


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _journal_params(params: dict[str, Any]) -> dict[str, Any]:
    """Request parameters as the journal keeps them: never a body, only its size."""
    safe: dict[str, Any] = {}
    for key, value in params.items():
        if key == "Body":
            safe[key] = {"bytes": len(value)} if isinstance(value, (bytes, bytearray)) else "stream"
        elif key == "Filename":
            path = Path(str(value))
            safe[key] = {
                "name": path.name,
                "bytes": path.stat().st_size if path.is_file() else None,
            }
        else:
            safe[key] = value
    return safe


def _identifiers(response: Any) -> Any:
    """The identifiers a response carried, for the journal and the denylist; no bodies."""
    if isinstance(response, dict):
        return {
            k: _identifiers(v) for k, v in response.items() if k not in {"Body", "ResponseMetadata"}
        }
    if isinstance(response, list):
        return [_identifiers(v) for v in response[:50]]
    if isinstance(response, (str, int, float, bool)) or response is None:
        return response
    return str(type(response).__name__)


def _creates(name: str, params: dict[str, str]) -> str:
    return {
        "biblios-qual-network": "one VPC, one private subnet, route table, two security groups, an "
        "S3 gateway endpoint and three interface endpoints (five with Bedrock); no internet or "
        "NAT gateway",
        "biblios-qual-artifacts": "one private TLS-only bucket (must be empty to destroy)",
        "biblios-qual-boundary": "one managed policy under /biblios/, the run-specific permissions "
        "boundary written from the bucket's real name and the run id (this run's inputs and "
        "results, Session Manager, the account reads, the one embedding model when enabled; "
        "streaming, KMS, IAM and role assumption denied)",
        "biblios-qual-custody": f"one IAM role under {params.get('RolePath', 'the role path')} "
        "under the run's permissions boundary (Session Manager core, the run's bucket "
        "prefixes, and the one embedding model when enabled) and its instance profile",
        "biblios-qual-instance": f"one {params.get('InstanceType', 'instance')} with IMDSv2, no "
        f"public address, root plus one {params.get('DataVolumeGiB', 'unrecorded-size')} GiB "
        "gp3 data volume at the account's default encryption; terminates on shutdown",
        "biblios-example-network": "one VPC with an internet gateway, one public subnet, a "
        "no-inbound security group",
        "biblios-example-instance": f"one {params.get('InstanceType', 'instance')} with a public "
        "address, IMDSv2, root plus one data volume, using the run's existing instance profile",
    }[name]


def cost_estimate() -> dict[str, Any]:
    """From the operator-maintained assumptions file; absent assumptions are said so."""
    path = REPOSITORY / "qualification" / "cost-assumptions.json"
    assumptions = json.loads(path.read_text()) if path.is_file() else {}
    hourly = assumptions.get("hourly_usd", {})

    def line(*keys: str) -> str:
        parts = []
        for key in keys:
            value = hourly.get(key)
            parts.append(
                f"{key} {value}/h" if isinstance(value, (int, float)) else f"{key} unrecorded"
            )
        return (
            "; ".join(parts) + f" (assumptions as of {assumptions.get('as_of', 'never recorded')})"
        )

    return {
        "as_of": assumptions.get("as_of"),
        "per_stack": {
            "biblios-qual-network": line("interface_endpoint") + "; three or five endpoints",
            "biblios-qual-artifacts": "storage and requests only",
            "biblios-qual-boundary": "none",
            "biblios-qual-custody": "none",
            "biblios-qual-instance": line("instance", "gp3_gib_month"),
            "biblios-example-network": "none while idle; data transfer out if any",
            "biblios-example-instance": line("instance", "gp3_gib_month"),
        },
        "storage_note": line("s3_gib_month"),
    }


def lane_commands(
    run_id: str,
    bucket: str,
    lanes: list[str],
    bedrock: dict[str, Any] | None,
    *,
    corpus: bool,
) -> list[str]:
    """The exact shell the instance runs; shown in the packet before it is sent.

    The archive is extracted with permissions and links preserved, the product and Vellric
    environments are installed from the staged cache with the same `uv sync` lines the
    offline proof ran (the development-only infra step is not part of the instance), the
    lanes run through the checkout's interpreter, every result artifact that exists is
    uploaded on exit whatever happened (a failure before the lanes still leaves install.log
    with the installer's output and the tool report), and the command exits with the lanes'
    own exit status so Session Manager reports a failure as one. Every tool comes from the
    bundle's own bin/; the instance's package manager is never asked for anything.
    """
    prefix = f"s3://{bucket}/inputs/{run_id}"
    results = f"s3://{bucket}/results/{run_id}"
    lane_args = f"--lanes {','.join(lanes)}" + (" --corpus ../corpus" if corpus else "")
    if bedrock:
        lane_args += (
            f" --bedrock-settings {bedrock['settings']} --bedrock-model {bedrock['model']}"
            f" --bedrock-region {bedrock['region']} --bedrock-texts {bedrock['texts']}"
        )
    upload = " ".join(
        f"[ -f {MOUNT}/{name} ] && aws s3 cp {MOUNT}/{name} {results}/{name} "
        "--only-show-errors || true;"
        for name in RESULT_ARTIFACTS
    )
    log = f"{MOUNT}/install.log"
    return [
        "set -euo pipefail",
        f"mkdir -p {MOUNT} && : >{log}",
        f"trap '{upload}' EXIT",
        f"rm -rf {MOUNT}/bundle && mkdir -p {MOUNT}/bundle && cd {MOUNT}/bundle",
        f"aws s3 cp {prefix}/bundle.tar bundle.tar --only-show-errors",
        f"aws s3 cp {prefix}/manifest.json manifest.json --only-show-errors",
        "tar --no-same-owner -xpf bundle.tar && rm bundle.tar",
        "export PATH=$PWD/bin:$PATH UV_OFFLINE=1 UV_CACHE_DIR=$PWD/cache "
        "UV_PYTHON_INSTALL_DIR=$PWD/python PALACE_MODELS_DIR=$PWD/models",
        "[ -x bin/vellric ] && export DRAWBRIDGE_VELLRIC=$PWD/bin/vellric || true",
        f'for tool in git file tesseract; do command -v "$tool" >>{log} 2>&1 '
        f'|| echo "$tool absent" >>{log}; done',
        f"cd checkout && uv sync --project project --locked --managed-python >>{log} 2>&1",
        f"uv sync --project qualification/vellric --locked --managed-python >>{log} 2>&1",
        "set +e",
        f"./bin/python {LANE_SCRIPT} --root {MOUNT} --checkout . {lane_args} "
        f"--output {MOUNT}/lanes.json >{MOUNT}/lanes.log 2>&1",
        f"echo $? >{MOUNT}/lanes.exit",
        "set -e",
        f'exit "$(cat {MOUNT}/lanes.exit)"',
    ]


def fixture_commands(run_id: str, bucket: str, archive: str, script: str) -> list[str]:
    """The exact shell the instance runs for a foreign fixture: fetch and extract the
    owner's archive under the data volume's fixture directory, run the owner's script with
    RESULTS set, upload everything the script left under the results directory whatever
    happened, and exit with the script's status."""
    key = f"s3://{bucket}/inputs/{run_id}/fixture/{archive}"
    results = f"s3://{bucket}/results/{run_id}/"
    marker = "BIBLIOS_FIXTURE_SCRIPT"
    return [
        "set -euo pipefail",
        f"mkdir -p {MOUNT}/results {MOUNT}/fixture-stage && cd {MOUNT}/fixture-stage",
        f"aws s3 cp {key} {archive} --only-show-errors",
        f"mkdir -p {MOUNT}/fixture && tar --no-same-owner -xaf {archive} -C {MOUNT}/fixture "
        f"&& rm {archive}",
        f"cat >{MOUNT}/fixture-run.sh <<'{marker}'",
        *script.rstrip("\n").split("\n"),
        marker,
        f"export RESULTS={MOUNT}/results",
        "set +e",
        f'bash {MOUNT}/fixture-run.sh "$RESULTS" >{MOUNT}/results/fixture.log 2>&1',
        f"echo $? >{MOUNT}/results/fixture.exit",
        "set -e",
        f"aws s3 cp --recursive {MOUNT}/results {results} --only-show-errors || true",
        f'exit "$(cat {MOUNT}/results/fixture.exit)"',
    ]


def synthesize_offline() -> Path:
    """In-process synthesis of every stack, exactly as the offline check does it."""
    try:
        code, out, log = infra_check.synthesize()
    except SystemExit as error:
        raise Refused(str(error)) from None
    if code != 0:
        raise Refused("offline synthesis failed:\n" + log[-2000:])
    return out


def resolve_cdk() -> tuple[Path, Path]:
    """The pinned Node and the repository-installed CDK CLI at the pinned version; refusal,
    never a fallback."""
    try:
        node_bin = infra_check.pinned_node_bin()
    except SystemExit as error:
        raise Refused(str(error)) from None
    cdk = REPOSITORY / ".kickoff" / "infra-tools" / "node_modules" / ".bin" / "cdk"
    if not cdk.is_file():
        raise Refused(
            "the pinned CDK CLI is not installed under .kickoff/infra-tools; run ./bin/setup"
        )
    pinned = json.loads((INFRA / "package.json").read_text())["dependencies"]["aws-cdk"]
    env = dict(os.environ, PATH=f"{node_bin}{os.pathsep}{os.environ.get('PATH', '')}")
    env["CDK_DISABLE_VERSION_CHECK"] = "1"
    found = subprocess.run(
        [str(cdk), "--version"], env=env, capture_output=True, text=True, check=False
    ).stdout.strip()
    if not found.startswith(pinned):
        raise Refused(f"the installed CDK CLI reports {found!r}, not the pinned {pinned}")
    return cdk, node_bin


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qualify-aws",
        description="Stand up, qualify and tear down Biblios's AWS qualification environment.",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        default=Path(
            os.environ.get("QUALIFICATION_HOME", str(Path.home() / ".biblios-qualification"))
        ),
    )
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--profile", default=None)
    parser.add_argument("--region", default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render every packet in order; perform no AWS, model or CDK call",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--lane", action="append", default=[], choices=ALL_LANES)
    plan.add_argument("--example", action="store_true")
    deploy = sub.add_parser("deploy")
    deploy.add_argument(
        "--boundary-arn",
        default=None,
        help="required when biblios-qual-custody is among the deployed stacks",
    )
    deploy.add_argument(
        "--stacks",
        default=None,
        help="comma-separated contiguous run of the private stacks in order; default all four; "
        "a later invocation continues from the recorded state",
    )
    deploy.add_argument("--role-path", default="/biblios/")
    deploy.add_argument("--instance-type", default="m7g.xlarge")
    deploy.add_argument("--data-gib", type=int, default=32)
    deploy.add_argument("--arch", choices=("arm64", "x86_64"), default="arm64")
    deploy.add_argument("--bedrock-model-arn", default=None)
    run = sub.add_parser("run")
    run.add_argument("--bundle", type=Path, default=None)
    run.add_argument("--fixture", type=Path, default=None, help="a foreign fixture archive")
    run.add_argument("--fixture-command", type=Path, default=None, help="its owner's script")
    run.add_argument(
        "--lane",
        action="append",
        default=[],
        choices=ALL_LANES,
        help=f"default: {', '.join(DEFAULT_LANES)}; bedrock only when named",
    )
    run.add_argument("--keep", action="store_true")
    run.add_argument("--bedrock-settings", default=None, help="path on the instance")
    run.add_argument("--bedrock-model", default=None)
    run.add_argument("--bedrock-region", default=None, help="default: --region")
    run.add_argument("--bedrock-texts", type=int, default=None)
    run.add_argument("--bedrock-cost-cap", default=None)
    sub.add_parser("results")
    sub.add_parser("teardown")
    qualify = sub.add_parser("qualify", help="deploy, run, collect and tear down in one invocation")
    for parent in (deploy, run):
        for action in parent._actions:  # noqa: SLF001 - mirror the two verbs' options
            if action.dest in {"help", "stacks"} or action.dest in {
                a.dest
                for a in qualify._actions  # noqa: SLF001
            }:
                continue
            qualify._add_action(action)  # noqa: SLF001
    example = sub.add_parser("example")
    example.add_argument("verb", choices=("deploy", "teardown"))
    example.add_argument("--instance-type", default="m7g.xlarge")
    example.add_argument("--data-gib", type=int, default=32)
    return parser


def make_runner(
    args: argparse.Namespace, *, transport: Transport | None = None, confirm: Confirm | None = None
) -> Runner:
    if args.command != "plan" and (
        args.profile is None or args.region is None or args.run_id is None
    ):
        raise Refused("--profile, --region and --run-id are required for every command but plan")
    run_id = args.run_id or "plan"
    evidence = args.evidence / run_id
    if transport is None:
        if args.dry_run or args.command in {"plan", "results"}:
            transport = RefusingTransport()
        else:
            cdk, node_bin = resolve_cdk()
            transport = RealTransport(
                profile=args.profile, region=args.region, cdk_executable=cdk, node_bin=node_bin
            )
    return Runner(
        evidence=evidence,
        transport=transport,
        confirm=confirm or terminal_confirm,
        dry_run=args.dry_run,
        profile=args.profile or "none",
        region=args.region or "none",
        run_id=run_id,
    )


def bedrock_options(args: argparse.Namespace) -> dict[str, Any] | None:
    given = {
        "settings": args.bedrock_settings,
        "model": args.bedrock_model,
        "texts": args.bedrock_texts,
        "cost_cap": args.bedrock_cost_cap,
    }
    if not any(v is not None for v in given.values()):
        return None
    missing = [k for k, v in given.items() if v is None]
    if missing:
        raise Refused(
            "the Bedrock lane needs --bedrock-settings, --bedrock-model, --bedrock-texts and "
            f"--bedrock-cost-cap together; missing {', '.join(missing)}"
        )
    return given | {"region": args.bedrock_region or args.region}


def main(
    argv: list[str] | None = None,
    *,
    transport: Transport | None = None,
    confirm: Confirm | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    try:
        runner = make_runner(args, transport=transport, confirm=confirm)
        lanes = list(args.lane) if getattr(args, "lane", None) else list(DEFAULT_LANES)
        if args.command == "plan":
            print(json.dumps(runner.plan(lanes=lanes, example=args.example), indent=1))
        elif args.command == "deploy":
            runner.deploy(
                boundary_arn=args.boundary_arn,
                role_path=args.role_path,
                instance_type=args.instance_type,
                data_gib=args.data_gib,
                arch=args.arch,
                bedrock_model_arn=args.bedrock_model_arn,
                stacks=tuple(s.strip() for s in args.stacks.split(",") if s.strip())
                if args.stacks
                else None,
            )
        elif args.command == "run":
            runner.run(
                bundle=args.bundle,
                lanes=lanes,
                keep=args.keep,
                bedrock=bedrock_options(args),
                fixture=args.fixture,
                fixture_command=args.fixture_command,
            )
        elif args.command == "qualify":
            runner.qualify(
                deploy_options={
                    "boundary_arn": args.boundary_arn,
                    "role_path": args.role_path,
                    "instance_type": args.instance_type,
                    "data_gib": args.data_gib,
                    "arch": args.arch,
                    "bedrock_model_arn": args.bedrock_model_arn,
                },
                run_options={
                    "bundle": args.bundle,
                    "lanes": lanes,
                    "bedrock": bedrock_options(args),
                    "fixture": args.fixture,
                    "fixture_command": args.fixture_command,
                },
                keep=args.keep,
            )
        elif args.command == "results":
            from results import render  # type: ignore[import-not-found]

            target = REPOSITORY / "briefs" / "aws-qualification-results.md"
            target.write_text(render(runner.evidence))
            print(f"rendered {target}")
        elif args.command == "teardown":
            runner.teardown()
        elif args.command == "example":
            if args.verb == "deploy":
                runner.deploy(
                    boundary_arn="(the run's existing role)",
                    role_path="/biblios/",
                    instance_type=args.instance_type,
                    data_gib=args.data_gib,
                    arch="arm64",
                    bedrock_model_arn=None,
                    example=True,
                )
            else:
                runner.teardown(example=True)
    except QualificationFailed as error:
        print(f"qualify-aws: {error}", file=sys.stderr)
        return 3
    except Refused as error:
        print(f"qualify-aws: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.exit(main())
