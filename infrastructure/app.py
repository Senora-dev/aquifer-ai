#!/usr/bin/env python3
"""CDK app entrypoint. Synthesizes the single Aquifer stack."""

from __future__ import annotations

import os
import sys

import aws_cdk as cdk
from aquifer_stack import AquiferStack
from stack_config import ConfigError

app = cdk.App()

try:
    AquiferStack(
        app,
        "AquiferStack",
        env=cdk.Environment(
            account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
            region=os.environ.get("CDK_DEFAULT_REGION"),
        ),
        description="Aquifer — engineering context for AI agents (single stack)",
    )
except ConfigError as exc:
    print(f"Aquifer configuration error: {exc}", file=sys.stderr)
    sys.exit(1)

app.synth()
