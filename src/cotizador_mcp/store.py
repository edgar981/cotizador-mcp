"""Saved quotes, written at most once per (caller, idempotency key)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["InMemoryQuoteStore", "QuoteStore", "StoredQuote"]


@dataclass(frozen=True, slots=True)
class StoredQuote:
    owner: str  # the caller's OAuth subject; "local" on stdio
    idempotency_key: str
    quote_id: str
    created_at: str
    fingerprint: str  # SHA-256 of the business arguments the quote was computed from
    product_name: str
    product_url: str | None
    quote_json: str  # the QuoteResult exactly as first returned


class QuoteStore(Protocol):
    def put_if_absent(self, quote: StoredQuote) -> tuple[StoredQuote, bool]:
        """Store ``quote`` unless its (owner, key) exists. Returns (the stored one, created?)."""
        ...


class InMemoryQuoteStore:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], StoredQuote] = {}

    def put_if_absent(self, quote: StoredQuote) -> tuple[StoredQuote, bool]:
        key = (quote.owner, quote.idempotency_key)
        if key in self.rows:
            return self.rows[key], False
        self.rows[key] = quote
        return quote, True
