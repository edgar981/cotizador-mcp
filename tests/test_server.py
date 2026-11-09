"""The quote_product tool, exercised through the MCP layer.

These drive the server in-process with the SDK's own ``Client``, so schema
validation, error relaying and structured output are all in the loop — the
same path a real agent's call takes, minus the transport.
"""

from __future__ import annotations

import pytest
from mcp import Client

from cotizador_mcp.pricing import QuoteParams, quote
from cotizador_mcp.server import build_server
from cotizador_mcp.settings import CATEGORY_WEIGHTS_LB, REFERENCE, REFERENCE_FX_OFFICIAL


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _call(arguments: dict):
    async with Client(build_server()) as client:
        return await client.call_tool("quote_product", arguments)


def _expected_price(price_usd: float, weight_lb: float) -> float:
    """The engine's answer for the reference configuration, computed directly."""
    return quote(
        QuoteParams(
            price_usd=price_usd,
            weight_lb=weight_lb,
            sales_tax_pct=REFERENCE.sales_tax_pct,
            carrier_rate_lb_usd=REFERENCE.carrier_rate_lb_usd,
            fx_official=REFERENCE_FX_OFFICIAL,
            fx_buffer_pct=REFERENCE.fx_buffer_pct,
            margin_pct=REFERENCE.margin_pct,
            rounding_cop=REFERENCE.rounding_cop,
        )
    ).price_cop


# --- the contract an agent sees -------------------------------------------


@pytest.mark.anyio
async def test_tool_is_advertised_as_read_only_with_schemas():
    async with Client(build_server()) as client:
        tools = (await client.list_tools()).tools

    assert [t.name for t in tools] == ["quote_product"]
    tool = tools[0]
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False
    assert tool.input_schema["required"] == ["price_usd"]
    assert tool.output_schema is not None


# --- answers ----------------------------------------------------------------


@pytest.mark.anyio
async def test_known_quote_checked_by_hand():
    # 120 USD sneakers, 2.6 lb: tax 8.40, freight 15.60, landed 144.00;
    # x 4,080 = 587,520 cost; +25% = 734,400; up to the next 5,000 = 735,000.
    result = await _call({"price_usd": 120, "category": "tenis"})

    assert not result.is_error
    out = result.structured_content
    assert out["price_cop"] == 735000
    assert out["landed_usd"] == 144.0
    assert out["weight_source"] == "category"


@pytest.mark.anyio
@pytest.mark.parametrize("category", sorted(CATEGORY_WEIGHTS_LB))
async def test_every_category_matches_the_engine(category):
    result = await _call({"price_usd": 59.99, "category": category})

    assert not result.is_error
    expected = _expected_price(59.99, CATEGORY_WEIGHTS_LB[category])
    assert result.structured_content["price_cop"] == expected


@pytest.mark.anyio
async def test_category_is_case_and_space_insensitive():
    result = await _call({"price_usd": 120, "category": "  Tenis "})

    assert result.structured_content["price_cop"] == 735000


@pytest.mark.anyio
async def test_explicit_weight_wins_over_category():
    result = await _call({"price_usd": 120, "category": "tenis", "weight_lb": 1.0})

    out = result.structured_content
    assert out["weight_lb"] == 1.0
    assert out["weight_source"] == "given"
    assert out["price_cop"] == _expected_price(120, 1.0)


@pytest.mark.anyio
async def test_commission_comes_out_of_the_margin_not_on_top():
    out = (await _call({"price_usd": 120, "category": "tenis"})).structured_content

    assert out["commission_cop"] + out["net_margin_cop"] == out["margin_cop"]
    assert out["commission_cop"] == pytest.approx(out["margin_cop"] * REFERENCE.commission_pct)


# --- the domestic leg is quoted, never folded in ---------------------------


@pytest.mark.anyio
async def test_zone_adds_a_separate_estimate_and_leaves_the_price_alone():
    without = (await _call({"price_usd": 120, "category": "tenis"})).structured_content
    with_zone = (
        await _call({"price_usd": 120, "category": "tenis", "zone": "Bogotá"})
    ).structured_content

    assert without["domestic_shipping_cop"] is None
    assert with_zone["domestic_shipping_cop"] == 16000
    assert with_zone["zone"] == "Bogotá"
    assert with_zone["price_cop"] == without["price_cop"]


# --- honesty about where the numbers come from ------------------------------


@pytest.mark.anyio
async def test_quote_declares_reference_rate_and_settings():
    out = (await _call({"price_usd": 120, "category": "tenis"})).structured_content

    assert out["fx_source"] == "reference"
    assert out["settings_source"] == "reference"


# --- refusals reach the model with a reason ---------------------------------


@pytest.mark.anyio
async def test_no_weight_and_unknown_category_is_refused_with_the_fix():
    result = await _call({"price_usd": 50, "category": "lamp"})

    assert result.is_error
    text = result.content[0].text
    assert "weight_lb" in text
    assert "tenis" in text  # tells the model which categories would work


@pytest.mark.anyio
async def test_unknown_zone_is_refused_with_the_known_zones():
    result = await _call({"price_usd": 50, "weight_lb": 1, "zone": "Marte"})

    assert result.is_error
    assert "Bogotá" in result.content[0].text


@pytest.mark.anyio
@pytest.mark.parametrize("bad", [0, -10])
async def test_non_positive_price_fails_schema_validation(bad):
    result = await _call({"price_usd": bad, "weight_lb": 1})

    assert result.is_error
