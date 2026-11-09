"""OAuth for the HTTP transport: Cognito as authorization server, this server as resource server.

Three pieces, each answering one question:

* :class:`CognitoTokenVerifier`: is this bearer token one Cognito issued *for this server*?
* :func:`metadata_routes`: where should a client go to get a token? (RFC 9728 + RFC 8414)
* :class:`ScopeGuard`: may this token call this tool?

Why there is a metadata shim at all
-----------------------------------
MCP clients discover the authorization server from its RFC 8414 / OIDC metadata and
**must refuse** to proceed when ``code_challenge_methods_supported`` is absent. Cognito
supports PKCE S256 but its published metadata omits that field (and lists only
confidential-client auth methods). So this server publishes the authorization-server
metadata itself: the endpoints are Cognito's, unchanged; only the description is
completed. Tokens are still issued and signed by Cognito, and verified against
Cognito's issuer and keys. See ``docs/decisions.md``, R1.

Why the audience check matters
------------------------------
Cognito access tokens normally carry no ``aud``. MCP clients send the RFC 8707
``resource`` parameter, and Cognito then binds the token to that URL in ``aud``. A
token minted for some other API in the same user pool therefore fails here, which
is the confused-deputy protection the MCP authorization spec asks for.

Why scope names contain a URL
-----------------------------
Cognito only binds a token to a resource whose custom scopes belong to it: "custom
scopes requested for resource-binding must be assigned to the resource being
requested". So the Cognito resource server's identifier *is* this server's MCP URL,
and the full scope strings are ``<mcp url>/quotes:read`` and ``<mcp url>/quotes:write``.
Each environment (localhost, the AWS URL) therefore has its own resource server
and its own scope strings. Tools refer to scopes by their short names.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import anyio.to_thread
import jwt
from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.exceptions import ToolError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

__all__ = [
    "READ",
    "SCOPE_NAMES",
    "WRITE",
    "AuthConfig",
    "CognitoTokenVerifier",
    "ScopeGuard",
    "metadata_routes",
]

#: Short scope names. The full scope string is ``AuthConfig.scope(name)``.
READ = "quotes:read"
WRITE = "quotes:write"
SCOPE_NAMES = (READ, WRITE)

PRM_PATH = "/.well-known/oauth-protected-resource"
AS_METADATA_PATH = "/.well-known/oauth-authorization-server"


@dataclass(frozen=True, slots=True)
class AuthConfig:
    """Where tokens come from and who this server is.

    ``public_url`` is the origin clients reach this server at (``http://localhost:8000``
    locally, the API Gateway URL in AWS). The MCP endpoint, and therefore the token
    audience, is ``public_url + /mcp``.
    """

    public_url: str
    user_pool_id: str
    cognito_domain: str
    client_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.client_ids:
            raise ValueError("At least one Cognito app client id is required.")
        object.__setattr__(self, "public_url", self.public_url.rstrip("/"))
        object.__setattr__(self, "cognito_domain", self.cognito_domain.rstrip("/"))

    @property
    def region(self) -> str:
        return self.user_pool_id.split("_", 1)[0]

    @property
    def issuer(self) -> str:
        """The ``iss`` Cognito writes into every token of this pool."""
        return f"https://cognito-idp.{self.region}.amazonaws.com/{self.user_pool_id}"

    @property
    def jwks_uri(self) -> str:
        return f"{self.issuer}/.well-known/jwks.json"

    @property
    def resource_url(self) -> str:
        """The MCP endpoint: token audience and Cognito resource-server identifier."""
        return f"{self.public_url}/mcp"

    def scope(self, name: str) -> str:
        """Full scope string as Cognito issues it, e.g. ``http://localhost:8000/mcp/quotes:read``."""
        return f"{self.resource_url}/{name}"

    @property
    def scopes(self) -> tuple[str, ...]:
        return tuple(self.scope(name) for name in SCOPE_NAMES)

    def settings(self) -> AuthSettings:
        return AuthSettings(
            # The authorization server clients are pointed at: this server's own
            # metadata shim, whose endpoints are Cognito's.
            issuer_url=self.public_url,
            resource_server_url=self.resource_url,
            # Gate at the endpoint: every call needs at least read. Tools that
            # need more check it themselves (ScopeGuard).
            required_scopes=[self.scope(READ)],
            # Second, independent check of the audience the verifier extracted.
            validate_token_resource=True,
        )

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> AuthConfig | None:
        """Read the config from ``COTIZADOR_*`` variables; ``None`` when auth is not configured."""
        env = dict(os.environ) if env is None else env
        pool = env.get("COTIZADOR_COGNITO_POOL_ID", "").strip()
        if not pool:
            return None
        return cls(
            public_url=env["COTIZADOR_PUBLIC_URL"],
            user_pool_id=pool,
            cognito_domain=env["COTIZADOR_COGNITO_DOMAIN"],
            client_ids=tuple(
                c.strip() for c in env["COTIZADOR_COGNITO_CLIENT_IDS"].split(",") if c.strip()
            ),
        )


KeyResolver = Callable[[str], Any]
"""Given a raw JWT, return the public key that should have signed it."""


class CognitoTokenVerifier:
    """Validate Cognito access tokens locally, against the pool's published keys.

    Every check that fails returns ``None``, which the SDK turns into a 401. The
    reason is deliberately not sent to the caller.
    """

    def __init__(self, config: AuthConfig, key_resolver: KeyResolver | None = None) -> None:
        self._config = config
        if key_resolver is None:
            jwks = jwt.PyJWKClient(config.jwks_uri, cache_keys=True, timeout=5)
            key_resolver = lambda token: jwks.get_signing_key_from_jwt(token).key
        self._resolve_key = key_resolver

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            # The key lookup may hit the network (first call, key rotation).
            key = await anyio.to_thread.run_sync(self._resolve_key, token)
            claims = jwt.decode(
                token,
                key,
                algorithms=["RS256"],
                issuer=self._config.issuer,
                audience=self._config.resource_url,
                options={"require": ["exp", "iat", "iss", "aud", "sub", "client_id", "token_use"]},
                leeway=30,
            )
        except (jwt.PyJWTError, jwt.PyJWKClientError, ValueError):
            return None
        # ID tokens are signed by the same pool; only access tokens authorize calls.
        if claims["token_use"] != "access":
            return None
        if claims["client_id"] not in self._config.client_ids:
            return None
        aud = claims["aud"]
        return AccessToken(
            token=token,
            client_id=claims["client_id"],
            scopes=str(claims.get("scope", "")).split(),
            expires_at=int(claims["exp"]),
            resource=aud if isinstance(aud, str) else self._config.resource_url,
            subject=claims["sub"],
            claims={"iss": claims["iss"], "username": claims.get("username")},
        )


class ScopeGuard:
    """Per-tool scope enforcement.

    The endpoint already demands :data:`READ`. Each tool still calls :meth:`require`
    with the scope it needs, so the tool-to-scope map is visible at the tool. With
    auth disabled (stdio, where whoever launched the process is the operator) it
    allows everything.
    """

    def __init__(self, config: AuthConfig | None) -> None:
        self._config = config

    def require(self, name: str) -> None:
        if self._config is None:
            return
        scope = self._config.scope(name)
        token = get_access_token()
        granted = set(token.scopes) if token else set()
        if scope not in granted:
            # A ToolError, so the model is told why and can tell the user, rather
            # than retrying blindly.
            raise ToolError(
                f"This tool needs the {scope!r} scope. The token grants: "
                f"{', '.join(sorted(granted)) or 'none'}."
            )


def metadata_routes(config: AuthConfig) -> list[Route]:
    """The two unauthenticated discovery documents a client reads before it has a token."""
    scopes = list(config.scopes)
    protected_resource = {
        "resource": config.resource_url,
        "authorization_servers": [config.public_url],
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
        "resource_name": "Cotizador",
    }
    authorization_server = {
        "issuer": config.public_url,
        "authorization_endpoint": f"{config.cognito_domain}/oauth2/authorize",
        "token_endpoint": f"{config.cognito_domain}/oauth2/token",
        "revocation_endpoint": f"{config.cognito_domain}/oauth2/revoke",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        # Public clients (Claude Code) hold no secret; PKCE protects the code.
        "token_endpoint_auth_methods_supported": ["none"],
        "revocation_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": scopes,
    }

    def serve(document: dict[str, Any]) -> Callable[[Request], Any]:
        async def endpoint(_: Request) -> JSONResponse:
            return JSONResponse(document, headers={"Cache-Control": "public, max-age=3600"})

        return endpoint

    return [
        # RFC 9728 §3.1: the well-known suffix goes before the resource's path.
        Route(PRM_PATH + "/mcp", serve(protected_resource), methods=["GET"]),
        Route(PRM_PATH, serve(protected_resource), methods=["GET"]),
        Route(AS_METADATA_PATH, serve(authorization_server), methods=["GET"]),
    ]
