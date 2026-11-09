"""Offline proof of the stack sets' safety properties, with AWS genuinely out of reach.

Run from this directory on the pinned Node with the locked environment. The check first
removes every AWS credential input from its own environment (variables, config files,
metadata, container routes) and loads a socket guard into the synthesizing Python process.
The sanitized environment is what keeps the SDK and the CDK CLI from reaching the account;
the socket guard binds the Python process only, not uv or the jsii Node child. It then
synthesizes every stack in-process, asserts each property on the actual templates, plants
each mutant in a deep copy and requires the assertion that names it to fail, synthesizes
once more through the pinned CDK CLI with an external `--output` (the route `deploy` takes)
and requires the templates to land there and nowhere in the repository, and finally plants
a context lookup and requires two specific refusals: in-process, the app declines a lookup
for a stack with no account; through the CLI with a fictitious account, the CLI reports
that it needs AWS calls but has no credentials. Exit 0 with `QUALIFICATION CHECK PASSED`
is the only pass.

It does not establish live enforcement, endpoint availability, image contents, the
account's boundary or any cost; every deploy is its own approved action.
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPOSITORY = HERE.parent
INFRA = HERE
STACKS = (
    "biblios-qual-network",
    "biblios-qual-artifacts",
    "biblios-qual-boundary",
    "biblios-qual-custody",
    "biblios-qual-instance",
    "biblios-example-network",
    "biblios-example-instance",
)
CREDENTIAL_PREFIXES = ("AWS_",)
CREDENTIAL_NAMES = (
    "AWS_PROFILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
)
SOCKET_GUARD = """
import socket
_connect = socket.socket.connect
def _refuse(self, address):
    host = address[0] if isinstance(address, tuple) else address
    if isinstance(host, str) and host in ("127.0.0.1", "::1", "localhost"):
        return _connect(self, address)
    raise OSError(f"network refused by the qualification check: {address!r}")
socket.socket.connect = _refuse
socket.socket.connect_ex = lambda self, address: 111
"""


class CheckFailure(Exception):
    pass


def pinned_node_bin() -> Path:
    """The pinned Node's bin directory: `BIBLIOS_NODE_BIN` (absolute, containing node) or the
    nvm layout for the version in infra/.node-version; a refusal otherwise, never PATH."""
    override = os.environ.get("BIBLIOS_NODE_BIN")
    if override:
        candidate = Path(override)
        if not candidate.is_absolute() or not (candidate / "node").is_file():
            raise SystemExit("REFUSED: BIBLIOS_NODE_BIN must be an absolute directory holding node")
    version = (INFRA / ".node-version").read_text().strip()
    if override:
        candidate = Path(override)
    else:
        candidate = Path.home() / ".nvm" / "versions" / "node" / version / "bin"
        if not (candidate / "node").is_file():
            raise SystemExit(
                f"REFUSED: pinned Node {version} is not installed at {candidate}; "
                "see infra/README.md"
            )
    reported = subprocess.run(
        [str(candidate / "node"), "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    if reported != version:
        raise SystemExit(
            f"REFUSED: node at {candidate} reports {reported or 'nothing'}, "
            f"not the pinned {version}"
        )
    return candidate


def sanitized_environment() -> dict[str, str]:
    """No credential input survives: variables, config files, metadata, container routes."""
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(CREDENTIAL_PREFIXES) and key not in CREDENTIAL_NAMES
    }
    env["AWS_CONFIG_FILE"] = "/dev/null"
    env["AWS_SHARED_CREDENTIALS_FILE"] = "/dev/null"
    env["AWS_EC2_METADATA_DISABLED"] = "true"
    env["UV_OFFLINE"] = "1"
    return env


FICTITIOUS_ACCOUNT = "111111111111"
FICTITIOUS_REGION = "xx-fictitious-1"
PINNED_CDK = REPOSITORY / ".kickoff" / "infra-tools" / "node_modules" / ".bin" / "cdk"
# In-process, a lookup for a stack with no account is declined before any provider runs.
IN_PROCESS_REFUSAL = "StackAccountRegionNotSpecified"
# Through the CLI with a fictitious account, the lookup is attempted and the sanitized
# environment has no credentials to attempt it with: the boundary that matters for deploy.
CLI_REFUSAL = f"Need to perform AWS calls for account {FICTITIOUS_ACCOUNT}, but no credentials"


def _environment(out: Path, *, plant: str | None, fictitious: bool) -> dict[str, str]:
    guard_dir = out / "guard"
    guard_dir.mkdir(exist_ok=True)
    (guard_dir / "sitecustomize.py").write_text(SOCKET_GUARD)
    env = sanitized_environment()
    env["CDK_OUTDIR"] = str(out / "cdk.out")
    env["CDK_DISABLE_VERSION_CHECK"] = "1"
    env["PYTHONPATH"] = str(guard_dir) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else ""
    )
    env["PATH"] = f"{pinned_node_bin()}{os.pathsep}{env.get('PATH', '')}"
    if plant:
        env["BIBLIOS_INFRA_PLANT"] = plant
    else:
        env.pop("BIBLIOS_INFRA_PLANT", None)
    if fictitious:
        env["CDK_DEFAULT_ACCOUNT"] = FICTITIOUS_ACCOUNT
        env["CDK_DEFAULT_REGION"] = FICTITIOUS_REGION
    return env


def synthesize(plant: str | None = None, *, fictitious: bool = False) -> tuple[int, Path, str]:
    """Synthesize in-process into a fresh directory outside the repository, under the socket
    guard. `fictitious` supplies a made-up account and region, which the app passes to no
    stack of its own; it exists for the planted lookup."""
    out = Path(tempfile.mkdtemp(prefix="biblios-infra-check-"))
    env = _environment(out, plant=plant, fictitious=fictitious)
    completed = subprocess.run(
        ["uv", "run", "--locked", "--no-sync", "--managed-python", "python", "app.py"],
        cwd=HERE,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.returncode, out / "cdk.out", completed.stdout + completed.stderr


def synthesize_through_cli(
    plant: str | None = None, *, fictitious: bool = False, stack: str | None = None
) -> tuple[int, Path, str]:
    """Synthesize through the pinned CDK CLI with an external `--output`, the route a deploy
    takes; the CLI, not the app, is what performs context lookups."""
    if not PINNED_CDK.is_file():
        raise SystemExit(
            "REFUSED: the pinned CDK CLI is not installed under .kickoff/infra-tools; "
            "run ./bin/setup"
        )
    out = Path(tempfile.mkdtemp(prefix="biblios-infra-cli-"))
    env = _environment(out, plant=plant, fictitious=fictitious)
    argv = [str(PINNED_CDK), "synth", "--no-notices", "--quiet", "--output", str(out / "cli.out")]
    if stack:
        argv.append(stack)
    completed = subprocess.run(argv, cwd=HERE, env=env, capture_output=True, text=True, check=False)
    return completed.returncode, out / "cli.out", completed.stdout + completed.stderr


def template(out: Path, name: str) -> dict:
    return json.loads((out / f"{name}.template.json").read_text())


def resources(doc: dict, kind: str) -> list[dict]:
    return [r for r in doc["Resources"].values() if r["Type"] == kind]


def one(doc: dict, kind: str) -> dict:
    found = resources(doc, kind)
    if len(found) != 1:
        raise CheckFailure(f"expected one {kind}, found {len(found)}")
    return found[0]


# ---- assertions: each named, each with a mutant that must make it fail ------------------

Assertion = Callable[[dict[str, dict]], None]
Mutant = Callable[[dict[str, dict]], None]


def private_network_has_no_internet(t: dict[str, dict]) -> None:
    doc = t["biblios-qual-network"]
    if resources(doc, "AWS::EC2::InternetGateway") or resources(doc, "AWS::EC2::NatGateway"):
        raise CheckFailure("private network has an internet or NAT gateway")
    if resources(doc, "AWS::EC2::Route"):
        raise CheckFailure("private network has a route beyond the local one")
    if one(doc, "AWS::EC2::Subnet")["Properties"].get("MapPublicIpOnLaunch") is not False:
        raise CheckFailure("private subnet maps public addresses")


def mutate_add_gateway(t: dict[str, dict]) -> None:
    t["biblios-qual-network"]["Resources"]["Planted"] = {
        "Type": "AWS::EC2::InternetGateway",
        "Properties": {},
    }


def private_network_endpoints(t: dict[str, dict]) -> None:
    doc = t["biblios-qual-network"]
    endpoints = resources(doc, "AWS::EC2::VPCEndpoint")
    kinds = {e["Properties"].get("VpcEndpointType") for e in endpoints}
    if "Gateway" not in kinds:
        raise CheckFailure("no S3 gateway endpoint")
    suffixes = [_service_suffix(e) for e in endpoints]
    for service in ("ssm", "ssmmessages", "ec2messages"):
        if f".{service}" not in suffixes:
            raise CheckFailure(f"missing interface endpoint for {service}")
    for e in endpoints:
        if e["Properties"].get("VpcEndpointType") == "Interface" and not e["Properties"].get(
            "PrivateDnsEnabled"
        ):
            raise CheckFailure("interface endpoint without private DNS")


def _service_suffix(endpoint: dict) -> str:
    """The literal tail of a service name built by Fn::Join, such as ".ssm"."""
    name = endpoint["Properties"]["ServiceName"]
    if isinstance(name, dict) and "Fn::Join" in name:
        last = name["Fn::Join"][1][-1]
        return last if isinstance(last, str) else ""
    return str(name)


def mutate_drop_ssm_endpoint(t: dict[str, dict]) -> None:
    doc = t["biblios-qual-network"]
    for key, r in list(doc["Resources"].items()):
        if r["Type"] == "AWS::EC2::VPCEndpoint" and _service_suffix(r) == ".ssm":
            del doc["Resources"][key]
            return
    raise CheckFailure("mutant could not find the ssm endpoint")


def _is_instance_group(props: dict) -> bool:
    return (
        props["GroupDescription"].lower().startswith(("qualification instance", "example instance"))
    )


def _instance_group_ids(doc: dict) -> set[str]:
    return {
        logical
        for logical, r in doc["Resources"].items()
        if r["Type"] == "AWS::EC2::SecurityGroup" and _is_instance_group(r["Properties"])
    }


def _targets_group(group_ref: object, logical_ids: set[str]) -> bool:
    """A GroupId given as {"Ref": id}, {"Fn::GetAtt": [id, "GroupId"]} or a bare string."""
    if isinstance(group_ref, dict):
        if "Ref" in group_ref:
            return group_ref["Ref"] in logical_ids
        if "Fn::GetAtt" in group_ref:
            return group_ref["Fn::GetAtt"][0] in logical_ids
        return False
    return str(group_ref) in logical_ids


def instance_groups_admit_nothing(t: dict[str, dict]) -> None:
    for name in ("biblios-qual-network", "biblios-example-network"):
        doc = t[name]
        ids = _instance_group_ids(doc)
        for group in resources(doc, "AWS::EC2::SecurityGroup"):
            props = group["Properties"]
            if _is_instance_group(props) and props.get("SecurityGroupIngress"):
                raise CheckFailure(f"{name}: instance group admits inbound traffic")
        for rule in resources(doc, "AWS::EC2::SecurityGroupIngress"):
            if _targets_group(rule["Properties"].get("GroupId"), ids):
                raise CheckFailure(f"{name}: a standalone ingress rule targets the instance group")
    private = t["biblios-qual-network"]
    for group in resources(private, "AWS::EC2::SecurityGroup"):
        for rule in group["Properties"].get("SecurityGroupEgress", []):
            if rule.get("CidrIp") == "0.0.0.0/0":
                raise CheckFailure("private shape group egresses to the internet")


def mutate_open_ingress(t: dict[str, dict]) -> None:
    for group in resources(t["biblios-qual-network"], "AWS::EC2::SecurityGroup"):
        if _is_instance_group(group["Properties"]):
            group["Properties"]["SecurityGroupIngress"] = [
                {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22, "CidrIp": "0.0.0.0/0"}
            ]


def mutate_standalone_ingress(t: dict[str, dict]) -> None:
    doc = t["biblios-qual-network"]
    (logical,) = _instance_group_ids(doc)
    doc["Resources"]["PlantedIngress"] = {
        "Type": "AWS::EC2::SecurityGroupIngress",
        "Properties": {
            "GroupId": {"Fn::GetAtt": [logical, "GroupId"]},
            "IpProtocol": "tcp",
            "FromPort": 22,
            "ToPort": 22,
            "CidrIp": "0.0.0.0/0",
        },
    }


def bucket_is_private_and_tls_only(t: dict[str, dict]) -> None:
    doc = t["biblios-qual-artifacts"]
    bucket = one(doc, "AWS::S3::Bucket")["Properties"]
    block = bucket.get("PublicAccessBlockConfiguration", {})
    if not all(
        block.get(k) is True
        for k in (
            "BlockPublicAcls",
            "BlockPublicPolicy",
            "IgnorePublicAcls",
            "RestrictPublicBuckets",
        )
    ):
        raise CheckFailure("bucket does not block all public access")
    policy = one(doc, "AWS::S3::BucketPolicy")["Properties"]["PolicyDocument"]
    denies = [
        s
        for s in policy["Statement"]
        if s["Effect"] == "Deny"
        and s.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") == "false"
    ]
    if not denies:
        raise CheckFailure("bucket policy does not deny insecure transport")
    if resources(doc, "Custom::S3AutoDeleteObjects") or resources(doc, "AWS::Lambda::Function"):
        raise CheckFailure("bucket would delete objects automatically")


def mutate_allow_public(t: dict[str, dict]) -> None:
    one(t["biblios-qual-artifacts"], "AWS::S3::Bucket")["Properties"][
        "PublicAccessBlockConfiguration"
    ]["BlockPublicPolicy"] = False


def role_is_bounded_and_scoped(t: dict[str, dict]) -> None:
    doc = t["biblios-qual-custody"]
    role = one(doc, "AWS::IAM::Role")["Properties"]
    if "PermissionsBoundary" not in role or "Path" not in role:
        raise CheckFailure("role lacks a permissions boundary or a path")
    for policy in role.get("Policies", []):
        for statement in policy["PolicyDocument"]["Statement"]:
            if statement["Resource"] == "*" and statement["Action"] != "s3:ListBucket":
                raise CheckFailure("inline policy grants a wildcard resource")
            if any(
                a == "*" or a.endswith(":*")
                for a in (
                    [statement["Action"]]
                    if isinstance(statement["Action"], str)
                    else statement["Action"]
                )
            ):
                raise CheckFailure("inline policy grants a wildcard action")
    bedrock = one(doc, "AWS::IAM::Policy")
    if "Condition" not in bedrock:
        raise CheckFailure("Bedrock lane policy is unconditional")
    actions: set[str] = set()
    for s in bedrock["Properties"]["PolicyDocument"]["Statement"]:
        actions |= {s["Action"]} if isinstance(s["Action"], str) else set(s["Action"])
    if actions != {
        "bedrock:InvokeModel",
        "bedrock:GetAccountDataRetention",
        "bedrock:GetFoundationModelAvailability",
    }:
        raise CheckFailure(f"Bedrock lane policy grants {sorted(actions)}")


def _joined(value: object) -> str:
    """The literal parts of a Fn::Join, for matching a prefix or suffix."""
    if isinstance(value, dict) and "Fn::Join" in value:
        return "".join(_joined(part) for part in value["Fn::Join"][1])
    return value if isinstance(value, str) else ""


def boundary_is_run_scoped(t: dict[str, dict]) -> None:
    doc = t["biblios-qual-boundary"]
    policy = one(doc, "AWS::IAM::ManagedPolicy")["Properties"]
    if policy.get("Path") != "/biblios/":
        raise CheckFailure("boundary is not under /biblios/")
    statements = policy["PolicyDocument"]["Statement"]
    allows = [s for s in statements if isinstance(s, dict) and s.get("Effect") == "Allow"]
    denies = [s for s in statements if isinstance(s, dict) and s.get("Effect") == "Deny"]
    for s in allows:
        actions = [s["Action"]] if isinstance(s["Action"], str) else s["Action"]
        resources = s["Resource"] if isinstance(s["Resource"], list) else [s["Resource"]]
        if any(a == "*" or a.endswith(":*") for a in actions):
            raise CheckFailure("boundary allows a wildcard action")
        if any(a.startswith("s3:") for a in actions):
            for r in resources:
                text = _joined(r)
                if r == "*" or "arn:" not in text:
                    raise CheckFailure("boundary allows S3 outside the run's bucket")
                if "s3:ListBucket" not in actions and (
                    "/inputs/" not in text and "/results/" not in text
                ):
                    raise CheckFailure("boundary allows S3 objects outside the run's prefixes")
            if "s3:ListBucket" in actions:
                prefixes = s.get("Condition", {}).get("StringLike", {}).get("s3:prefix")
                if not prefixes or not all(
                    _joined(p).startswith(("inputs/", "results/")) for p in prefixes
                ):
                    raise CheckFailure("boundary lists the bucket without the prefix condition")
    denied: set[str] = set()
    for s in denies:
        denied |= {s["Action"]} if isinstance(s["Action"], str) else set(s["Action"])
    if not {"bedrock:InvokeModelWithResponseStream", "kms:*", "iam:*", "sts:AssumeRole"} <= denied:
        raise CheckFailure("boundary lacks the streaming, KMS, IAM and AssumeRole denies")


def mutate_widen_boundary(t: dict[str, dict]) -> None:
    policy = one(t["biblios-qual-boundary"], "AWS::IAM::ManagedPolicy")["Properties"]
    for s in policy["PolicyDocument"]["Statement"]:
        if isinstance(s, dict) and s.get("Sid") == "RunInputs":
            s["Resource"] = "*"


def mutate_unbounded_role(t: dict[str, dict]) -> None:
    del one(t["biblios-qual-custody"], "AWS::IAM::Role")["Properties"]["PermissionsBoundary"]


def instance_is_hardened(t: dict[str, dict]) -> None:
    for name, public in (("biblios-qual-instance", False), ("biblios-example-instance", True)):
        props = one(t[name], "AWS::EC2::Instance")["Properties"]
        meta = props.get("MetadataOptions", {})
        if meta.get("HttpTokens") != "required" or meta.get("HttpPutResponseHopLimit") != 1:
            raise CheckFailure(f"{name}: IMDSv2 not required")
        (nic,) = props["NetworkInterfaces"]
        if nic.get("AssociatePublicIpAddress") is not public:
            raise CheckFailure(
                f"{name}: public address setting is {nic.get('AssociatePublicIpAddress')}"
            )
        if props.get("InstanceInitiatedShutdownBehavior") != "terminate":
            raise CheckFailure(f"{name}: shutdown does not terminate")
        (data,) = props["BlockDeviceMappings"]
        ebs = data["Ebs"]
        if "Encrypted" in ebs:
            raise CheckFailure(
                f"{name}: the data volume sets Encrypted; the account default decides"
            )
        if ebs.get("DeleteOnTermination") is not True or ebs.get("VolumeType") != "gp3":
            raise CheckFailure(f"{name}: data volume is not a disposable gp3 volume")
        if props.get("PropagateTagsToVolumeOnCreation") is not True:
            raise CheckFailure(f"{name}: volume would not carry the run tag")


def mutate_imds_v1(t: dict[str, dict]) -> None:
    one(t["biblios-qual-instance"], "AWS::EC2::Instance")["Properties"]["MetadataOptions"][
        "HttpTokens"
    ] = "optional"


def mutate_set_encrypted(t: dict[str, dict]) -> None:
    one(t["biblios-qual-instance"], "AWS::EC2::Instance")["Properties"]["BlockDeviceMappings"][0][
        "Ebs"
    ]["Encrypted"] = True


def every_resource_is_tagged(t: dict[str, dict]) -> None:
    taggable = (
        "AWS::EC2::Instance",
        "AWS::EC2::VPC",
        "AWS::EC2::Subnet",
        "AWS::EC2::SecurityGroup",
        "AWS::S3::Bucket",
        "AWS::IAM::Role",
    )  # AWS::IAM::ManagedPolicy takes no tags in CloudFormation; its stack carries them
    for name, doc in t.items():
        for r in doc["Resources"].values():
            if r["Type"] in taggable:
                keys = {tag["Key"] for tag in r["Properties"].get("Tags", [])}
                if not {"Project", "biblios:run"} <= keys:
                    raise CheckFailure(f"{name}: {r['Type']} lacks the ownership tags")


def mutate_untag_instance(t: dict[str, dict]) -> None:
    one(t["biblios-qual-instance"], "AWS::EC2::Instance")["Properties"]["Tags"] = []


def nothing_is_committed(t: dict[str, dict]) -> None:
    """No account id, region literal or resource id in any template."""
    text = json.dumps(t)
    for marker in (
        "arn:aws:iam::0",
        "arn:aws:iam::1",
        "arn:aws:iam::2",
        "arn:aws:iam::3",
        "arn:aws:iam::4",
        "arn:aws:iam::5",
        "arn:aws:iam::6",
        "arn:aws:iam::7",
        "arn:aws:iam::8",
        "arn:aws:iam::9",
        "us-east-",
        "us-west-",
        "eu-west-",
        "ami-0",
        "subnet-0",
        "sg-0",
        "vpc-0",
    ):
        if marker in text:
            raise CheckFailure(f"a template carries a concrete identifier: {marker}")


def mutate_commit_region(t: dict[str, dict]) -> None:
    t["biblios-qual-network"]["Resources"]["Planted"] = {
        "Type": "AWS::EC2::VPC",
        "Properties": {
            "CidrBlock": "10.0.0.0/16",
            "Tags": [{"Key": "Region", "Value": "us-east-1"}],
        },
    }


CHECKS: tuple[tuple[str, Assertion, tuple[Mutant, ...]], ...] = (
    ("private-network-has-no-internet", private_network_has_no_internet, (mutate_add_gateway,)),
    ("private-network-endpoints", private_network_endpoints, (mutate_drop_ssm_endpoint,)),
    (
        "instance-groups-admit-nothing",
        instance_groups_admit_nothing,
        (mutate_open_ingress, mutate_standalone_ingress),
    ),
    ("bucket-is-private-and-tls-only", bucket_is_private_and_tls_only, (mutate_allow_public,)),
    ("role-is-bounded-and-scoped", role_is_bounded_and_scoped, (mutate_unbounded_role,)),
    ("boundary-is-run-scoped", boundary_is_run_scoped, (mutate_widen_boundary,)),
    ("instance-is-hardened", instance_is_hardened, (mutate_imds_v1, mutate_set_encrypted)),
    ("every-resource-is-tagged", every_resource_is_tagged, (mutate_untag_instance,)),
    ("nothing-is-committed", nothing_is_committed, (mutate_commit_region,)),
)


def main() -> int:
    if any(k.startswith("AWS_ACCESS") or k == "AWS_PROFILE" for k in sanitized_environment()):
        print(
            "QUALIFICATION CHECK FAILED: credential variables survived sanitization",
            file=sys.stderr,
        )
        return 1
    code, out, log = synthesize()
    if code != 0:
        print(log, file=sys.stderr)
        print("QUALIFICATION CHECK FAILED: synthesis failed", file=sys.stderr)
        return 1
    if out.resolve().is_relative_to(REPOSITORY):
        print("QUALIFICATION CHECK FAILED: synthesis wrote inside the repository", file=sys.stderr)
        return 1
    templates = {name: template(out, name) for name in STACKS}
    failures: list[str] = []
    for name, assertion, mutants in CHECKS:
        try:
            assertion(templates)
            print(f"ok      {name}")
        except CheckFailure as error:
            failures.append(f"{name}: {error}")
            print(f"FAILED  {name}: {error}")
            continue
        for index, mutant in enumerate(mutants, 1):
            mutated = copy.deepcopy(templates)
            mutant(mutated)
            try:
                assertion(mutated)
            except CheckFailure:
                print(f"ok      {name} mutant {index} refused")
            else:
                failures.append(f"{name}: mutant {index} passed the check")
                print(f"FAILED  {name}: mutant {index} passed the check")
    local_cdk_out = HERE / "cdk.out"
    had_local = local_cdk_out.exists()
    cli_code, cli_out, cli_log = synthesize_through_cli()
    cli_templates = all((cli_out / f"{name}.template.json").is_file() for name in STACKS)
    if cli_code != 0 or not cli_templates:
        failures.append("pinned CLI synthesis with --output failed")
        print("FAILED  cli-synthesis\n" + cli_log[-1500:])
    elif local_cdk_out.exists() and not had_local:
        failures.append("pinned CLI synthesis wrote infra/cdk.out despite --output")
        print("FAILED  cli-synthesis: wrote inside the repository")
    else:
        print("ok      cli-synthesis (external --output, templates present)")
    plant_code, _, plant_log = synthesize(plant="lookup")
    if plant_code == 0 or IN_PROCESS_REFUSAL not in plant_log:
        failures.append(
            "planted lookup in-process: expected the no-account refusal "
            f"({IN_PROCESS_REFUSAL}), got exit {plant_code}"
        )
        print("FAILED  planted-lookup in-process\n" + plant_log[-800:])
    else:
        print(f"ok      planted-lookup in-process refused ({IN_PROCESS_REFUSAL})")
    cli_plant_code, _, cli_plant_log = synthesize_through_cli(
        plant="lookup", fictitious=True, stack="biblios-planted-lookup"
    )
    if cli_plant_code == 0 or CLI_REFUSAL not in cli_plant_log:
        failures.append(
            "planted lookup through the CLI: expected the no-credentials refusal, "
            f"got exit {cli_plant_code}"
        )
        print("FAILED  planted-lookup cli\n" + cli_plant_log[-800:])
    else:
        print("ok      planted-lookup cli refused (no credentials for the fictitious account)")
    if failures:
        print("QUALIFICATION CHECK FAILED: " + "; ".join(failures), file=sys.stderr)
        return 1
    print("QUALIFICATION CHECK PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
