# cotizador-mcp

An MCP server that gives AI agents governed access to a real pricing engine.

The engine comes from a web app a personal shopper uses to price US retail products
for resale in Colombia. It has produced more than 1,000 real quotes. This repository
exposes that engine to agents. What it tries to get right is the governance around
the tools: typed schemas, read-only annotations, errors the model can act on,
scoped OAuth, an audit record per call, and idempotent writes. All of it runs on
AWS serverless, deployed with CDK.

> **Status: week 1 of 3.** The pricing engine and `quote_product` run locally over
> stdio and stateless HTTP. Everything marked *planned* below is not built yet.

## Status

| | Deliverable | State |
| --- | --- | --- |
| M1 | Scaffold: Python 3.12, uv, `mcp` SDK, pytest, ruff | Done locally; CI planned |
| M2 | Pricing engine ported from TypeScript, with parity tests | **Done** |
| M3 | Exchange-rate source (official COP/USD rate, cached daily) | Planned |
| M4 | Tools: `quote_product` · `get_exchange_rate` · `extract_product` · `save_quote` | `quote_product` **done**; rest planned |
| M5 | Local run over stdio and stateless Streamable HTTP | **Done** |
| M6 | AWS stack in CDK: Lambda, HTTP API, DynamoDB, least-privilege IAM | Planned |
| M7 | OAuth 2.1 with Cognito; scopes enforced per tool | Planned |
| M8 | Audit trail: one DynamoDB record per tool call | Planned |
| M9 | CI/CD with GitHub Actions and an OIDC deploy role | Planned |
| M10 | This README | In progress |

## Tools

| Tool | Kind | What it does | State |
| --- | --- | --- | --- |
| `quote_product` | read-only, deterministic | US price + category or weight → published COP price, full breakdown, optional domestic shipping estimate | **Available** |
| `get_exchange_rate` | read-only | Today's official COP/USD rate, with its validity date | Planned |
| `extract_product` | read-only | Product name, price and category from a screenshot (Claude on Bedrock, schema-enforced output) | Planned |
| `save_quote` | write, idempotent | Stores a quote; the same idempotency key returns the same record | Planned |

### `quote_product`

| Input | Required | Notes |
| --- | --- | --- |
| `price_usd` | yes | Must be greater than 0 |
| `category` | no | One of `tenis`, `ropa`, `perfume`, `bolso`, `maquillaje`, `reloj`, `suplementos`. Supplies a typical weight |
| `weight_lb` | no | Wins over the category's weight when both are given |
| `zone` | no | `Bogotá`, `Ciudades principales`, `Resto del país`, `Municipio`. Adds a domestic shipping estimate |

Either `weight_lb` or a known `category` is required. If neither is given, the tool
returns an error naming what is missing. The same happens for an unknown zone.

The computation runs in this order:

1. Add US sales tax and freight (per-pound tariff × weight) to get the landed cost in USD.
2. Convert at the official rate plus a buffer.
3. Add the margin.
4. Round up to the next 5,000 COP.

The commission is a share of the margin, so it never changes the price. The
domestic shipping estimate is returned separately and is never part of
`price_cop`.

Example: 120 USD sneakers, delivered in Bogotá. These are the real values from
MCP Inspector:

```json
{
  "price_cop": 735000,
  "landed_usd": 144,
  "fx_applied": 4080,
  "cost_cop": 587520,
  "margin_cop": 146880,
  "commission_cop": 14688,
  "net_margin_cop": 132192,
  "domestic_shipping_cop": 16000,
  "weight_source": "category",
  "fx_source": "reference",
  "settings_source": "reference"
}
```

`fx_source` and `settings_source` are there so the agent can tell the user what
kind of number it is. Every value here is a public reference value, not the
business's real configuration, and the exchange rate is a fixed stand-in until
M3 lands.

## Run it locally

You need [uv](https://docs.astral.sh/uv/). It installs the right Python by itself.

```bash
uv sync
uv run pytest
```

**stdio**, which is how a local client launches it:

```bash
uv run cotizador-mcp
```

**Stateless Streamable HTTP**, the same mode the Lambda will run in, served at
`http://127.0.0.1:8000/mcp`:

```bash
uv run cotizador-mcp --transport http --port 8000
```

**MCP Inspector** can list the tools and call one without an AI client:

```bash
npx -y @modelcontextprotocol/inspector@2.14.0 --cli uv run cotizador-mcp --method tools/list
```

```bash
npx -y @modelcontextprotocol/inspector@2.14.0 --cli uv run cotizador-mcp --method tools/call --tool-name quote_product --tool-arg price_usd=120 --tool-arg category=tenis
```

**Claude Code** can connect over stdio. Run this from the repository root:

```bash
claude mcp add cotizador -- uv run --directory "$PWD" cotizador-mcp
```

It can also connect to the HTTP server running locally:

```bash
claude mcp add --transport http cotizador http://127.0.0.1:8000/mcp
```

## Design notes

- **Stateless by protocol.** The server targets MCP revision 2026-07-28, which removed
  sessions. Every request carries its own protocol version and client capabilities,
  so any Lambda instance can answer any request. A `tools/call` works cold, with no
  handshake first.
- **Errors the model can act on.** Expected failures, such as a missing weight or
  an unknown zone, raise the SDK's `ToolError`, and the model sees the reason.
  Anything unexpected reaches the model only as a generic error, while the details
  stay in the server logs. Internals don't leak through tool results.
- **Parity with the original.** `tests/fixtures/parity.json` holds outputs produced by
  *running* the TypeScript engine at a pinned commit. The Python port must match every
  one, including JavaScript's rounding of halves (`Math.round`), which differs from
  Python's `round`.
- **No real configuration.** Margins, tariffs, tax, rate buffer, commission and category
  weights are the app's public reference values. The business's real settings are not
  in this repository.

Every decision, with its reasons and the gotchas found along the way, is in
[`docs/decisions.md`](docs/decisions.md).

## Architecture

*Planned (M6).* MCP client → API Gateway HTTP API (JWT authorizer) → Lambda running
the server in stateless HTTP mode → DynamoDB (`quotes`, `audit`) and Bedrock.
A diagram will go here.

## Auth flow

*Planned (M7).* Cognito is the authorization server and the MCP server is the
resource server. The server will validate the JWT, publish Protected Resource
Metadata (RFC 9728), and map scopes to tools: `quotes:read` for the read tools,
`quotes:write` for `save_quote`. Cognito does not support Dynamic Client
Registration, so clients are pre-registered. Decision R1 explains why.

## Audit trail

*Planned (M8).* One record per tool call: request id, caller, scopes, tool, a hash
of the arguments (never raw images), status, latency, and model tokens. A query
answering "every `save_quote` call by client X today" will go here.

## Threat model

*Planned.* Covers token theft and replay, scope escalation, prompt injection
through tool results and screenshots, SSRF if image URLs are accepted, and cost abuse.

## Cost

*Planned.* The target is cents per month at demo volume. An account-level budget of
USD 5 with an alert at USD 1 is already in place (decision R7).

## License

[MIT](LICENSE)
