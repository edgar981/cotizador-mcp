"""The Lambda app, wired to DynamoDB (moto) and behind a non-local Host."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from moto import mock_aws
from starlette.testclient import TestClient
from support import CONFIG, ENVELOPE, KEY, make_token
from test_dynamo import audit_table, quotes_table

from cotizador_mcp.auth import AuthConfig, CognitoTokenVerifier
from cotizador_mcp.lambda_app import create_app
from cotizador_mcp.server import http_app

API = "https://abc123.execute-api.us-east-2.amazonaws.com"
ENV = {
    "AWS_REGION": "us-east-2",
    "COTIZADOR_PUBLIC_URL": API,
    "COTIZADOR_COGNITO_POOL_ID": "us-east-2_TESTPOOL",
    "COTIZADOR_COGNITO_DOMAIN": "https://example.auth.us-east-2.amazoncognito.com",
    "COTIZADOR_COGNITO_CLIENT_IDS": "claude-code-client",
    "COTIZADOR_QUOTES_TABLE": "quotes",
    "COTIZADOR_AUDIT_TABLE": "audit",
    "COTIZADOR_FX_TABLE": "fx_rates",
}


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        db = boto3.resource("dynamodb", region_name="us-east-2")
        quotes_table(db)
        audit_table(db)
        yield db


def test_the_remote_app_refuses_to_start_without_auth(aws: Any) -> None:
    with pytest.raises(RuntimeError, match="never runs open"):
        create_app({**ENV, "COTIZADOR_COGNITO_POOL_ID": ""})


def test_behind_api_gateway_the_public_host_is_accepted_and_others_are_not(aws: Any) -> None:
    with TestClient(create_app(ENV), base_url=API) as client:
        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["resource"] == f"{API}/mcp"
        assert prm.json()["scopes_supported"][0] == f"{API}/mcp/quotes:read"
        assert client.post("/mcp", json={}).status_code == 401  # needs a token


def test_a_valid_token_on_a_foreign_host_is_still_refused() -> None:
    """DNS-rebinding guard: the server answers only for its own public host."""
    config = AuthConfig(
        public_url=API,
        user_pool_id=CONFIG.user_pool_id,
        cognito_domain=CONFIG.cognito_domain,
        client_ids=CONFIG.client_ids,
    )
    verify = CognitoTokenVerifier(config, key_resolver=lambda _t: KEY.public_key())
    token = make_token(aud=config.resource_url, scope=config.scope("quotes:read"))
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"_meta": ENVELOPE}}
    with TestClient(http_app(config, token_verifier=verify), base_url=API) as client:
        assert client.post("/mcp", json=body, headers=headers).status_code == 200
    with TestClient(
        http_app(config, token_verifier=verify), base_url="https://evil.example.com"
    ) as client:
        assert client.post("/mcp", json=body, headers=headers).status_code == 421
