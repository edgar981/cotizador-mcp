"""save_quote (idempotent, server-computed) and the audit trail around every call."""

from __future__ import annotations

import logging
from typing import Any

import pytest
from mcp import Client
from starlette.testclient import TestClient
from support import CONFIG, ENVELOPE, make_token, rpc_result, verifier

from cotizador_mcp.audit import InMemoryAuditSink
from cotizador_mcp.auth import WRITE
from cotizador_mcp.server import build_server, http_app
from cotizador_mcp.store import InMemoryQuoteStore

SNEAKERS = {
    "product_name": "Running shoe",
    "price_usd": 120,
    "category": "tenis",
    "zone": "Bogotá",
    "idempotency_key": "4b1f0c4e-6a1e-4a43-9a51-1f1b2a7e0c11",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def call(server: Any, tool: str, args: dict[str, Any]) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, args)


# --- save_quote -------------------------------------------------------------


@pytest.mark.anyio
async def test_save_quote_is_advertised_as_an_idempotent_non_destructive_write() -> None:
    async with Client(build_server()) as client:
        tool = next(t for t in (await client.list_tools()).tools if t.name == "save_quote")
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert set(tool.input_schema["required"]) == {"product_name", "price_usd", "idempotency_key"}


@pytest.mark.anyio
async def test_same_key_twice_is_one_record_and_the_same_response() -> None:
    store = InMemoryQuoteStore()
    server = build_server(store=store)
    first = await call(server, "save_quote", SNEAKERS)
    second = await call(server, "save_quote", SNEAKERS)

    assert not first.is_error and not second.is_error
    assert first.structured_content == second.structured_content
    assert len(store.rows) == 1
    assert first.structured_content["quote"]["price_cop"] == 735000
    assert first.structured_content["quote"]["domestic_shipping_cop"] == 16000


@pytest.mark.anyio
async def test_reusing_a_key_for_a_different_product_is_refused() -> None:
    store = InMemoryQuoteStore()
    server = build_server(store=store)
    await call(server, "save_quote", SNEAKERS)
    result = await call(server, "save_quote", {**SNEAKERS, "price_usd": 999})
    assert result.is_error
    assert "already used" in result.content[0].text
    assert len(store.rows) == 1


@pytest.mark.anyio
async def test_different_keys_are_different_records() -> None:
    store = InMemoryQuoteStore()
    server = build_server(store=store)
    await call(server, "save_quote", SNEAKERS)
    await call(server, "save_quote", {**SNEAKERS, "idempotency_key": "another-key-123"})
    assert len(store.rows) == 2


@pytest.mark.anyio
@pytest.mark.parametrize("key", ["short", "has spaces in it", "x" * 129])
async def test_malformed_idempotency_keys_are_rejected(key: str) -> None:
    result = await call(
        build_server(store=InMemoryQuoteStore()), "save_quote", {**SNEAKERS, "idempotency_key": key}
    )
    assert result.is_error


@pytest.mark.anyio
async def test_only_https_product_urls_are_accepted() -> None:
    server = build_server(store=InMemoryQuoteStore())
    bad = await call(server, "save_quote", {**SNEAKERS, "product_url": "javascript:alert(1)"})
    good = await call(
        server, "save_quote", {**SNEAKERS, "product_url": "https://shop.example.com/p/1"}
    )
    assert bad.is_error and not good.is_error


@pytest.mark.anyio
async def test_without_a_store_saving_is_a_tool_error() -> None:
    result = await call(build_server(), "save_quote", SNEAKERS)
    assert result.is_error and "no quote store" in result.content[0].text


# --- audit trail ------------------------------------------------------------


@pytest.mark.anyio
async def test_every_tool_call_is_audited_with_its_outcome() -> None:
    sink = InMemoryAuditSink()
    server = build_server(store=InMemoryQuoteStore(), audit_sink=sink)
    async with Client(server) as client:
        await client.call_tool("quote_product", {"price_usd": 120, "category": "tenis"})
        await client.call_tool("quote_product", {"price_usd": 120})  # no weight: refused
        await client.call_tool("save_quote", SNEAKERS)
        await client.call_tool("save_quote", SNEAKERS)  # retry
        await client.list_tools()  # not a tool call: not audited

    assert [(r.tool, r.outcome) for r in sink.records] == [
        ("quote_product", "ok"),
        ("quote_product", "error"),
        ("save_quote", "ok"),
        ("save_quote", "replayed"),
    ]
    first = sink.records[0]
    assert (first.client_id, first.subject) == ("local", "local")
    assert first.arg_names == ["category", "price_usd"]
    assert len(first.args_sha256) == 64
    assert first.latency_ms >= 0
    assert first.expires_at > 0


@pytest.mark.anyio
async def test_audit_never_contains_argument_values(caplog: pytest.LogCaptureFixture) -> None:
    sink = InMemoryAuditSink()
    secretish = {**SNEAKERS, "product_name": "Very Distinctive Product Name 9137"}
    with caplog.at_level(logging.INFO, logger="cotizador.audit"):
        await call(
            build_server(store=InMemoryQuoteStore(), audit_sink=sink), "save_quote", secretish
        )
    assert "Very Distinctive Product Name 9137" not in repr(sink.records)
    assert "Very Distinctive Product Name 9137" not in caplog.text
    assert '"tool":"save_quote"' in caplog.text  # the log line is the fallback trail


@pytest.mark.anyio
async def test_a_crashing_tool_is_audited_as_crash() -> None:
    sink = InMemoryAuditSink()
    server = build_server(audit_sink=sink)

    @server.tool()
    def explode() -> int:
        raise RuntimeError("internal detail")

    result = await call(server, "explode", {})
    assert result.is_error and "internal detail" not in result.content[0].text
    assert [(r.tool, r.outcome) for r in sink.records] == [("explode", "crash")]


class FailingSink:
    def write(self, record: Any) -> None:
        raise ConnectionError("dynamodb unreachable")


@pytest.mark.anyio
async def test_a_failing_audit_sink_does_not_break_the_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.INFO, logger="cotizador.audit"):
        result = await call(
            build_server(audit_sink=FailingSink()),
            "quote_product",
            {"price_usd": 50, "weight_lb": 1},
        )
    assert not result.is_error
    assert "audit sink write failed" in caplog.text


# --- over HTTP, with tokens -------------------------------------------------


def http_call(client: TestClient, token: str, tool: str, arguments: dict[str, Any]) -> Any:
    return client.post(
        "/mcp",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2026-07-28",
            "Mcp-Method": "tools/call",
            "Mcp-Name": tool,
            "Authorization": f"Bearer {token}",
        },
        json={
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {"name": tool, "arguments": arguments, "_meta": ENVELOPE},
        },
    )


def test_read_only_token_cannot_save_and_the_refusal_is_audited() -> None:
    sink, store = InMemoryAuditSink(), InMemoryQuoteStore()
    app = http_app(CONFIG, token_verifier=verifier(), store=store, audit_sink=sink)
    read_only = make_token(scope=CONFIG.scope("quotes:read"))
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = http_call(client, read_only, "save_quote", SNEAKERS)

    result = rpc_result(response)
    assert response.status_code == 200 and result["isError"] is True
    assert "quotes:write" in result["content"][0]["text"]
    assert store.rows == {}
    record = sink.records[-1]
    assert (record.tool, record.outcome) == ("save_quote", "denied")
    assert (record.client_id, record.subject) == ("claude-code-client", "user-123")
    assert record.scopes == [CONFIG.scope("quotes:read")]
    assert record.rpc_id == "7"


def test_read_write_token_saves_under_its_own_subject() -> None:
    store = InMemoryQuoteStore()
    app = http_app(CONFIG, token_verifier=verifier(), store=store)
    with TestClient(app, base_url="http://localhost:8000") as client:
        response = http_call(client, make_token(), "save_quote", SNEAKERS)
        other_user = http_call(client, make_token(sub="user-456"), "save_quote", SNEAKERS)

    assert rpc_result(response)["isError"] is False
    assert rpc_result(other_user)["isError"] is False
    # Same key, two users: two records. Keys are scoped to the caller.
    assert sorted(owner for owner, _ in store.rows) == ["user-123", "user-456"]
    assert WRITE == "quotes:write"
