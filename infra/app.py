"""Synthesize the qualification and example stack sets; synthesis alone creates nothing.

`CDK_OUTDIR` must be an absolute directory outside the repository, so no synthesized
template lands in the tree. Account and region come from the CDK's default environment
variables at deploy time. `BIBLIOS_INFRA_PLANT=lookup` is the offline check's planted
fault: it makes the app attempt a context lookup, which needs an AWS call, so the
credentials-withheld proof can show that such a call is refused.
"""

from __future__ import annotations

import os
from pathlib import Path

import aws_cdk as cdk
from example_stack import ExampleInstanceStack, ExampleNetworkStack
from qualification_stack import (
    ArtifactsStack,
    BoundaryStack,
    CustodyStack,
    InstanceStack,
    NetworkStack,
)

repository = Path(__file__).resolve().parent.parent
try:
    output = Path(os.environ["CDK_OUTDIR"])
    if not output.is_absolute() or output.resolve().is_relative_to(repository):
        raise ValueError("CDK_OUTDIR must be an absolute path outside the source repository")
except (KeyError, ValueError) as error:
    raise SystemExit("REFUSED: external CDK output is required") from error

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region=os.environ.get("CDK_DEFAULT_REGION")
)
app = cdk.App(outdir=str(output))
# The private qualification shape: deploy in this order, destroy in reverse.
NetworkStack(app, "biblios-qual-network", env=env)
ArtifactsStack(app, "biblios-qual-artifacts", env=env)
BoundaryStack(app, "biblios-qual-boundary", env=env)
CustodyStack(app, "biblios-qual-custody", env=env)
InstanceStack(app, "biblios-qual-instance", env=env)
# The low-security example shape: its own two stacks, deployed and destroyed separately.
ExampleNetworkStack(app, "biblios-example-network", env=env)
ExampleInstanceStack(app, "biblios-example-instance", env=env)
if os.environ.get("BIBLIOS_INFRA_PLANT") == "lookup":
    from aws_cdk import aws_ec2 as ec2

    planted = cdk.Stack(app, "biblios-planted-lookup", env=env)
    ec2.Vpc.from_lookup(planted, "Planted", is_default=True)
app.synth()
