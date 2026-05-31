#!/usr/bin/env python3
"""CDK app entrypoint. Synthesizes the single Aquifer stack."""

from __future__ import annotations

import os

import aws_cdk as cdk
from aquifer_stack import AquiferStack

app = cdk.App()

AquiferStack(
    app,
    "AquiferStack",
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION"),
    ),
    description="Aquifer.ai — in-VPC Context Lake (single-stack baseline)",
)

app.synth()
