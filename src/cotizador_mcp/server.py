"""The MCP server: tools over the pricing engine.

Two transports, one server object:

* **stdio** — how a local client (Claude Code, MCP Inspector) launches it.
* **stateless Streamable HTTP** — how it runs remotely.

``stateless_http=True`` is not a tuning knob here, it is the whole deployment
story. MCP revision 2026-07-28 removed protocol-level sessions, so each request
carries everything it needs. That is what lets this run on Lambda at all: there
is no session to keep warm between invocations, and two consecutive calls may
legitimately land on different instances.
"""

from __future__ import annotations

import argparse
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from cotizador_mcp.pricing import (
    QuoteParams,
    commission_split,
    domestic_shipping,
    find_zone,
    quote,
)
from cotizador_mcp.settings import (
    CATEGORY_WEIGHTS_LB,
    REFERENCE,
    REFERENCE_FX_OFFICIAL,
)

__all__ = ["build_server", "main"]


class QuoteResult(BaseModel):
    """A full quote. Every intermediate value is returned, not just the price.

    An agent that only gets a number cannot explain it, and the operator this
    was built for is asked "why that much?" constantly.
    """

    price_cop: float = Field(description="Published price in COP, after rounding.")
    subtotal_cop: float = Field(description="Cost plus margin, before rounding.")
    cost_cop: float = Field(description="Landed cost in COP.")
    margin_cop: float = Field(description="Margin in COP, included in the price.")

    tax_usd: float = Field(description="Estimated US sales tax.")
    freight_usd: float = Field(description="Mail-forwarder freight to Colombia.")
    landed_usd: float = Field(description="Price plus tax plus freight, in USD.")

    weight_lb: float = Field(description="Weight used, in pounds.")
    weight_source: Literal["given", "category"] = Field(
        description="Whether the weight was supplied or inferred from the category."
    )

    fx_official: float = Field(description="Official COP-per-USD rate used.")
    fx_applied: float = Field(description="Rate after the buffer, used to convert.")
    fx_source: Literal["reference", "live"] = Field(
        description=(
            "'reference' means a stand-in rate, NOT today's rate: the quote is "
            "arithmetically correct but not a real offer. Say so when relaying it."
        )
    )

    commission_cop: float = Field(description="Operator commission, taken from the margin.")
    net_margin_cop: float = Field(description="Margin left after commission.")

    domestic_shipping_cop: float | None = Field(
        default=None,
        description=(
            "Bogota-to-destination leg, quoted separately. Null when no zone was "
            "given. This is NEVER part of price_cop."
        ),
    )
    zone: str | None = Field(default=None, description="Zone the domestic leg used.")

    settings_source: Literal["reference"] = Field(
        default="reference",
        description=(
            "Margin, tariff, tax and commission come from published reference "
            "defaults, not from any real shop's configuration."
        ),
    )


def build_server() -> MCPServer:
    """Construct the server. Separate from :func:`main` so tests can drive it."""
    server = MCPServer(
        name="cotizador",
        title="Cotizador",
        version="0.1.0",
        instructions=(
            "Prices US retail products for import into Colombia. Quotes are "
            "deterministic arithmetic over a reference configuration; they are "
            "not offers. Relay the price together with its fx_source."
        ),
    )

    @server.tool(
        annotations=ToolAnnotations(
            read_only_hint=True,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
    def quote_product(
        price_usd: Annotated[
            float, Field(gt=0, description="US list price of the product, in USD.")
        ],
        category: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Product category, used to infer a shipping weight when "
                    f"weight_lb is omitted. Known: {', '.join(CATEGORY_WEIGHTS_LB)}."
                ),
            ),
        ] = None,
        weight_lb: Annotated[
            float | None,
            Field(default=None, gt=0, description="Shipping weight in pounds."),
        ] = None,
        zone: Annotated[
            str | None,
            Field(
                default=None,
                description=(
                    "Destination zone for a separate domestic-shipping estimate. "
                    f"Known: {', '.join(z.name for z in REFERENCE.zones)}."
                ),
            ),
        ] = None,
    ) -> QuoteResult:
        """Quote the delivered COP price of a US product.

        Supply either weight_lb or a known category. The domestic leg is
        returned separately and is never folded into the published price.
        """
        if weight_lb is not None:
            resolved_weight, weight_source = float(weight_lb), "given"
        elif category and category.strip().lower() in CATEGORY_WEIGHTS_LB:
            resolved_weight = CATEGORY_WEIGHTS_LB[category.strip().lower()]
            weight_source = "category"
        else:
            # A guessed weight would silently move the price, so refuse instead.
            # ToolError, not ValueError: the SDK relays its message to the model
            # so it can fix the call. Any other exception counts as a crash, and
            # its text never leaves the server.
            known = ", ".join(CATEGORY_WEIGHTS_LB)
            raise ToolError(
                f"Need weight_lb, or a category among: {known}. Got category={category!r}."
            )

        breakdown = quote(
            QuoteParams(
                price_usd=price_usd,
                weight_lb=resolved_weight,
                sales_tax_pct=REFERENCE.sales_tax_pct,
                carrier_rate_lb_usd=REFERENCE.carrier_rate_lb_usd,
                fx_official=REFERENCE_FX_OFFICIAL,
                fx_buffer_pct=REFERENCE.fx_buffer_pct,
                margin_pct=REFERENCE.margin_pct,
                rounding_cop=REFERENCE.rounding_cop,
            )
        )
        split = commission_split(breakdown.margin_cop, REFERENCE.commission_pct)

        domestic_cop: float | None = None
        resolved_zone: str | None = None
        if zone:
            found = find_zone(REFERENCE.zones, zone)
            if found is None:
                names = ", ".join(z.name for z in REFERENCE.zones)
                raise ToolError(f"Unknown zone {zone!r}. Known: {names}.")
            domestic_cop = domestic_shipping(found, resolved_weight)
            resolved_zone = found.name

        return QuoteResult(
            price_cop=breakdown.price_cop,
            subtotal_cop=breakdown.subtotal_cop,
            cost_cop=breakdown.cost_cop,
            margin_cop=breakdown.margin_cop,
            tax_usd=breakdown.tax_usd,
            freight_usd=breakdown.freight_usd,
            landed_usd=breakdown.landed_usd,
            weight_lb=resolved_weight,
            weight_source=weight_source,
            fx_official=REFERENCE_FX_OFFICIAL,
            fx_applied=breakdown.fx_applied,
            fx_source="reference",
            commission_cop=split.commission_cop,
            net_margin_cop=split.net_margin_cop,
            domestic_shipping_cop=domestic_cop,
            zone=resolved_zone,
        )

    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the cotizador MCP server.")
    parser.add_argument("--transport", choices=["stdio", "http"], default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    server = build_server()
    if args.transport == "stdio":
        server.run("stdio")
    else:
        server.run(
            "streamable-http",
            host=args.host,
            port=args.port,
            # See the module docstring: stateless is the deployment story.
            stateless_http=True,
        )


if __name__ == "__main__":
    main()
