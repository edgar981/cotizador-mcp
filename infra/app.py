"""CDK app: ``cdk synth`` / ``cdk diff`` / ``cdk deploy`` from the repository root."""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import aws_cdk as cdk

from infra.ci_stack import CotizadorMcpCiStack
from infra.stack import CotizadorMcpStack

app = cdk.App()
# Account from the caller's credentials (never committed); region fixed (decisions R5).
env = cdk.Environment(account=os.environ.get("CDK_DEFAULT_ACCOUNT"), region="us-east-2")

CotizadorMcpStack(
    app,
    "CotizadorMcp",
    env=env,
    alarm_email=app.node.try_get_context("alarm_email"),
    description="cotizador-mcp: MCP server over a pricing engine (Lambda, HTTP API, Cognito, DynamoDB).",
)
CotizadorMcpCiStack(
    app,
    "CotizadorMcpCi",
    env=env,
    repo=app.node.try_get_context("github_repo") or "edgar981/cotizador-mcp",
    description="cotizador-mcp: GitHub Actions OIDC roles for CI.",
)
cdk.Tags.of(app).add("project", "cotizador-mcp")
app.synth()
