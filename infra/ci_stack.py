"""GitHub Actions -> AWS without stored keys: an OIDC provider and two roles.

* ``cotizador-mcp-ci-diff``: assumable from pull requests; may only assume the CDK
  bootstrap *lookup* role (read-only), which is what ``cdk diff`` needs.
* ``cotizador-mcp-ci-deploy``: assumable only from ``main``; may assume the CDK
  bootstrap deploy and asset-publishing roles, which carry the actual permissions.

Deployed once by hand (it is what lets CI deploy everything else).
"""

from __future__ import annotations

from aws_cdk import CfnOutput, Stack
from aws_cdk import aws_iam as iam
from constructs import Construct

GITHUB_OIDC = "token.actions.githubusercontent.com"
#: The default CDK bootstrap qualifier.
QUALIFIER = "hnb659fds"


class CotizadorMcpCiStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, repo: str, **kwargs: object) -> None:
        super().__init__(scope, construct_id, **kwargs)

        provider = iam.OidcProviderNative(
            self,
            "GitHub",
            url=f"https://{GITHUB_OIDC}",
            client_ids=["sts.amazonaws.com"],
        )

        def github_role(name: str, subject: str, bootstrap_roles: list[str]) -> iam.Role:
            role = iam.Role(
                self,
                name,
                role_name=f"cotizador-mcp-ci-{name.lower()}",
                max_session_duration=None,
                assumed_by=iam.WebIdentityPrincipal(
                    provider.oidc_provider_arn,
                    conditions={
                        "StringEquals": {f"{GITHUB_OIDC}:aud": "sts.amazonaws.com"},
                        "StringLike": {f"{GITHUB_OIDC}:sub": subject},
                    },
                ),
            )
            role.add_to_policy(
                iam.PolicyStatement(
                    actions=["sts:AssumeRole", "sts:TagSession"],
                    resources=[
                        f"arn:aws:iam::{self.account}:role/cdk-{QUALIFIER}-{kind}-role-{self.account}-{self.region}"
                        for kind in bootstrap_roles
                    ],
                )
            )
            return role

        diff = github_role("Diff", f"repo:{repo}:pull_request", ["lookup"])
        deploy = github_role(
            "Deploy", f"repo:{repo}:ref:refs/heads/main", ["deploy", "file-publishing", "lookup"]
        )

        CfnOutput(self, "DiffRoleArn", value=diff.role_arn)
        CfnOutput(self, "DeployRoleArn", value=deploy.role_arn)
