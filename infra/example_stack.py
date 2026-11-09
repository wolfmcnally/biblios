"""The low-security example environment: a public subnet, no inbound rules, Session Manager
over the internet. It demonstrates and documents; nothing qualifies on it."""

from __future__ import annotations

from aws_cdk import BootstraplessSynthesizer, CfnOutput, CfnParameter, Fn, Stack
from aws_cdk import aws_ec2 as ec2
from constructs import Construct
from qualification_stack import _stack, instance_resource

EXAMPLE_CIDR = "10.45.0.0/24"
EXAMPLE_SUBNET_CIDR = "10.45.0.0/26"


class ExampleNetworkStack(Stack):
    """One public subnet behind an internet gateway; the instance group admits nothing in."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, synthesizer=BootstraplessSynthesizer(), **kwargs)
        _stack(self)
        vpc = ec2.CfnVPC(
            self, "Vpc", cidr_block=EXAMPLE_CIDR, enable_dns_hostnames=True, enable_dns_support=True
        )
        gateway = ec2.CfnInternetGateway(self, "Gateway")
        attachment = ec2.CfnVPCGatewayAttachment(
            self, "GatewayAttachment", vpc_id=vpc.ref, internet_gateway_id=gateway.ref
        )
        subnet = ec2.CfnSubnet(
            self,
            "PublicSubnet",
            vpc_id=vpc.ref,
            cidr_block=EXAMPLE_SUBNET_CIDR,
            availability_zone=Fn.select(0, Fn.get_azs()),
            map_public_ip_on_launch=True,
        )
        table = ec2.CfnRouteTable(self, "PublicRoutes", vpc_id=vpc.ref)
        route = ec2.CfnRoute(
            self,
            "DefaultRoute",
            route_table_id=table.ref,
            destination_cidr_block="0.0.0.0/0",
            gateway_id=gateway.ref,
        )
        route.add_resource_dependency(attachment)
        ec2.CfnSubnetRouteTableAssociation(
            self, "PublicAssociation", subnet_id=subnet.ref, route_table_id=table.ref
        )
        group = ec2.CfnSecurityGroup(
            self,
            "InstanceGroup",
            group_description="Example instance: no inbound rules, HTTPS out to the internet",
            vpc_id=vpc.ref,
            security_group_ingress=[],
            security_group_egress=[
                ec2.CfnSecurityGroup.EgressProperty(
                    ip_protocol="tcp", from_port=443, to_port=443, cidr_ip="0.0.0.0/0"
                )
            ],
        )
        CfnOutput(self, "SubnetId", value=subnet.ref)
        CfnOutput(self, "InstanceGroupId", value=group.ref)


class ExampleInstanceStack(Stack):
    """The example shape's instance: the same instance, with a public address for egress."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, synthesizer=BootstraplessSynthesizer(), **kwargs)
        _stack(self)
        subnet = CfnParameter(self, "SubnetId", type="AWS::EC2::Subnet::Id")
        group = CfnParameter(self, "InstanceGroupId", type="AWS::EC2::SecurityGroup::Id")
        instance_resource(
            self,
            subnet_id=subnet.value_as_string,
            group_id=group.value_as_string,
            public_address=True,
        )
