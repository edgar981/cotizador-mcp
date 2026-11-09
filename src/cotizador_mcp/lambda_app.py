"""The ASGI app as it runs on AWS Lambda.

The Lambda Web Adapter starts uvicorn with :func:`create_app` (see ``infra/lambda/run.sh``)
and forwards API Gateway requests to it as plain HTTP, so the code path is the same one
that runs locally with ``--transport http``; only the stores differ (DynamoDB here).
Configuration comes from environment variables set by the CDK stack.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

import boto3
from starlette.applications import Starlette

from cotizador_mcp.auth import AuthConfig
from cotizador_mcp.dynamo import DynamoAuditSink, DynamoQuoteStore, DynamoRateCache
from cotizador_mcp.extract import ExtractorConfig, ProductExtractor
from cotizador_mcp.fx import RateSource
from cotizador_mcp.server import http_app
from cotizador_mcp.settings import CATEGORY_WEIGHTS_LB

__all__ = ["create_app"]


def create_app(env: Mapping[str, str] | None = None) -> Starlette:
    env = dict(os.environ if env is None else env)
    # Audit lines and errors go to stdout, which Lambda ships to CloudWatch Logs.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")

    auth = AuthConfig.from_env(env)
    if auth is None:
        raise RuntimeError(
            "COTIZADOR_COGNITO_POOL_ID must be set: the remote server never runs open."
        )

    dynamodb = boto3.resource("dynamodb", region_name=env.get("AWS_REGION"))
    return http_app(
        auth,
        host="127.0.0.1",
        rates=RateSource(DynamoRateCache(dynamodb.Table(env["COTIZADOR_FX_TABLE"]))),
        store=DynamoQuoteStore(dynamodb.Table(env["COTIZADOR_QUOTES_TABLE"])),
        audit_sink=DynamoAuditSink(dynamodb.Table(env["COTIZADOR_AUDIT_TABLE"])),
        extractor=ProductExtractor(ExtractorConfig.from_env(env), CATEGORY_WEIGHTS_LB),
    )
