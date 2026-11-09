"""The DynamoDB stores, against moto's in-process fake. No AWS account is touched."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from mcp import Client
from moto import mock_aws

from cotizador_mcp.audit import AuditRecord
from cotizador_mcp.dynamo import DynamoAuditSink, DynamoQuoteStore, DynamoRateCache
from cotizador_mcp.fx import CachedRate
from cotizador_mcp.server import build_server
from cotizador_mcp.store import StoredQuote


@pytest.fixture
def dynamodb(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.setenv(name, "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-2")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    with mock_aws():
        yield boto3.resource("dynamodb", region_name="us-east-2")


def quotes_table(db: Any) -> Any:
    return db.create_table(
        TableName="quotes",
        KeySchema=[
            {"AttributeName": "owner", "KeyType": "HASH"},
            {"AttributeName": "idempotency_key", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "owner", "AttributeType": "S"},
            {"AttributeName": "idempotency_key", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def audit_table(db: Any) -> Any:
    return db.create_table(
        TableName="audit",
        KeySchema=[
            {"AttributeName": "client_id", "KeyType": "HASH"},
            {"AttributeName": "sk", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "client_id", "AttributeType": "S"},
            {"AttributeName": "sk", "AttributeType": "S"},
            {"AttributeName": "tool", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by_tool",
                "KeySchema": [
                    {"AttributeName": "tool", "KeyType": "HASH"},
                    {"AttributeName": "sk", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def quote(owner: str = "user-1", key: str = "key-00000001", qid: str = "q1") -> StoredQuote:
    return StoredQuote(
        owner=owner,
        idempotency_key=key,
        quote_id=qid,
        created_at="2026-11-08T23:00:00Z",
        fingerprint="f" * 64,
        product_name="Running shoe",
        product_url=None,
        quote_json='{"price_cop": 735000}',
    )


def test_put_if_absent_writes_once_and_returns_the_original(dynamodb: Any) -> None:
    store = DynamoQuoteStore(quotes_table(dynamodb))
    first, created = store.put_if_absent(quote(qid="q1"))
    again, created_again = store.put_if_absent(quote(qid="q2"))  # same owner+key
    assert (created, created_again) == (True, False)
    assert again == first  # q1, not q2
    other, created_other = store.put_if_absent(quote(owner="user-2", qid="q3"))
    assert created_other and other.quote_id == "q3"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_save_quote_twice_through_dynamodb_is_one_item(dynamodb: Any) -> None:
    table = quotes_table(dynamodb)
    server = build_server(store=DynamoQuoteStore(table))
    args = {
        "product_name": "Running shoe",
        "price_usd": 120,
        "category": "tenis",
        "idempotency_key": "4b1f0c4e-6a1e-4a43-9a51-1f1b2a7e0c11",
    }
    async with Client(server) as client:
        first = await client.call_tool("save_quote", args)
        second = await client.call_tool("save_quote", args)
    assert first.structured_content == second.structured_content
    assert table.scan()["Count"] == 1


def record(client_id: str, tool: str, ts: str, audit_id: str) -> AuditRecord:
    return AuditRecord(
        audit_id=audit_id,
        ts=ts,
        day=ts[:10],
        tool=tool,
        outcome="ok",
        latency_ms=12,
        client_id=client_id,
        subject="user-1",
        scopes=["http://localhost:8000/mcp/quotes:write"],
        arg_names=["price_usd"],
        args_sha256="a" * 64,
        expires_at=int(datetime(2027, 2, 6, tzinfo=UTC).timestamp()),
    )


def test_audit_query_all_save_quote_calls_by_a_client_today(dynamodb: Any) -> None:
    sink = DynamoAuditSink(audit_table(dynamodb))
    sink.write(record("claude-code", "save_quote", "2026-11-08T10:00:00.000Z", "a1"))
    sink.write(record("claude-code", "quote_product", "2026-11-08T10:01:00.000Z", "a2"))
    sink.write(record("claude-code", "save_quote", "2026-11-07T22:00:00.000Z", "a3"))  # yesterday
    sink.write(record("other-client", "save_quote", "2026-11-08T11:00:00.000Z", "a4"))

    found = sink.by_client_and_tool("claude-code", "save_quote", "2026-11-08")
    assert [item["audit_id"] for item in found] == ["a1"]


def test_rate_cache_round_trip_and_latest(dynamodb: Any) -> None:
    table = dynamodb.create_table(
        TableName="fx_rates",
        KeySchema=[{"AttributeName": "day", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "day", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    cache = DynamoRateCache(table)
    assert cache.latest() is None
    cache.put(CachedRate("2026-11-05", 3825.0, "2026-11-05"))
    cache.put(CachedRate("2026-11-08", 3237.83, "2026-11-07"))
    assert cache.get("2026-11-08") == CachedRate("2026-11-08", 3237.83, "2026-11-07")
    assert cache.get("2026-11-06") is None
    assert cache.latest() == CachedRate("2026-11-08", 3237.83, "2026-11-07")
