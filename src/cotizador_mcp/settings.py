"""Reference settings — deliberately NOT a real shop's configuration.

The pricing engine takes every rate as a parameter, so it has no opinion about
margins or tariffs. Something still has to supply them when an agent calls the
tool, and this module is that something.

Every value here is the published starting default from the web app's own
``lib/settings-defaults.ts`` — what a fresh install shows before anyone edits
it. No operator's real margin, tariff or commission appears in this repo, and
none should: those are a business's pricing, not a demo's.

That is also why the demo is honest about what it is. A quote computed from
these numbers is a correct computation of a reference configuration, not a
price anyone is offering. The tool's output says so in ``settings_source``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from cotizador_mcp.pricing import DEFAULT_ZONES, Zone

__all__ = [
    "CATEGORY_WEIGHTS_LB",
    "REFERENCE",
    "REFERENCE_FX_OFFICIAL",
    "ReferenceSettings",
]


@dataclass(frozen=True, slots=True)
class ReferenceSettings:
    """The parameters a quote needs, as fractions (0.25 is 25%)."""

    margin_pct: float
    carrier_rate_lb_usd: float
    sales_tax_pct: float
    fx_buffer_pct: float
    rounding_cop: float
    commission_pct: float
    zones: tuple[Zone, ...]


REFERENCE = ReferenceSettings(
    margin_pct=0.25,
    carrier_rate_lb_usd=6.0,
    sales_tax_pct=0.07,
    fx_buffer_pct=0.02,
    rounding_cop=5000,
    commission_pct=0.10,
    zones=DEFAULT_ZONES,
)

#: Typical shipping weight per product category, in pounds. Lets an agent quote
#: from a product page that states a price but not a weight — which is most of
#: them.
CATEGORY_WEIGHTS_LB: Mapping[str, float] = MappingProxyType(
    {
        "tenis": 2.6,
        "ropa": 1.1,
        "perfume": 1.1,
        "bolso": 2.2,
        "maquillaje": 0.7,
        "reloj": 1.1,
        "suplementos": 1.5,
    }
)

#: Stand-in COP-per-USD rate, used only when there is no official rate at all:
#: the server runs offline, or the official source is down and nothing is cached.
#: A quote carrying it is labelled ``fx_source="reference"`` so that neither an
#: agent nor a reader can mistake it for today's number.
REFERENCE_FX_OFFICIAL = 4000.0
