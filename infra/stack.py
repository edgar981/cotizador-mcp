"""The service: API Gateway (HTTP API) -> Lambda (MCP server) -> DynamoDB and Bedrock,
with Cognito as the authorization server. See docs/decisions.md for the reasoning."""

from __future__ import annotations

from aws_cdk import (
    BundlingOptions,
    CfnOutput,
    DockerImage,
    Duration,
    Fn,
    RemovalPolicy,
    Stack,
)
from aws_cdk import aws_apigatewayv2 as apigw
from aws_cdk import aws_apigatewayv2_authorizers as authorizers
from aws_cdk import aws_apigatewayv2_integrations as integrations
from aws_cdk import aws_cloudwatch as cloudwatch
from aws_cdk import aws_cloudwatch_actions as cw_actions
from aws_cdk import aws_cognito as cognito
from aws_cdk import aws_dynamodb as dynamodb
from aws_cdk import aws_iam as iam
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from aws_cdk import aws_sns as sns
from constructs import Construct

from infra.bundling import ROOT, UvBundling

#: Lambda Web Adapter, published by AWS (account 753240598075). Pinned: 1.1.0.
LWA_LAYER = "arn:aws:lambda:{region}:753240598075:layer:LambdaAdapterLayerArm64:30"
#: Where Claude Code listens for the OAuth redirect (claude mcp add --callback-port 8765).
CLAUDE_CODE_CALLBACK = "http://localhost:8765/callback"


def _stack_suffix(stack: Stack) -> str:
    """First block of the stack id's UUID, e.g. ``e34fec60``: stable, unique, anonymous."""
    uuid = Fn.select(2, Fn.split("/", stack.stack_id))
    return Fn.select(0, Fn.split("-", uuid))


class CotizadorMcpStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        bedrock_region: str = "us-east-1",
        extract_model: str = "anthropic.claude-opus-5-5",
        extract_fallback_model: str = "anthropic.claude-opus-4-8",
        alarm_email: str | None = None,
        **kwargs: object,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # --- API first: its URL names the OAuth resource, so most things depend on it,
        # and nothing it owns depends on them (routes are added at the end).
        api = apigw.HttpApi(self, "Api", api_name="cotizador-mcp")
        public_url = api.api_endpoint  # https://{id}.execute-api.{region}.amazonaws.com
        resource_url = Fn.join("", [public_url, "/mcp"])

        # --- Cognito: the authorization server.
        pool = cognito.UserPool(
            self,
            "Users",
            user_pool_name="cotizador-mcp",
            feature_plan=cognito.FeaturePlan.ESSENTIALS,
            self_sign_up_enabled=False,  # an operator creates every user
            sign_in_aliases=cognito.SignInAliases(email=True),
            auto_verify=cognito.AutoVerifiedAttrs(email=True),
            account_recovery=cognito.AccountRecovery.EMAIL_ONLY,
            removal_policy=RemovalPolicy.DESTROY,
        )
        domain = pool.add_domain(
            "Login",
            cognito_domain=cognito.CognitoDomainOptions(
                # Unique per stack without publishing the account id: the domain shows
                # up in the public authorization-server metadata.
                domain_prefix=Fn.join("-", ["cotizador-mcp", _stack_suffix(self)])
            ),
            # Resource binding (the RFC 8707 `resource` parameter) needs managed login.
            managed_login_version=cognito.ManagedLoginVersion.NEWER_MANAGED_LOGIN,
        )
        read = cognito.ResourceServerScope(
            scope_name="quotes:read", scope_description="Compute and read quotes"
        )
        write = cognito.ResourceServerScope(
            scope_name="quotes:write", scope_description="Save quotes"
        )
        # Cognito binds a token only to scopes of the resource server whose identifier
        # IS the requested resource, so the identifier is the MCP URL (decisions R1).
        resource_server = pool.add_resource_server(
            "Mcp",
            identifier=resource_url,
            user_pool_resource_server_name="cotizador-mcp",
            scopes=[read, write],
        )
        claude_code = pool.add_client(
            "ClaudeCode",
            user_pool_client_name="claude-code",
            generate_secret=False,  # public client: PKCE, nothing to leak
            o_auth=cognito.OAuthSettings(
                flows=cognito.OAuthFlows(authorization_code_grant=True),
                scopes=[
                    cognito.OAuthScope.resource_server(resource_server, read),
                    cognito.OAuthScope.resource_server(resource_server, write),
                ],
                callback_urls=[CLAUDE_CODE_CALLBACK],
            ),
            supported_identity_providers=[cognito.UserPoolClientIdentityProvider.COGNITO],
            prevent_user_existence_errors=True,
            enable_token_revocation=True,
            access_token_validity=Duration.minutes(60),
            id_token_validity=Duration.minutes(60),
            refresh_token_validity=Duration.days(7),
        )
        cognito.CfnManagedLoginBranding(
            self,
            "LoginStyle",
            user_pool_id=pool.user_pool_id,
            client_id=claude_code.user_pool_client_id,
            use_cognito_provided_values=True,
        )

        # --- DynamoDB (layouts documented in src/cotizador_mcp/dynamo.py).
        quotes = dynamodb.TableV2(
            self,
            "Quotes",
            partition_key=dynamodb.Attribute(name="owner", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="idempotency_key", type=dynamodb.AttributeType.STRING),
            point_in_time_recovery_specification=dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True
            ),
            removal_policy=RemovalPolicy.RETAIN,
        )
        audit = dynamodb.TableV2(
            self,
            "Audit",
            partition_key=dynamodb.Attribute(name="client_id", type=dynamodb.AttributeType.STRING),
            sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
            time_to_live_attribute="expires_at",
            global_secondary_indexes=[
                dynamodb.GlobalSecondaryIndexPropsV2(
                    index_name="by_tool",
                    partition_key=dynamodb.Attribute(
                        name="tool", type=dynamodb.AttributeType.STRING
                    ),
                    sort_key=dynamodb.Attribute(name="sk", type=dynamodb.AttributeType.STRING),
                )
            ],
            # An audit trail should outlive the stack that wrote it.
            removal_policy=RemovalPolicy.RETAIN,
        )
        fx_rates = dynamodb.TableV2(
            self,
            "FxRates",
            partition_key=dynamodb.Attribute(name="day", type=dynamodb.AttributeType.STRING),
            removal_policy=RemovalPolicy.DESTROY,  # a cache: refetched on demand
        )

        # --- The server.
        server_logs = logs.LogGroup(
            self,
            "ServerLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        server = lambda_.Function(
            self,
            "Server",
            runtime=lambda_.Runtime.PYTHON_3_12,
            architecture=lambda_.Architecture.ARM_64,
            handler="run.sh",
            code=lambda_.Code.from_asset(
                str(ROOT),
                exclude=[
                    ".git",
                    ".venv",
                    "cdk.out",
                    "tests",
                    "docs",
                    "tools",
                    ".github",
                    "**/__pycache__",
                    ".pytest_cache",
                    ".ruff_cache",
                    ".env*",
                    "*.md",
                ],
                bundling=BundlingOptions(
                    image=DockerImage.from_registry("unused-local-bundling"),
                    local=UvBundling(),
                ),
            ),
            layers=[
                lambda_.LayerVersion.from_layer_version_arn(
                    self, "WebAdapter", LWA_LAYER.format(region=self.region)
                )
            ],
            memory_size=1024,
            # API Gateway HTTP APIs end every request at 30 s.
            timeout=Duration.seconds(29),
            log_group=server_logs,
            environment={
                "AWS_LAMBDA_EXEC_WRAPPER": "/opt/bootstrap",
                "AWS_LWA_PORT": "8080",
                "AWS_LWA_READINESS_CHECK_PATH": "/.well-known/oauth-protected-resource",
                "COTIZADOR_PUBLIC_URL": public_url,
                "COTIZADOR_COGNITO_POOL_ID": pool.user_pool_id,
                "COTIZADOR_COGNITO_DOMAIN": domain.base_url(),
                "COTIZADOR_COGNITO_CLIENT_IDS": claude_code.user_pool_client_id,
                "COTIZADOR_QUOTES_TABLE": quotes.table_name,
                "COTIZADOR_AUDIT_TABLE": audit.table_name,
                "COTIZADOR_FX_TABLE": fx_rates.table_name,
                "COTIZADOR_BEDROCK_REGION": bedrock_region,
                "COTIZADOR_EXTRACT_MODEL": extract_model,
                "COTIZADOR_EXTRACT_FALLBACK_MODEL": extract_fallback_model,
            },
        )

        # --- Least privilege: each table gets only the calls the code makes.
        quotes.grant(server, "dynamodb:PutItem", "dynamodb:GetItem")
        audit.grant(server, "dynamodb:PutItem")
        fx_rates.grant(server, "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Scan")
        server.add_to_role_policy(
            iam.PolicyStatement(
                sid="ClaudeOnBedrock",
                # On the bedrock-mantle surface, inference is authorized on the project,
                # not on a model ARN (decisions R3). Only the default project, one region.
                actions=["bedrock-mantle:CreateInference"],
                resources=[
                    f"arn:aws:bedrock-mantle:{bedrock_region}:{self.account}:project/default"
                ],
            )
        )

        # --- Routes. The JWT authorizer is the first gate; the server checks again
        # (audience, client, scopes per tool).
        jwt = authorizers.HttpJwtAuthorizer(
            "Cognito",
            # Must equal the tokens' `iss` exactly. CloudFormation's ProviderURL omits
            # the scheme, so it cannot be used here.
            jwt_issuer=Fn.join(
                "", ["https://cognito-idp.", self.region, ".amazonaws.com/", pool.user_pool_id]
            ),
            jwt_audience=[resource_url],
        )
        backend = integrations.HttpLambdaIntegration("Server", server)
        api.add_routes(
            path="/mcp",
            methods=[apigw.HttpMethod.POST, apigw.HttpMethod.GET],
            integration=backend,
            authorizer=jwt,
            authorization_scopes=[Fn.join("", [resource_url, "/quotes:read"])],
        )
        for path in (
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-protected-resource/mcp",
            "/.well-known/oauth-authorization-server",
        ):
            api.add_routes(path=path, methods=[apigw.HttpMethod.GET], integration=backend)

        # Stage throttling caps what a leaked token (or anyone) can spend.
        access_logs = logs.LogGroup(
            self,
            "AccessLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.DESTROY,
        )
        stage = api.default_stage.node.default_child  # type: ignore[union-attr]
        stage.default_route_settings = apigw.CfnStage.RouteSettingsProperty(
            throttling_burst_limit=10, throttling_rate_limit=5
        )
        stage.access_log_settings = apigw.CfnStage.AccessLogSettingsProperty(
            destination_arn=access_logs.log_group_arn,
            format=(
                '{"requestId":"$context.requestId","ip":"$context.identity.sourceIp",'
                '"route":"$context.routeKey","status":"$context.status",'
                '"latencyMs":"$context.responseLatency",'
                '"clientId":"$context.authorizer.claims.client_id",'
                '"sub":"$context.authorizer.claims.sub",'
                '"authError":"$context.authorizer.error"}'
            ),
        )

        # --- One alarm: any server error.
        alarm = cloudwatch.Alarm(
            self,
            "ServerErrors",
            metric=server.metric_errors(period=Duration.minutes(5)),
            threshold=1,
            evaluation_periods=1,
            treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
            alarm_description="The MCP server Lambda raised an error.",
        )
        topic = sns.Topic(self, "Alarms")
        alarm.add_alarm_action(cw_actions.SnsAction(topic))
        if alarm_email:
            # A fixed construct id: EmailSubscription names the construct after the
            # address, which then shows in `cdk diff` output, and CI logs are public.
            sns.Subscription(
                self,
                "AlarmEmail",
                topic=topic,
                protocol=sns.SubscriptionProtocol.EMAIL,
                endpoint=alarm_email,
            )

        CfnOutput(self, "McpUrl", value=resource_url)
        CfnOutput(self, "ClaudeCodeClientId", value=claude_code.user_pool_client_id)
        CfnOutput(self, "UserPoolId", value=pool.user_pool_id)
        CfnOutput(self, "AuditTable", value=audit.table_name)
