"""Audit trail: one record per tool call, written by server middleware.

The record is built in :func:`audit_middleware`, which wraps every inbound MCP
message, so a tool cannot forget to be audited: a new tool is covered the moment
it is registered. A tool can only *add* to its record (model tokens, a refined
outcome) through :func:`note`.

What a record holds, and what it deliberately does not:

* who: the OAuth ``client_id`` and ``sub`` (``local`` on stdio), and the scopes;
* what: the tool name and the argument *names*; the arguments themselves only as a
  SHA-256 of their canonical JSON. No raw values, so no images and no product data.
* how it went: ``ok``, ``error`` (anticipated, reported to the model), ``denied``
  (missing scope), ``replayed`` (idempotent retry), ``crash`` (unexpected);
  latency; model tokens where a model was called.

Every record is also logged as one JSON line. If the durable sink fails, the
trail still exists in the logs, and the failure itself is logged.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from mcp.server.auth.middleware.auth_context import get_access_token

__all__ = [
    "AuditRecord",
    "AuditSink",
    "InMemoryAuditSink",
    "Outcome",
    "audit_middleware",
    "fingerprint",
    "note",
]

logger = logging.getLogger("cotizador.audit")

Outcome = Literal["ok", "error", "denied", "replayed", "crash"]
RETENTION_DAYS = 90


@dataclass(slots=True)
class AuditRecord:
    audit_id: str
    ts: str  # ISO-8601 UTC, millisecond precision; sorts lexically
    day: str  # YYYY-MM-DD (UTC)
    tool: str
    outcome: Outcome
    latency_ms: int
    client_id: str
    subject: str
    scopes: list[str]
    arg_names: list[str]
    args_sha256: str
    rpc_id: str | None = None
    http_request_id: str | None = None
    model_input_tokens: int | None = None
    model_output_tokens: int | None = None
    expires_at: int = 0  # epoch seconds; DynamoDB TTL deletes the record after this


class AuditSink(Protocol):
    def write(self, record: AuditRecord) -> None: ...


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[AuditRecord] = []

    def write(self, record: AuditRecord) -> None:
        self.records.append(record)


@dataclass(slots=True)
class _Note:
    outcome: Outcome | None = None
    extra: dict[str, Any] = field(default_factory=dict)


_current: contextvars.ContextVar[_Note | None] = contextvars.ContextVar("audit_note", default=None)


def note(*, outcome: Outcome | None = None, **extra: Any) -> None:
    """Let the running tool refine its own audit record. A no-op outside a tool call."""
    current = _current.get()
    if current is None:
        return
    if outcome is not None:
        current.outcome = outcome
    current.extra.update(extra)


def fingerprint(value: Any) -> str:
    """SHA-256 of canonical JSON: same arguments, same hash, regardless of key order."""
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def _crash_text(tool: str) -> str:
    # The SDK's fixed message for an unexpected exception, with nothing after it
    # (an anticipated ToolError reads "Error executing tool <name>: <reason>").
    # Pinned by a test, so an SDK change shows up as a failure, not a mislabel.
    return f"Error executing tool {tool}"


def _classify(tool: str, result: Any) -> Outcome:
    if not isinstance(result, dict) or not result.get("isError"):
        return "ok"
    content = result.get("content") or []
    first = content[0].get("text") if content and isinstance(content[0], dict) else None
    return "crash" if first == _crash_text(tool) else "error"


def audit_middleware(
    sink: AuditSink | None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> Callable[..., Any]:
    """Server middleware that audits ``tools/call`` and passes everything else through."""

    async def middleware(ctx: Any, call_next: Callable[[Any], Any]) -> Any:
        if ctx.method != "tools/call":
            return await call_next(ctx)

        params = ctx.params if isinstance(ctx.params, dict) else {}
        tool = str(params.get("name", ""))
        arguments = params.get("arguments") or {}
        started = time.perf_counter()
        current = _Note()
        reset = _current.set(current)
        outcome: Outcome = "crash"
        try:
            result = await call_next(ctx)
            outcome = current.outcome or _classify(tool, result)
            return result
        finally:
            _current.reset(reset)
            _emit(sink, _build(ctx, tool, arguments, outcome, started, current, clock()))

    return middleware


def _build(
    ctx: Any,
    tool: str,
    arguments: Any,
    outcome: Outcome,
    started: float,
    current: _Note,
    now: datetime,
) -> AuditRecord:
    token = get_access_token()
    headers = getattr(ctx, "headers", None) or {}
    return AuditRecord(
        audit_id=uuid.uuid4().hex,
        ts=now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        day=now.date().isoformat(),
        tool=tool,
        outcome=outcome,
        latency_ms=round((time.perf_counter() - started) * 1000),
        client_id=token.client_id if token else "local",
        subject=(token.subject or "unknown") if token else "local",
        scopes=sorted(token.scopes) if token else [],
        arg_names=sorted(arguments) if isinstance(arguments, dict) else [],
        args_sha256=fingerprint(arguments),
        rpc_id=None if ctx.request_id is None else str(ctx.request_id),
        http_request_id=headers.get("x-request-id") or headers.get("x-amzn-requestid"),
        model_input_tokens=current.extra.get("model_input_tokens"),
        model_output_tokens=current.extra.get("model_output_tokens"),
        expires_at=int(now.timestamp()) + RETENTION_DAYS * 86400,
    )


def _emit(sink: AuditSink | None, record: AuditRecord) -> None:
    logger.info(json.dumps({"audit": asdict(record)}, separators=(",", ":")))
    if sink is None:
        return
    try:
        sink.write(record)
    except Exception:
        logger.exception("audit sink write failed for %s", record.audit_id)
