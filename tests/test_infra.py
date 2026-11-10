"""Assertions on the synthesized CloudFormation: the properties that matter for security
and for the OAuth flow to work at all. Bundling is skipped; nothing is deployed."""

from __future__ import annotations

import json
from typing import Any

import pytest

cdk = pytest.importorskip("aws_cdk")
from aws_cdk.assertions import Match, Template

from infra.ci_stack import CotizadorMcpCiStack
from infra.stack import CotizadorMcpStack

ENV = cdk.Environment(account="111111111111", region="us-east-2")


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App(context={"aws:cdk:bundling-stacks": []})
    return Template.from_stack(
        CotizadorMcpStack(app, "Test", env=ENV, alarm_email="ops@example.com")
    )


def resources(template: Template, kind: str) -> dict[str, Any]:
    return template.find_resources(kind)


def test_authorizer_issuer_is_the_exact_token_iss_and_audience_is_the_mcp_url(
    template: Template,
) -> None:
    (authorizer,) = resources(template, "AWS::ApiGatewayV2::Authorizer").values()
    config = json.dumps(authorizer["Properties"]["JwtConfiguration"])
    assert '"https://cognito-idp.us-east-2.amazonaws.com/", {"Ref"' in config
    assert "ProviderURL" not in config
    assert '"/mcp"' in config


def test_only_the_mcp_routes_need_a_token(template: Template) -> None:
    routes = {
        r["Properties"]["RouteKey"]: r["Properties"]
        for r in resources(template, "AWS::ApiGatewayV2::Route").values()
    }
    assert routes["POST /mcp"]["AuthorizationType"] == "JWT"
    assert routes["GET /mcp"]["AuthorizationType"] == "JWT"
    for key in (
        "GET /.well-known/oauth-protected-resource",
        "GET /.well-known/oauth-protected-resource/mcp",
        "GET /.well-known/oauth-authorization-server",
    ):
        assert routes[key].get("AuthorizationType", "NONE") == "NONE"
    assert len(routes) == 5  # nothing else is exposed


def test_no_iam_wildcard_actions_and_bedrock_is_one_project(template: Template) -> None:
    for policy in resources(template, "AWS::IAM::Policy").values():
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = (
                statement["Action"]
                if isinstance(statement["Action"], list)
                else [statement["Action"]]
            )
            assert all("*" not in a for a in actions), actions
            if "bedrock-mantle:CreateInference" in actions:
                assert (
                    statement["Resource"]
                    == "arn:aws:bedrock-mantle:us-east-1:111111111111:project/default"
                )


def test_cognito_has_no_self_sign_up_and_a_public_pkce_client(template: Template) -> None:
    template.has_resource_properties(
        "AWS::Cognito::UserPool",
        {"AdminCreateUserConfig": {"AllowAdminCreateUserOnly": True}, "UserPoolTier": "ESSENTIALS"},
    )
    template.has_resource_properties(
        "AWS::Cognito::UserPoolClient",
        {
            "GenerateSecret": False,
            "AllowedOAuthFlows": ["code"],
            "CallbackURLs": ["http://localhost:8765/callback"],
            "PreventUserExistenceErrors": "ENABLED",
        },
    )
    template.has_resource_properties("AWS::Cognito::UserPoolDomain", {"ManagedLoginVersion": 2})
    # The domain is public (authorization-server metadata), so it must not carry the account.
    (domain,) = template.find_resources("AWS::Cognito::UserPoolDomain").values()
    assert "AWS::AccountId" not in json.dumps(domain["Properties"]["Domain"])


def test_stage_is_throttled_and_access_logged(template: Template) -> None:
    template.has_resource_properties(
        "AWS::ApiGatewayV2::Stage",
        {
            "DefaultRouteSettings": {"ThrottlingBurstLimit": 10, "ThrottlingRateLimit": 5},
            "AccessLogSettings": Match.object_like({"Format": Match.string_like_regexp('"sub"')}),
        },
    )


def test_logs_are_kept_thirty_days_and_audit_outlives_the_stack(template: Template) -> None:
    for group in resources(template, "AWS::Logs::LogGroup").values():
        assert group["Properties"]["RetentionInDays"] == 30
    tables = resources(template, "AWS::DynamoDB::GlobalTable")
    audit = next(t for t in tables.values() if "TimeToLiveSpecification" in t["Properties"])
    assert audit["DeletionPolicy"] == "Retain"
    assert audit["Properties"]["TimeToLiveSpecification"]["AttributeName"] == "expires_at"


def test_one_error_alarm_wired_to_email(template: Template) -> None:
    template.resource_count_is("AWS::CloudWatch::Alarm", 1)
    template.has_resource_properties(
        "AWS::SNS::Subscription", {"Protocol": "email", "Endpoint": "ops@example.com"}
    )


def test_alarm_email_is_in_no_logical_id(template: Template) -> None:
    # Logical ids print in `cdk diff` and deploy output, and CI logs are public. Secret
    # masking hides only the exact address, not a mangled form of it.
    ids = " ".join(template.to_json()["Resources"]).lower()
    assert "ops" not in ids and "example" not in ids


def test_lambda_runs_behind_the_web_adapter_with_auth_configured(template: Template) -> None:
    (fn,) = resources(template, "AWS::Lambda::Function").values()
    props = fn["Properties"]
    assert props["Architectures"] == ["arm64"] and props["Handler"] == "run.sh"
    env = props["Environment"]["Variables"]
    assert env["AWS_LAMBDA_EXEC_WRAPPER"] == "/opt/bootstrap"
    for key in (
        "COTIZADOR_PUBLIC_URL",
        "COTIZADOR_COGNITO_POOL_ID",
        "COTIZADOR_COGNITO_CLIENT_IDS",
    ):
        assert key in env
    assert props["Timeout"] < 30  # under API Gateway's limit


def test_ci_roles_trust_only_this_repo_and_the_approved_environment_for_deploy() -> None:
    app = cdk.App()
    prefix = "repo:owner@1/repo@2"  # GitHub's immutable form: owner and repository ids
    template = Template.from_stack(CotizadorMcpCiStack(app, "Ci", env=ENV, subject_prefix=prefix))
    roles = {
        r["Properties"]["RoleName"]: r["Properties"]
        for r in template.find_resources("AWS::IAM::Role").values()
    }
    deploy = json.dumps(roles["cotizador-mcp-ci-deploy"]["AssumeRolePolicyDocument"])
    diff = json.dumps(roles["cotizador-mcp-ci-diff"]["AssumeRolePolicyDocument"])
    assert f'"token.actions.githubusercontent.com:sub": "{prefix}:environment:production"' in deploy
    assert "refs/heads" not in deploy  # a job in an environment never presents a branch
    assert f'"token.actions.githubusercontent.com:sub": "{prefix}:pull_request"' in diff
    assert "StringLike" not in deploy + diff  # exact subjects, no wildcards
    assert '"token.actions.githubusercontent.com:aud": "sts.amazonaws.com"' in deploy
