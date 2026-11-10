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
    # The repository's OIDC subject prefix, immutable form (see ci_stack.py); a fork
    # passes its own with --context github_oidc_subject_prefix=...
    subject_prefix=app.node.try_get_context("github_oidc_subject_prefix")
    or "repo:edgar981@77586877/cotizador-mcp@1451981537",
    description="cotizador-mcp: GitHub Actions OIDC roles for CI.",
)
cdk.Tags.of(app).add("project", "cotizador-mcp")
app.synth()
