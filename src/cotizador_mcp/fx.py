"""Official COP/USD exchange rate (TRM), ported from the web app's ``lib/trm.ts``.

Source: the TRM series that Colombia's financial regulator publishes on
datos.gov.co, dataset ``32sa-8pi3``. Behaviour matches the original:

1. Today's rate (Bogotá's calendar day) is served from the cache when present.
2. Otherwise the API is asked for the rate in force today, and the answer cached.
3. If the API fails, the last cached rate is returned, flagged ``stale`` and with
   the date it was valid from, so the caller can say how old it is.
4. With no API and no cache, the answer is ``None``. Nothing here raises.

The HTTP call and the cache are both injected. Tests use fakes; locally the cache is
in memory; in AWS it is a DynamoDB table.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol
from zoneinfo import ZoneInfo

__all__ = [
    "TRM_ENDPOINT",
    "CachedRate",
    "InMemoryRateCache",
    "Rate",
    "RateCache",
    "RateSource",
    "bogota_day",
    "fetch_official_rate",
]

TRM_ENDPOINT = "https://www.datos.gov.co/resource/32sa-8pi3.json"
TIMEOUT_S = 8.0
BOGOTA = ZoneInfo("America/Bogota")

HttpGet = Callable[[str, float], bytes]
"""GET ``url`` with a timeout in seconds; return the body, or raise on any failure."""


@dataclass(frozen=True, slots=True)
class CachedRate:
    day: str  # Bogotá day it was fetched for, "YYYY-MM-DD"
    value: float  # COP per USD
    valid_from: str  # the TRM's own validity date, "YYYY-MM-DD"


@dataclass(frozen=True, slots=True)
class Rate:
    value: float
    valid_from: str
    stale: bool  # True when the API failed and the last known value was used


class RateCache(Protocol):
    def get(self, day: str) -> CachedRate | None: ...
    def put(self, rate: CachedRate) -> None: ...
    def latest(self) -> CachedRate | None: ...


class InMemoryRateCache:
    """Process-local cache. Enough for stdio and local HTTP."""

    def __init__(self) -> None:
        self._rows: dict[str, CachedRate] = {}

    def get(self, day: str) -> CachedRate | None:
        return self._rows.get(day)

    def put(self, rate: CachedRate) -> None:
        self._rows[rate.day] = rate

    def latest(self) -> CachedRate | None:
        return self._rows[max(self._rows)] if self._rows else None


def bogota_day(now: datetime | None = None) -> str:
    """Today's date in Bogotá as ``YYYY-MM-DD``. The TRM changes on Bogotá's calendar."""
    now = now or datetime.now(UTC)
    return now.astimezone(BOGOTA).date().isoformat()


def _urllib_get(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def fetch_official_rate(day: str, http_get: HttpGet = _urllib_get) -> tuple[float, str] | None:
    """The TRM in force on ``day``: ``(value, valid_from)``, or ``None`` if the answer is unusable."""
    query = urllib.parse.urlencode(
        {
            "$where": f"vigenciadesde <= '{day}T00:00:00.000'",
            "$order": "vigenciadesde DESC",
            "$limit": "1",
        }
    )
    rows = json.loads(http_get(f"{TRM_ENDPOINT}?{query}", TIMEOUT_S))
    row = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(row, dict) or not row.get("vigenciadesde"):
        return None
    try:
        value = float(row.get("valor"))
    except (TypeError, ValueError):
        return None
    if not value > 0 or value == float("inf"):
        return None
    return value, str(row["vigenciadesde"])[:10]


class RateSource:
    """Today's TRM with the original's cache-then-API-then-last-known order."""

    def __init__(
        self,
        cache: RateCache,
        http_get: HttpGet = _urllib_get,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._cache = cache
        self._http_get = http_get
        self._clock = clock

    def current(self) -> Rate | None:
        day = bogota_day(self._clock())

        cached = _quietly(lambda: self._cache.get(day))
        if cached is not None:
            return Rate(cached.value, cached.valid_from, stale=False)

        fresh = _quietly(lambda: fetch_official_rate(day, self._http_get))
        if fresh is not None:
            value, valid_from = fresh
            _quietly(lambda: self._cache.put(CachedRate(day, value, valid_from)))
            return Rate(value, valid_from, stale=False)

        last = _quietly(self._cache.latest)
        if last is not None:
            return Rate(last.value, last.valid_from, stale=True)
        return None


def _quietly[T](action: Callable[[], T]) -> T | None:
    """Run ``action``; any failure reads as "no answer", like the original's ``.catch(() => null)``."""
    try:
        return action()
    except Exception:  # noqa: BLE001 (deliberate: an outage must degrade, not crash)
        return None
