"""The private qualification environment: network, artifacts, boundary, custody, instance.

Five stacks, deployed in this order and destroyed in reverse, each its own action-time
confirmation. The boundary stack creates the run-specific permissions boundary from the
artifacts stack's real bucket name, so CDK owns the whole lifecycle and no IAM step is done
by hand. Nothing has a public address, there is no internet or NAT gateway, and the
only way in is Session Manager over private interface endpoints. Account, region, image,
boundary and run identifiers arrive as parameters at deploy time and are never committed.
Synthesis creates nothing. The data volume that holds the shelves is left at the account's
default encryption setting: the property is neither set nor cleared here.
"""

from __future__ import annotations

from aws_cdk import (
    Aws,
    BootstraplessSynthesizer,
    CfnCondition,
    CfnOutput,
    CfnParameter,
    Fn,
    Stack,
    Tags,
)
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3 as s3
from constructs import Construct

PROJECT_TAG = "biblios"
RUN_TAG = "biblios:run"
CIDR = "10.44.0.0/24"
SUBNET_CIDR = "10.44.0.0/26"
DATA_DEVICE = "/dev/sdf"
MOUNT_POINT = "/mnt/biblios"
SESSION_ENDPOINTS = ("ssm", "ssmmessages", "ec2messages")
BEDROCK_ENDPOINTS = ("bedrock-runtime", "bedrock")
# The account's allow-list shape: current-generation general-purpose types, CPU only.
INSTANCE_TYPES = (
    "m7g.large",
    "m7g.xlarge",
    "m7g.2xlarge",
    "m7i.large",
    "m7i.xlarge",
    "m7i.2xlarge",
)
BOOTSTRAP = f"""#!/bin/bash
set -euo pipefail
device=""
for candidate in {DATA_DEVICE} /dev/xvdf /dev/nvme1n1; do
  if [ -b "$candidate" ]; then device="$candidate"; break; fi
done
if [ -z "$device" ]; then
  for _ in $(seq 1 30); do
    sleep 2
    for candidate in {DATA_DEVICE} /dev/xvdf /dev/nvme1n1; do
      if [ -b "$candidate" ]; then device="$candidate"; break 2; fi
    done
  done
fi
[ -n "$device" ] || {{ echo "biblios: data volume not attached" >&2; exit 1; }}
if ! blkid "$device" >/dev/null 2>&1; then mkfs.ext4 -L biblios "$device"; fi
mkdir -p {MOUNT_POINT}
mount "$device" {MOUNT_POINT}
printf '%s\\n' "$device" > {MOUNT_POINT}/.biblios-data-volume
"""


def _stack(stack: Stack) -> CfnParameter:
    """Every resource carries the run's ownership tag; teardown is proved by that tag."""
    run = CfnParameter(stack, "RunId", type="String", allowed_pattern=r"^[a-z0-9-]{1,64}$")
    Tags.of(stack).add("Project", PROJECT_TAG)
    Tags.of(stack).add(RUN_TAG, run.value_as_string, exclude_resource_types=["aws:cdk:stack"])
    return run


def _true(stack: Stack, name: str, parameter: CfnParameter) -> CfnCondition:
    return CfnCondition(
        stack, name, expression=Fn.condition_equals(parameter.value_as_string, "true")
    )


class NetworkStack(Stack):
    """One private subnet with no internet route; Session Manager and S3 on private routes."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, synthesizer=BootstraplessSynthesizer(), **kwargs)
        _stack(self)
        prefix_list = CfnParameter(
            self, "S3PrefixList", type="String", description="The region's S3 managed prefix list"
        )
        bedrock = CfnParameter(
            self,
            "BedrockEndpoints",
            type="String",
            default="false",
            allowed_values=["true", "false"],
            description="Add the Bedrock interface endpoints for the Bedrock lane",
        )
        with_bedrock = _true(self, "WithBedrock", bedrock)
        vpc = ec2.CfnVPC(
            self, "Vpc", cidr_block=CIDR, enable_dns_hostnames=True, enable_dns_support=True
        )
        subnet = ec2.CfnSubnet(
            self,
            "PrivateSubnet",
            vpc_id=vpc.ref,
            cidr_block=SUBNET_CIDR,
            availability_zone=Fn.select(0, Fn.get_azs()),
            map_public_ip_on_launch=False,
        )
        table = ec2.CfnRouteTable(self, "PrivateRoutes", vpc_id=vpc.ref)
        ec2.CfnSubnetRouteTableAssociation(
            self, "PrivateAssociation", subnet_id=subnet.ref, route_table_id=table.ref
        )
        instance_group = ec2.CfnSecurityGroup(
            self,
            "InstanceGroup",
            group_description="Qualification instance: no inbound, HTTPS out to endpoints only",
            vpc_id=vpc.ref,
            security_group_ingress=[],
            security_group_egress=[
                ec2.CfnSecurityGroup.EgressProperty(
                    ip_protocol="tcp",
                    from_port=443,
                    to_port=443,
                    destination_prefix_list_id=prefix_list.value_as_string,
                    description="S3 gateway endpoint",
                )
            ],
        )
        endpoint_group = ec2.CfnSecurityGroup(
            self,
            "EndpointGroup",
            group_description="Interface endpoints: HTTPS from the instance group only",
            vpc_id=vpc.ref,
            security_group_ingress=[
                ec2.CfnSecurityGroup.IngressProperty(
                    ip_protocol="tcp",
                    from_port=443,
                    to_port=443,
                    source_security_group_id=instance_group.ref,
                )
            ],
            security_group_egress=[],
        )
        ec2.CfnSecurityGroupEgress(
            self,
            "InstanceToEndpoints",
            group_id=instance_group.ref,
            ip_protocol="tcp",
            from_port=443,
            to_port=443,
            destination_security_group_id=endpoint_group.ref,
            description="interface endpoints",
        )
        ec2.CfnVPCEndpoint(
            self,
            "S3Gateway",
            vpc_id=vpc.ref,
            service_name=f"com.amazonaws.{Aws.REGION}.s3",
            vpc_endpoint_type="Gateway",
            route_table_ids=[table.ref],
        )
        for service in SESSION_ENDPOINTS + BEDROCK_ENDPOINTS:
            endpoint = ec2.CfnVPCEndpoint(
                self,
                "Endpoint" + "".join(part.capitalize() for part in service.split("-")),
                vpc_id=vpc.ref,
                service_name=f"com.amazonaws.{Aws.REGION}.{service}",
                vpc_endpoint_type="Interface",
                subnet_ids=[subnet.ref],
                security_group_ids=[endpoint_group.ref],
                private_dns_enabled=True,
            )
            if service in BEDROCK_ENDPOINTS:
                endpoint.cfn_options.condition = with_bedrock
        CfnOutput(self, "SubnetId", value=subnet.ref)
        CfnOutput(self, "InstanceGroupId", value=instance_group.ref)


class ArtifactsStack(Stack):
    """One private, TLS-only bucket for the staged bundle and the collected results.

    Destroying it requires it to be empty: the runner's teardown removes the run's objects
    under its own confirmation first; nothing deletes objects automatically.
    """

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, synthesizer=BootstraplessSynthesizer(), **kwargs)
        _stack(self)
        bucket = s3.CfnBucket(
            self,
            "Artifacts",
            public_access_block_configuration=s3.CfnBucket.PublicAccessBlockConfigurationProperty(
                block_public_acls=True,
                block_public_policy=True,
                ignore_public_acls=True,
                restrict_public_buckets=True,
            ),
            ownership_controls=s3.CfnBucket.OwnershipControlsProperty(
                rules=[
                    s3.CfnBucket.OwnershipControlsRuleProperty(
                        object_ownership="BucketOwnerEnforced"
                    )
                ]
            ),
            bucket_encryption=s3.CfnBucket.BucketEncryptionProperty(
                server_side_encryption_configuration=[
                    s3.CfnBucket.ServerSideEncryptionRuleProperty(
                        server_side_encryption_by_default=s3.CfnBucket.ServerSideEncryptionByDefaultProperty(
                            sse_algorithm="AES256"
                        )
                    )
                ]
            ),
        )
        s3.CfnBucketPolicy(
            self,
            "TlsOnly",
            bucket=bucket.ref,
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "DenyInsecureTransport",
                        "Effect": "Deny",
                        "Principal": "*",
                        "Action": "s3:*",
                        "Resource": [bucket.attr_arn, Fn.join("", [bucket.attr_arn, "/*"])],
                        "Condition": {"Bool": {"aws:SecureTransport": "false"}},
                    }
                ],
            },
        )
        CfnOutput(self, "BucketName", value=bucket.ref)


SESSION_ACTIONS = (
    "ssm:UpdateInstanceInformation",
    "ssm:ListAssociations",
    "ssm:ListInstanceAssociations",
    "ssm:UpdateInstanceAssociationStatus",
    "ssm:GetDocument",
    "ssm:DescribeDocument",
    "ssmmessages:CreateControlChannel",
    "ssmmessages:CreateDataChannel",
    "ssmmessages:OpenControlChannel",
    "ssmmessages:OpenDataChannel",
    "ec2messages:AcknowledgeMessage",
    "ec2messages:DeleteMessage",
    "ec2messages:FailMessage",
    "ec2messages:GetEndpoint",
    "ec2messages:GetMessages",
    "ec2messages:SendReply",
)
BEDROCK_READS = ("bedrock:GetAccountDataRetention", "bedrock:GetFoundationModelAvailability")


class BoundaryStack(Stack):
    """The run-specific permissions boundary: the ceiling of the instance role, written
    from the real bucket name and the run id, so it admits exactly this run's inputs and
    results, the Session Manager and Run Command actions, the one embedding model when
    the Bedrock lane is enabled, the account reads, and nothing else; streaming, KMS, IAM
    and role assumption are denied outright."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, synthesizer=BootstraplessSynthesizer(), **kwargs)
        run = _stack(self)
        bucket = CfnParameter(self, "ArtifactBucket", type="String")
        model = CfnParameter(self, "BedrockModelArn", type="String", default="unset")
        with_bedrock = CfnCondition(
            self,
            "WithBedrock",
            expression=Fn.condition_not(Fn.condition_equals(model.value_as_string, "unset")),
        )
        bucket_arn = Fn.join("", [f"arn:{Aws.PARTITION}:s3:::", bucket.value_as_string])
        inputs = Fn.join("", ["inputs/", run.value_as_string, "/*"])
        results = Fn.join("", ["results/", run.value_as_string, "/*"])
        policy = iam.CfnManagedPolicy(
            self,
            "RunBoundary",
            path="/biblios/",
            description="Permissions boundary of one qualification run's instance role",
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Sid": "SessionManagerAndRunCommand",
                        "Effect": "Allow",
                        "Action": list(SESSION_ACTIONS),
                        "Resource": "*",
                    },
                    {
                        "Sid": "RunInputs",
                        "Effect": "Allow",
                        "Action": "s3:GetObject",
                        "Resource": Fn.join("", [bucket_arn, "/", inputs]),
                    },
                    {
                        "Sid": "RunResults",
                        "Effect": "Allow",
                        "Action": "s3:PutObject",
                        "Resource": Fn.join("", [bucket_arn, "/", results]),
                    },
                    {
                        "Sid": "RunListing",
                        "Effect": "Allow",
                        "Action": "s3:ListBucket",
                        "Resource": bucket_arn,
                        "Condition": {"StringLike": {"s3:prefix": [inputs, results]}},
                    },
                    {
                        "Sid": "AccountReads",
                        "Effect": "Allow",
                        "Action": [*BEDROCK_READS, "sts:GetCallerIdentity"],
                        "Resource": "*",
                    },
                    Fn.condition_if(
                        with_bedrock.logical_id,
                        {
                            "Sid": "OneEmbeddingModel",
                            "Effect": "Allow",
                            "Action": "bedrock:InvokeModel",
                            "Resource": model.value_as_string,
                        },
                        Aws.NO_VALUE,
                    ),
                    {
                        "Sid": "NeverStreamNeverKmsNeverIam",
                        "Effect": "Deny",
                        "Action": [
                            "bedrock:InvokeModelWithResponseStream",
                            "kms:*",
                            "iam:*",
                            "sts:AssumeRole",
                        ],
                        "Resource": "*",
                    },
                ],
            },
        )
        CfnOutput(self, "BoundaryArn", value=policy.ref)


class CustodyStack(Stack):
    """The instance role under the account's boundary: Session Manager, the run's bucket
    prefixes, and, only when the Bedrock lane is enabled, one embedding model."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, synthesizer=BootstraplessSynthesizer(), **kwargs)
        run = _stack(self)
        bucket = CfnParameter(self, "ArtifactBucket", type="String")
        boundary = CfnParameter(
            self,
            "WorkloadBoundaryArn",
            type="String",
            description="The administrator-owned permissions boundary every created role must "
            "carry",
            allowed_pattern=r"^arn:aws[a-z-]*:iam::[0-9]{12}:policy/.+$",
        )
        role_path = CfnParameter(
            self,
            "RolePath",
            type="String",
            default="/biblios/",
            allowed_pattern=r"^/[a-z0-9/-]*/$",
            description="The role path the account admits for experiment roles",
        )
        model = CfnParameter(
            self,
            "BedrockModelArn",
            type="String",
            default="unset",
            description="The one embedding model the Bedrock lane may invoke, or unset",
        )
        with_bedrock = CfnCondition(
            self,
            "WithBedrock",
            expression=Fn.condition_not(Fn.condition_equals(model.value_as_string, "unset")),
        )
        bucket_arn = Fn.join("", [f"arn:{Aws.PARTITION}:s3:::", bucket.value_as_string])
        role = iam.CfnRole(
            self,
            "InstanceRole",
            path=role_path.value_as_string,
            permissions_boundary=boundary.value_as_string,
            assume_role_policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Principal": {"Service": "ec2.amazonaws.com"},
                        "Action": "sts:AssumeRole",
                    }
                ],
            },
            managed_policy_arns=[
                f"arn:{Aws.PARTITION}:iam::aws:policy/AmazonSSMManagedInstanceCore"
            ],
            policies=[
                iam.CfnRole.PolicyProperty(
                    policy_name="run-artifacts",
                    policy_document={
                        "Version": "2012-10-17",
                        "Statement": [
                            {
                                "Effect": "Allow",
                                "Action": "s3:GetObject",
                                "Resource": Fn.join(
                                    "", [bucket_arn, "/inputs/", run.value_as_string, "/*"]
                                ),
                            },
                            {
                                "Effect": "Allow",
                                "Action": "s3:PutObject",
                                "Resource": Fn.join(
                                    "", [bucket_arn, "/results/", run.value_as_string, "/*"]
                                ),
                            },
                            {
                                "Effect": "Allow",
                                "Action": "s3:ListBucket",
                                "Resource": bucket_arn,
                                "Condition": {
                                    "StringLike": {
                                        "s3:prefix": [
                                            Fn.join("", ["inputs/", run.value_as_string, "/*"]),
                                            Fn.join("", ["results/", run.value_as_string, "/*"]),
                                        ]
                                    }
                                },
                            },
                        ],
                    },
                )
            ],
        )
        bedrock = iam.CfnPolicy(
            self,
            "BedrockLane",
            policy_name="bedrock-lane",
            roles=[role.ref],
            policy_document={
                "Version": "2012-10-17",
                "Statement": [
                    {
                        "Effect": "Allow",
                        "Action": "bedrock:InvokeModel",
                        "Resource": model.value_as_string,
                    },
                    {
                        "Effect": "Allow",
                        "Action": list(BEDROCK_READS),
                        "Resource": "*",
                    },
                ],
            },
        )
        bedrock.cfn_options.condition = with_bedrock
        profile = iam.CfnInstanceProfile(
            self, "InstanceProfile", path=role_path.value_as_string, roles=[role.ref]
        )
        CfnOutput(self, "InstanceProfileName", value=profile.ref)
        CfnOutput(self, "InstanceRoleName", value=role.ref)


def instance_resource(
    stack: Stack,
    *,
    subnet_id: str,
    group_id: str,
    public_address: bool,
) -> ec2.CfnInstance:
    """The one qualification instance; shared by the private and the example shapes.

    IMDSv2 required, one explicit network interface, root plus one gp3 data volume whose
    encryption property is deliberately absent (the account's default decides), and
    termination on guest shutdown.
    """
    image = CfnParameter(stack, "ImageId", type="AWS::EC2::Image::Id")
    profile = CfnParameter(stack, "InstanceProfileName", type="String")
    instance_type = CfnParameter(
        stack,
        "InstanceType",
        type="String",
        default=INSTANCE_TYPES[1],
        allowed_values=list(INSTANCE_TYPES),
    )
    data_gib = CfnParameter(
        stack, "DataVolumeGiB", type="Number", default=32, min_value=8, max_value=512
    )
    instance = ec2.CfnInstance(
        stack,
        "Instance",
        image_id=image.value_as_string,
        instance_type=instance_type.value_as_string,
        iam_instance_profile=profile.value_as_string,
        instance_initiated_shutdown_behavior="terminate",
        metadata_options=ec2.CfnInstance.MetadataOptionsProperty(
            http_tokens="required", http_put_response_hop_limit=1, http_endpoint="enabled"
        ),
        network_interfaces=[
            ec2.CfnInstance.NetworkInterfaceProperty(
                device_index="0",
                subnet_id=subnet_id,
                group_set=[group_id],
                associate_public_ip_address=public_address,
                delete_on_termination=True,
            )
        ],
        block_device_mappings=[
            ec2.CfnInstance.BlockDeviceMappingProperty(
                device_name=DATA_DEVICE,
                ebs=ec2.CfnInstance.EbsProperty(
                    volume_type="gp3",
                    volume_size=data_gib.value_as_number,
                    delete_on_termination=True,
                ),
            )
        ],
        user_data=Fn.base64(BOOTSTRAP),
        propagate_tags_to_volume_on_creation=True,
    )
    CfnOutput(stack, "InstanceId", value=instance.ref)
    return instance


class InstanceStack(Stack):
    """The private shape's instance: no public address, in the private subnet and group."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, synthesizer=BootstraplessSynthesizer(), **kwargs)
        _stack(self)
        subnet = CfnParameter(self, "SubnetId", type="AWS::EC2::Subnet::Id")
        group = CfnParameter(self, "InstanceGroupId", type="AWS::EC2::SecurityGroup::Id")
        instance_resource(
            self,
            subnet_id=subnet.value_as_string,
            group_id=group.value_as_string,
            public_address=False,
        )
