"""DynamoDB implementations of the three stores. The only module that imports boto3.

Table layouts (created by the CDK stack; the same names are used by the tests):

``quotes``    pk ``owner`` (S), sk ``idempotency_key`` (S).
              One item per save. The conditional put is the idempotency mechanism.
``audit``     pk ``client_id`` (S), sk ``sk`` = ``<ts>#<audit_id>`` (S);
              GSI ``by_tool``: pk ``tool``, sk ``sk``. TTL on ``expires_at``.
``fx_rates``  pk ``day`` (S). One item per Bogotá day.
"""

from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any

from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

from cotizador_mcp.audit import AuditRecord
from cotizador_mcp.fx import CachedRate
from cotizador_mcp.store import StoredQuote

__all__ = ["DynamoAuditSink", "DynamoQuoteStore", "DynamoRateCache"]


class DynamoQuoteStore:
    def __init__(self, table: Any) -> None:
        self._table = table

    def put_if_absent(self, quote: StoredQuote) -> tuple[StoredQuote, bool]:
        item = {k: v for k, v in asdict(quote).items() if v is not None}
        try:
            self._table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(#o)",
                ExpressionAttributeNames={"#o": "owner"},
            )
            return quote, True
        except ClientError as error:
            if error.response["Error"]["Code"] != "ConditionalCheckFailedException":
                raise
        existing = self._table.get_item(
            Key={"owner": quote.owner, "idempotency_key": quote.idempotency_key},
            ConsistentRead=True,
        )["Item"]
        return StoredQuote(**{f: existing.get(f) for f in StoredQuote.__dataclass_fields__}), False


class DynamoAuditSink:
    def __init__(self, table: Any) -> None:
        self._table = table

    def write(self, record: AuditRecord) -> None:
        item = {k: v for k, v in asdict(record).items() if v is not None and v != []}
        item["sk"] = f"{record.ts}#{record.audit_id}"
        self._table.put_item(Item=item)

    def by_client_and_tool(self, client_id: str, tool: str, day: str) -> list[dict[str, Any]]:
        """Every call of ``tool`` by ``client_id`` on ``day``: the README's example query."""
        response = self._table.query(
            KeyConditionExpression=Key("client_id").eq(client_id) & Key("sk").begins_with(day),
            FilterExpression=Attr("tool").eq(tool),
        )
        return response["Items"]


class DynamoRateCache:
    def __init__(self, table: Any) -> None:
        self._table = table

    def get(self, day: str) -> CachedRate | None:
        item = self._table.get_item(Key={"day": day}).get("Item")
        return _rate(item) if item else None

    def put(self, rate: CachedRate) -> None:
        self._table.put_item(
            Item={"day": rate.day, "value": Decimal(str(rate.value)), "valid_from": rate.valid_from}
        )

    def latest(self) -> CachedRate | None:
        # A few hundred rows a year at most; a scan is cheaper than a second index.
        items: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {}
        while True:
            page = self._table.scan(**kwargs)
            items.extend(page["Items"])
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        return _rate(max(items, key=lambda i: i["day"])) if items else None


def _rate(item: dict[str, Any]) -> CachedRate:
    return CachedRate(day=item["day"], value=float(item["value"]), valid_from=item["valid_from"])
