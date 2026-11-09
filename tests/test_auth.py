"""OAuth as a resource server: token checks, discovery documents, scope per tool.

Tokens here are signed with a throwaway RSA key standing in for the Cognito
pool's. Nothing in this file talks to AWS.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken
from mcp.server.mcpserver.exceptions import ToolError
from starlette.testclient import TestClient
from support import CONFIG, ENVELOPE, OTHER_KEY, make_token, rpc_result, verifier

from cotizador_mcp.auth import READ, WRITE, ScopeGuard
from cotizador_mcp.server import http_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# --- the verifier -----------------------------------------------------------


def test_config_derives_issuer_and_audience() -> None:
    assert CONFIG.issuer == "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_TESTPOOL"
    assert CONFIG.resource_url == "http://localhost:8000/mcp"  # trailing slash dropped


@pytest.mark.anyio
async def test_valid_token_is_accepted_with_its_scopes_and_subject() -> None:
    token = await verifier().verify_token(make_token())
    assert token is not None
    assert token.client_id == "claude-code-client"
    assert token.subject == "user-123"
    assert set(token.scopes) == {
        "http://localhost:8000/mcp/quotes:read",
        "http://localhost:8000/mcp/quotes:write",
    }
    assert token.resource == CONFIG.resource_url


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("label", "overrides"),
    [
        ("token minted for another API", {"aud": "https://other-api.example.com"}),
        ("token with no resource binding", {"aud": None}),
        (
            "another user pool",
            {"iss": "https://cognito-idp.us-east-2.amazonaws.com/us-east-2_OTHER"},
        ),
        ("an ID token, not an access token", {"token_use": "id"}),
        ("an app client we never registered", {"client_id": "someone-else"}),
        ("expired", {"exp": int(time.time()) - 3600, "iat": int(time.time()) - 7200}),
    ],
)
async def test_tokens_that_must_be_refused(label: str, overrides: dict[str, Any]) -> None:
    assert await verifier().verify_token(make_token(**overrides)) is None, label


@pytest.mark.anyio
async def test_forged_signature_is_refused() -> None:
    assert await verifier().verify_token(make_token(key=OTHER_KEY)) is None


@pytest.mark.anyio
async def test_garbage_is_refused() -> None:
    assert await verifier().verify_token("not-a-jwt") is None


# --- the scope guard --------------------------------------------------------


def _as(names: list[str]) -> Any:
    scopes = [CONFIG.scope(n) for n in names]
    return auth_context_var.set(
        AuthenticatedUser(AccessToken(token="t", client_id="c", scopes=scopes))
    )


def test_scope_guard_refuses_a_token_without_the_scope() -> None:
    reset = _as([READ])
    try:
        with pytest.raises(ToolError, match="quotes:write"):
            ScopeGuard(CONFIG).require(WRITE)
    finally:
        auth_context_var.reset(reset)


def test_scope_guard_allows_a_token_with_the_scope() -> None:
    reset = _as([READ, WRITE])
    try:
        ScopeGuard(CONFIG).require(WRITE)
    finally:
        auth_context_var.reset(reset)


def test_scope_guard_is_off_without_auth() -> None:
    ScopeGuard(None).require(WRITE)  # stdio: the operator launched the process


# --- over HTTP --------------------------------------------------------------


@pytest.fixture
def client() -> Any:
    with TestClient(
        http_app(CONFIG, token_verifier=verifier()), base_url="http://localhost:8000"
    ) as c:
        yield c


def call_quote(client: TestClient, token: str | None) -> Any:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": "2026-07-28",
        "Mcp-Method": "tools/call",
        "Mcp-Name": "quote_product",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "quote_product",
            "arguments": {"price_usd": 120, "category": "tenis"},
            "_meta": ENVELOPE,
        },
    }
    return client.post("/mcp", headers=headers, json=body)


def test_no_token_gets_401_pointing_at_the_resource_metadata(client: TestClient) -> None:
    r = call_quote(client, None)
    assert r.status_code == 401
    challenge = r.headers["www-authenticate"]
    assert challenge.startswith("Bearer ")
    assert (
        'resource_metadata="http://localhost:8000/.well-known/oauth-protected-resource/mcp"'
        in challenge
    )


def test_protected_resource_metadata_lists_every_scope(client: TestClient) -> None:
    doc = client.get("/.well-known/oauth-protected-resource/mcp").json()
    assert doc["resource"] == "http://localhost:8000/mcp"
    assert doc["authorization_servers"] == ["http://localhost:8000"]
    # Named after the resource itself: Cognito binds a token only to scopes of
    # the resource server whose identifier is the requested resource.
    assert doc["scopes_supported"] == [
        "http://localhost:8000/mcp/quotes:read",
        "http://localhost:8000/mcp/quotes:write",
    ]


def test_authorization_server_metadata_completes_what_cognito_omits(client: TestClient) -> None:
    doc = client.get("/.well-known/oauth-authorization-server").json()
    assert doc["issuer"] == "http://localhost:8000"  # the URL clients discovered it at
    assert doc["code_challenge_methods_supported"] == ["S256"]
    assert doc["token_endpoint_auth_methods_supported"] == ["none"]
    assert doc["authorization_endpoint"] == (
        "https://example-dev.auth.us-east-2.amazoncognito.com/oauth2/authorize"
    )
    assert doc["token_endpoint"].endswith(".amazoncognito.com/oauth2/token")


def test_valid_token_can_quote(client: TestClient) -> None:
    r = call_quote(client, make_token())
    assert r.status_code == 200
    assert rpc_result(r)["structuredContent"]["price_cop"] == 735000


def test_token_for_another_api_gets_401(client: TestClient) -> None:
    r = call_quote(client, make_token(aud="https://other-api.example.com"))
    assert r.status_code == 401


def test_token_without_read_scope_gets_403(client: TestClient) -> None:
    r = call_quote(client, make_token(scope=CONFIG.scope(WRITE)))
    assert r.status_code == 403
    assert 'error="insufficient_scope"' in r.headers["www-authenticate"]
