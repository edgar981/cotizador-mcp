"""Shared test helpers: a fake Cognito pool and its tokens. Nothing here talks to AWS."""

from __future__ import annotations

import json
import time
from typing import Any

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from cotizador_mcp.auth import READ, WRITE, AuthConfig, CognitoTokenVerifier

CONFIG = AuthConfig(
    public_url="http://localhost:8000/",
    user_pool_id="us-east-2_TESTPOOL",
    cognito_domain="https://example-dev.auth.us-east-2.amazoncognito.com",
    client_ids=("claude-code-client",),
)
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def make_token(key: Any = KEY, **overrides: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": "user-123",
        "iss": CONFIG.issuer,
        "aud": CONFIG.resource_url,
        "client_id": "claude-code-client",
        "token_use": "access",
        "scope": f"{CONFIG.scope(READ)} {CONFIG.scope(WRITE)}",
        "username": "user-123",
        "iat": now,
        "exp": now + 600,
    }
    claims.update(overrides)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256", headers={"kid": "test"})


def verifier() -> CognitoTokenVerifier:
    return CognitoTokenVerifier(CONFIG, key_resolver=lambda _token: KEY.public_key())


ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
    "io.modelcontextprotocol/clientCapabilities": {},
}


def rpc_result(response: Any) -> dict[str, Any]:
    text = response.text
    if response.headers.get("content-type", "").startswith("text/event-stream"):
        text = next(line[5:] for line in text.splitlines() if line.startswith("data:"))
    return json.loads(text)["result"]
