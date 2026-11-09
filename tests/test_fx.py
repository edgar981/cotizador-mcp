"""The exchange-rate source: same order as the web app (cache, API, last known)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from mcp import Client

from cotizador_mcp.fx import (
    CachedRate,
    InMemoryRateCache,
    RateSource,
    bogota_day,
    fetch_official_rate,
)
from cotizador_mcp.server import build_server

# 2026-11-09 03:00 UTC is still 2026-11-08 in Bogotá (UTC-5).
LATE_EVENING_IN_BOGOTA = datetime(2026, 11, 9, 3, 0, tzinfo=UTC)
API_ROW = [{"valor": "3842.17", "vigenciadesde": "2026-11-07T00:00:00.000"}]


class FakeHttp:
    def __init__(self, body: Any = API_ROW, fail: bool = False) -> None:
        self.body, self.fail, self.urls = body, fail, []

    def __call__(self, url: str, timeout: float) -> bytes:
        self.urls.append(url)
        if self.fail:
            raise TimeoutError("datos.gov.co did not answer")
        return json.dumps(self.body).encode()


def source(http: FakeHttp, cache: InMemoryRateCache | None = None) -> RateSource:
    return RateSource(cache or InMemoryRateCache(), http, clock=lambda: LATE_EVENING_IN_BOGOTA)


def test_the_day_is_bogotas() -> None:
    assert bogota_day(LATE_EVENING_IN_BOGOTA) == "2026-11-08"


def test_query_asks_for_the_rate_in_force_that_day() -> None:
    http = FakeHttp()
    assert fetch_official_rate("2026-11-08", http) == (3842.17, "2026-11-07")
    assert "vigenciadesde+%3C%3D+%272026-11-08T00%3A00%3A00.000%27" in http.urls[0]
    assert "%24order=vigenciadesde+DESC" in http.urls[0]
    assert "%24limit=1" in http.urls[0]


def test_fresh_rate_is_fetched_and_cached() -> None:
    http, cache = FakeHttp(), InMemoryRateCache()
    rate = source(http, cache).current()
    assert rate is not None
    assert (rate.value, rate.valid_from, rate.stale) == (3842.17, "2026-11-07", False)
    assert cache.get("2026-11-08") == CachedRate("2026-11-08", 3842.17, "2026-11-07")


def test_todays_cached_rate_skips_the_api() -> None:
    http, cache = FakeHttp(), InMemoryRateCache()
    cache.put(CachedRate("2026-11-08", 3900.0, "2026-11-08"))
    rate = source(http, cache).current()
    assert rate is not None and (rate.value, rate.stale) == (3900.0, False)
    assert http.urls == []


def test_api_down_falls_back_to_the_last_known_rate_and_says_so() -> None:
    cache = InMemoryRateCache()
    cache.put(CachedRate("2026-11-01", 3810.5, "2026-10-31"))
    cache.put(CachedRate("2026-11-05", 3825.0, "2026-11-05"))
    rate = source(FakeHttp(fail=True), cache).current()
    assert rate is not None
    assert (rate.value, rate.valid_from, rate.stale) == (3825.0, "2026-11-05", True)


@pytest.mark.parametrize(
    "body",
    [[], [{"valor": "abc", "vigenciadesde": "2026-11-07"}], [{"valor": "0"}], {"error": "x"}],
    ids=["empty", "non-numeric", "zero-and-no-date", "not-a-list"],
)
def test_unusable_api_answers_count_as_down(body: Any) -> None:
    assert source(FakeHttp(body=body)).current() is None


def test_no_api_and_no_cache_is_none() -> None:
    assert source(FakeHttp(fail=True)).current() is None


class BrokenCache(InMemoryRateCache):
    def get(self, day: str) -> CachedRate | None:
        raise ConnectionError("cache unreachable")

    def put(self, rate: CachedRate) -> None:
        raise ConnectionError("cache unreachable")


def test_a_broken_cache_does_not_stop_a_fresh_rate() -> None:
    rate = source(FakeHttp(), BrokenCache()).current()
    assert rate is not None and rate.value == 3842.17


# --- through the tools ------------------------------------------------------


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _call(server: Any, tool: str, args: dict[str, Any]) -> Any:
    async with Client(server) as client:
        return await client.call_tool(tool, args)


@pytest.mark.anyio
async def test_get_exchange_rate_reports_live_rate() -> None:
    result = await _call(build_server(rates=source(FakeHttp())), "get_exchange_rate", {})
    assert not result.is_error
    assert result.structured_content == {
        "rate_cop_per_usd": 3842.17,
        "valid_from": "2026-11-07",
        "source": "live",
    }


@pytest.mark.anyio
async def test_get_exchange_rate_without_any_rate_is_a_tool_error() -> None:
    result = await _call(build_server(rates=source(FakeHttp(fail=True))), "get_exchange_rate", {})
    assert result.is_error
    assert "unavailable" in result.content[0].text


@pytest.mark.anyio
async def test_quote_uses_the_live_rate_and_labels_it() -> None:
    result = await _call(
        build_server(rates=source(FakeHttp())),
        "quote_product",
        {"price_usd": 120, "category": "tenis"},
    )
    out = result.structured_content
    assert (out["fx_official"], out["fx_source"], out["fx_valid_from"]) == (
        3842.17,
        "live",
        "2026-11-07",
    )
    # 3842.17 x 1.02 = 3919.01 (JS-rounded); x 144 landed = 564,337.44 cost;
    # +25% = 705,421.80; up to the next 5,000 = 710,000.
    assert out["fx_applied"] == 3919.01
    assert out["price_cop"] == 710000


@pytest.mark.anyio
async def test_quote_on_a_stale_rate_says_last_known() -> None:
    cache = InMemoryRateCache()
    cache.put(CachedRate("2026-11-05", 3825.0, "2026-11-05"))
    result = await _call(
        build_server(rates=source(FakeHttp(fail=True), cache)),
        "quote_product",
        {"price_usd": 120, "category": "tenis"},
    )
    assert result.structured_content["fx_source"] == "last_known"
    assert result.structured_content["fx_valid_from"] == "2026-11-05"


@pytest.mark.anyio
async def test_quote_with_no_rate_at_all_falls_back_to_reference() -> None:
    result = await _call(
        build_server(rates=source(FakeHttp(fail=True))),
        "quote_product",
        {"price_usd": 120, "category": "tenis"},
    )
    assert result.structured_content["fx_source"] == "reference"
    assert result.structured_content["price_cop"] == 735000
