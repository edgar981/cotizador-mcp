# Decisions

Each entry is a question that could have sunk the project if answered in week 3
instead of week 1. Written as it was resolved, with the source that settled it.

---

## R1 — How do MCP clients register with the authorization server?

**Status:** decided, proof pending.

The MCP authorization spec expects a client to obtain credentials in this order:

1. credentials it was pre-registered with,
2. a Client ID Metadata Document (`client_id_metadata_document_supported`),
3. Dynamic Client Registration (`registration_endpoint`),
4. ask the user.

**Amazon Cognito supports neither 2 nor 3.** Its user pools have no
`registration_endpoint` and no CIMD support, so a client that only knows how to
self-register cannot connect to a Cognito-backed server at all.

**Decision: pre-registered clients (option 1).** Claude Code accepts
`--client-id` / `--client-secret` / `--callback-port`, which is exactly option 1.
This is also the honest posture for the threat model: an MCP server whose tools
can write should not let arbitrary clients register themselves.

**Cost:** browser-based connectors that can only self-register (claude.ai
connectors, some third-party clients) will not connect without an operator
creating an app client for them first. The README says so rather than hiding it.

**Still to prove:** Claude Code → local server → Cognito, end to end, before any
CDK is written. Until that runs, this decision is research, not fact.

---

## R2 — Which revision of the MCP spec do we target?

**Status:** decided.

**Revision `2026-07-28`**, and the SDK pinned to a version that implements it.

What changed from the revision most tutorials and blog posts still describe:

- **No protocol-level sessions.** `Mcp-Session-Id` is gone; requests are
  self-contained. This is what makes a Lambda deployment natural rather than a
  fight — there is no session state to keep alive between invocations.
- **No `initialize` / `notifications/initialized` handshake.**
- **`server/discover` is a MUST.**
- **Required headers:** `MCP-Protocol-Version`, `Mcp-Method`, and `Mcp-Name` on
  `tools/call`.
- **No SSE resumability.**
- **DCR is deprecated** in favour of Client ID Metadata Documents — which is the
  second half of why R1 lands on pre-registered clients: the mechanism Cognito
  lacks is also the one the spec now prefers.

**Consequence for the build:** stateless Streamable HTTP is the only transport
worth targeting for the remote deployment. The brief assumed otherwise in places.

---

## R2b — The SDK's API is not what the brief assumed

**Status:** found in the week-1 spike.

The brief specifies "official MCP Python SDK (`mcp`, FastMCP API)". In `mcp`
2.3.0 **there is no `mcp.server.fastmcp`.** The class is `MCPServer`. The
decorator style is the same, and `token_verifier` is a constructor argument,
so M7's auth approach survives unchanged — but any code written from memory of
FastMCP will not import.

---

## R3 — Bedrock model access

**Status:** action with Edgar, 2026-11-08.

Bedrock now enables serverless foundation models automatically on first invoke,
**but Anthropic models still require the first-time-use form.** Without it, calls
fail with `ResourceNotFoundException` or a `ValidationException` saying the
operation is not allowed.

Where it is: Bedrock console → **Model catalog** → an Anthropic model → submit
the use-case details. (Equivalently, the `PutUseCaseForModelAccess` API.) Access
is granted immediately on successful submission; propagation can take ~15 min.

Bedrock **API keys**, offered prominently on the console Quickstart page, are
not used here. AWS documents them as being for exploration and development. The
Lambda authenticates to Bedrock with its execution role.

---

## R4 — AWS account plan

**Status:** action with Edgar, 2026-11-08.

New accounts start on the **Free plan**: credits for six months, and the account
is closed rather than billed when they run out. A demo that an interviewer can
open months from now cannot live on an account that closes itself. Upgrade to
the Paid plan, with a **USD 5 budget alarm** (M6) as the actual spend control.

---

## R5 — Region

**Status:** decided, 2026-11-08.

**`us-east-2` (Ohio).**

Not for any property of the region — because it is already the account's default,
and a project that fights its own account default spends week 2 debugging empty
console pages. Claude models are reachable there through the `us.` cross-region
inference profile.

**Known consequence for M6.** The brief asks for Bedrock invoke permission
"scoped to the one model ARN". With a cross-region inference profile that is not
one ARN: the policy needs the inference-profile ARN *and* the underlying
foundation-model ARNs in every region the profile can route to. Least privilege
here means an explicit list, not a single resource. Verify against the actual
profile before writing the policy.

The region is a single constant in the CDK app, so this is cheap to revisit.

---

## Not used: Agent Toolkit for AWS

**Status:** considered and declined, 2026-11-08.

AWS suggests it in the console. It is AWS's toolkit for giving AI coding agents
access to AWS — MCP servers, skills and plugins pointing *into* AWS. This project
builds an MCP server over a pricing engine. The two are opposite directions
through the same protocol, and it is not a dependency of anything here.
