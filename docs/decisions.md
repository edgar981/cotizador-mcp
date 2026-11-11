# Decisions

Each entry is a question that could have sunk the project if answered in week 3
instead of week 1. Written as it was resolved, with the source that settled it.

---

## R1 — How do MCP clients register with the authorization server?

**Status:** decided and proved (2026-11-09).

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

**Proof required:** Claude Code → local server → Cognito, end to end. Until that
ran, this decision was research, not fact (proof below).

**What building it showed (2026-11-08).** Three Cognito facts shaped the server,
each found by probing the real pool rather than from documentation:

1. **Cognito's metadata omits `code_challenge_methods_supported`.** Its OIDC
   discovery document lists no PKCE methods (and only `client_secret_*` token auth),
   and it serves nothing at the RFC 8414 paths. The MCP spec says a client *must
   refuse* to proceed without that field. So the server publishes the
   authorization-server metadata itself at `/.well-known/oauth-authorization-server`:
   Cognito's own authorize/token/revoke endpoints, plus `S256` and `none`. Tokens are
   still issued and signed by Cognito and verified against Cognito's issuer and keys.
   This is the "thin shim" the brief anticipated, at the metadata level only: no
   tokens pass through it.
2. **Resource binding works, with a condition.** Cognito accepts the RFC 8707
   `resource` parameter (https, or `http://localhost`) and puts it in the access
   token's `aud`, so the server can refuse tokens minted for any other API. But it
   only binds a token to scopes of the resource server whose identifier *is* that
   resource: "custom scopes requested for resource-binding must be assigned to the
   resource being requested". So the resource-server identifier is the MCP URL, and
   scopes read `http://localhost:8000/mcp/quotes:read`. Each environment has its own
   resource server. The match is exact: a trailing slash fails.
3. **A scope the client is not allowed is an error, not dropped**
   (`invalid_scope`). Scopes advertised in Protected Resource Metadata must
   therefore be scopes every intended client is allowed to request.

Claude Code is registered as a **public client** (no secret, PKCE), which is the
right shape for a native app and means no client secret exists to leak.

**Proof (2026-11-09).** Registered with
`claude mcp add --transport http --client-id <id> --callback-port 8765`, Claude Code
took the 401 challenge to the Protected Resource Metadata, read the
authorization-server metadata the server publishes, opened Cognito's managed login,
and came back with a token the server accepted. That token passed the audience check,
so Claude Code sends the RFC 8707 `resource` parameter and Cognito bound the token to
the MCP URL. It then called `quote_product` and `save_quote`; both audit records name
the app client, the user's `sub`, and both scopes. A read-only token being refused by
`save_quote` is covered by tests, not yet by a live client.

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

- **Every request carries its own envelope.** With no `initialize`, the protocol
  version and the client's capabilities travel in each request's
  `params._meta` (`io.modelcontextprotocol/protocolVersion`,
  `io.modelcontextprotocol/clientCapabilities`). The headers alone are not
  enough: a request without the envelope is a `-32602`. Found by sending raw
  requests in the week-1 spike, not from the summary above.

**Consequence for the build:** stateless Streamable HTTP is the only transport
worth targeting for the remote deployment. The brief assumed otherwise in places.

**Verified locally (week 1):** a cold `tools/call` with no prior request
succeeds; `server/discover` advertises only `2026-07-28`; no response carries
`Mcp-Session-Id`; the SDK client forced to `mode="2026-07-28"` completes a
list-and-call in two requests.

---

## R2b — The SDK's API is not what the brief assumed

**Status:** found in the week-1 spike.

The brief specifies "official MCP Python SDK (`mcp`, FastMCP API)". In `mcp`
2.3.0 **there is no `mcp.server.fastmcp`.** The class is `MCPServer`. The
decorator style is the same, and `token_verifier` is a constructor argument,
so M7's auth approach survives unchanged — but any code written from memory of
FastMCP will not import.

The same SDK draws a line that matters for governance: a tool that raises
`ToolError` has its message relayed to the model (so the agent can fix its
call), while **any other exception is treated as a crash and its text never
leaves the server** — the client sees only `Error executing tool <name>`. Tools
here raise `ToolError` for anticipated refusals and nothing else, so internal
error text cannot leak through a tool result by accident.

---

## R3 — Bedrock model access

**Status:** form submitted; quota case open with AWS Support, 2026-11-08.

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

**Three gates, not one.** On a new account, access to Claude has three separate
gates, and each one shows a different error:

1. The use-case form (above): `ResourceNotFoundException` / `ValidationException`.
2. Account verification: `AccessDeniedException`, "Your account is currently
   being verified". Clears on its own, normally within 2 hours.
3. **Quotas.** The account's applied Claude quotas are **0** (tokens and
   requests per minute, for every Claude model checked: Sonnet 4.6, Haiku 4.5),
   while the AWS default is 6,000,000 tokens/min. The error is
   `ThrottlingException`, "Too many tokens per day", which reads like a usage
   limit even though no call has ever succeeded.

The third gate is not lifted the obvious way. Service Quotas rejects any request
below the default ("You must provide a quota value greater than the default
quota value"), so a modest increase cannot be requested there. The Support API
needs a paid support plan. What remains is a console support case, which the
Basic plan allows. Check the quota before debugging IAM:
`aws service-quotas list-service-quotas --service-code bedrock` and look at the
model's cross-region tokens-per-minute value.

**Update, later the same day: two Bedrock surfaces, and the model choice.** Claude
on Bedrock is served two ways, and they have separate model lists and quotas:

| | Legacy (`bedrock-runtime`) | Claude in Amazon Bedrock (`bedrock-mantle`) |
| --- | --- | --- |
| API | `Converse` / `InvokeModel`, ARN-versioned IDs | Anthropic Messages API at `/anthropic/v1/messages` |
| Models | Opus 4.6 and earlier (incl. Sonnet 4.6, the web app's model) | Opus 4.7 and later (`anthropic.claude-opus-5-5`, ...) |
| IAM | `bedrock:InvokeModel` on profile + model ARNs | `bedrock-mantle:CreateInference` on a *project* (`arn:aws:bedrock-mantle:<region>:<account>:project/default`) |
| Quota | per model, **0** on this account | default 2M input TPM |

`extract_product` targets the newer surface through Anthropic's own SDK
(`AnthropicBedrockMantle`), with structured output instead of the web app's JSON
scraping. Default model `anthropic.claude-opus-5-5`, effort `medium`; a refusal is
retried on `anthropic.claude-opus-4-8` by the SDK's client-side fallback middleware
(Bedrock has no server-side fallback). All three are environment settings.

Two things the probe found that the documentation does not say:

- **Region.** From this account, `us-east-2` lists only Haiku 4.5 on the newer
  surface; `us-east-1` lists Opus 5.5, Opus 5, Sonnet 5, Opus 4.8/4.7, Fable 5 and
  Haiku 4.5. So the stack stays in `us-east-2` (R5) and only the Bedrock client
  points at `us-east-1`. Data stays in the US.
- **Access.** Every Claude model on the newer surface answers 403 "not available for
  this account", including models documented as open to all Bedrock customers. Same
  new-account gate as the zero quota; the support case covers both.

**Checked again, 2026-11-10 20:22 (UTC-5): still refused.** One call, no retries, the
smallest that exercises the gate: `AnthropicBedrockMantle` in `us-east-1`,
`anthropic.claude-opus-5-5`, a one-line text message, `max_tokens=16`. Answer: HTTP 403,
`permission_error`, "anthropic.claude-opus-5-5 is not available for this account. You
can explore other available models on Amazon Bedrock. For additional access options,
contact AWS Sales". The same error as on 2026-11-08; the support case is untouched.

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

## R6 — Local AWS credentials

**Status:** decided, 2026-11-08.

**`aws login` as an IAM user, never root, and no access keys.** The CLI gets
short-lived credentials from a browser sign-in (they refresh every ~15 min, up
to a 12-hour session) through the `SignInLocalDevelopmentAccess` managed policy.
Nothing long-lived sits in `~/.aws`.

With GitHub Actions deploying through an OIDC role (M9), the project has **no
long-lived AWS credentials anywhere** — not on the developer machine, not in CI.

Gotcha worth writing down: if the browser already holds a root console session,
`aws login` silently authorises *that* session and the CLI profile ends up
pointing at `:root`. Sign out of the console first, then choose "IAM user".

---

## R7 — The budget lives outside the stack

**Status:** done, 2026-11-08. Departs from the brief (M6).

M6 lists the USD 5 budget alarm as part of the CDK stack. It is not, on purpose:
it was created by hand at the account level (`account-monthly-5-usd`) before
anything was deployed.

A budget's job is to watch **everything in the account**, including whatever
the stack forgets to tag and whatever survives a failed `cdk destroy`. A budget
owned by the stack is deleted together with it, and it does not exist yet during
the first `cdk deploy` — the riskiest moment. So the guard rail sits under the
stack, not inside it.

Four email alerts: actual spend over 20 %, 85 % and 100 %, and forecast over
100 %. The 20 % alert (USD 1) is not in AWS's template. This project should cost
cents a month, and the account has no free credits — the 85 % alert would only
fire at USD 4.25, long after something started going wrong. USD 1 is the alert
that means "look now".

---

## R8 — How it runs on AWS

**Status:** deployed 2026-11-09 (us-east-2), with Edgar's OK. Later deploys go through
CI and wait for his approval; the first one ran 2026-11-10.

**First deploy from CI, 2026-11-10.** The merge of pull request #1 started the deploy
workflow (run 42920815435), which waited at the `production` environment until Edgar
approved it and finished at 18:22 (UTC-5). The stack already matched the code, so only
`CDKMetadata` changed. Afterwards the URL answers 401 without a token and both
well-known routes answer 200.

**Sign-in against the deployed URL, 2026-11-10: works.** Claude Code 2.1.322, added
with the `ClaudeCodeClientId` and `McpUrl` outputs as in the README, opened the browser,
Edgar signed in through Cognito, and `get_exchange_rate` answered through API Gateway
and the Lambda. The bare 401 (below) did not get in the way: the client found the
metadata at the well-known URLs.

**First-deploy findings.**

- API Gateway's JWT authorizer answers 401 with a bare `WWW-Authenticate: Bearer`, as
  expected, so discovery relies on the public well-known routes. They answer from the
  Lambda: 3.2 s cold (2.5 s of it init), 0.4 s warm; 145 MB of 1024 MB used.
- **The Cognito domain is public, so it must not carry the account id.** The prefix
  first used the account id, which then appeared in the authorization-server
  metadata. It now uses the first block of the stack id's UUID. Changing it once
  deployed takes two steps: a user pool holds one prefix domain at a time, and
  CloudFormation creates a replacement before deleting the original, so the update
  fails (and rolls back) until the old domain is removed by hand. Cheap only before
  anyone signs in.


- **Lambda Web Adapter, not a Lambda-specific handler.** uvicorn serves the same
  Starlette app as `--transport http`; the adapter turns API Gateway events into HTTP.
  What runs in AWS is the code path the tests and local runs exercise. Layer pinned
  (1.1.0, arm64).
- **Packaging without Docker.** `uv pip install --python-platform aarch64-manylinux2014
  --only-binary :all:` builds the asset on any machine, CI included (75 MB unzipped).
- **The API URL is the OAuth resource.** API → Cognito resource server → app client →
  Lambda environment is a dependency chain with no cycle, because routes are added
  after the function. No custom domain is needed for the demo.
- **Two gates.** API Gateway's JWT authorizer (issuer, audience, read scope) rejects
  bad tokens before a Lambda runs; the server checks again, including per-tool
  scopes. **Known cost:** an HTTP API authorizer's 401 cannot carry the
  `resource_metadata` challenge the server sends. MCP clients must then fall back to
  the well-known metadata URLs, which are public routes here. Verified with Claude
  Code against the deployed URL (above).
- **Bedrock IAM is scoped to one project in one region, not to one model.** On the
  newer Bedrock surface the authorized resource is the project; restricting to a model
  needs a condition key not yet verified here, and a wrong key would silently deny
  every call. Narrower is a follow-up, not a guess.
- **Cost caps:** stage throttling (5 rps, burst 10). The account's Lambda concurrency
  limit is 10, so reserved concurrency cannot be used (it must leave 10 unreserved).
- **CI/CD without keys.** GitHub's OIDC provider; a *diff* role trusted from pull
  requests and a *deploy* role, each limited to assuming the CDK bootstrap roles.
  Actions are pinned to commit SHAs.
- **The deploy role trusts an environment, not a branch.** A job that names a GitHub
  environment presents the OIDC subject `repo:<repo>:environment:<name>`, never
  `ref:refs/heads/main`; trusting the branch would have refused every approved deploy.
  So the role trusts `environment:production`, and the environment admits only `main`
  and waits for the owner's approval (brief rule 4: every deploy is Edgar's call).
- **The roles trust the immutable subject, not the repository name.** This repository
  has GitHub's immutable subject claims, so its tokens say
  `repo:edgar981@77586877/cotizador-mcp@1451981537:…` (owner and repository ids), not
  `repo:edgar981/cotizador-mcp:…`. The first pull request's `cdk diff` was refused on
  exactly that. Trusting the ids is also the safer form: a renamed repository, or a new
  one under a reused name, presents different ids and cannot assume the roles. The
  subjects are matched with `StringEquals`, no wildcards.
- **Nothing personal in public logs.** Role ARNs (which carry the account id) and the
  alarm email are GitHub secrets, which Actions masks; the AWS action also masks the
  account id in everything printed after it.
- **Masking hides the exact secret, not what is derived from it.** `EmailSubscription`
  names its construct after the address, so the logical id carried a mangled copy of the
  alarm email, and the first pull request's `cdk diff` printed it in a public log. The
  log was deleted; the subscription now has a fixed id (`AlarmEmail`), a test keeps the
  address out of every logical id, and the diff job gets the email as a secret so it
  reports no change. The old logical id was replaced from a local deploy, so no public
  log ever shows it.

---

## Not used: Agent Toolkit for AWS

**Status:** considered and declined, 2026-11-08.

AWS suggests it in the console. It is AWS's toolkit for giving AI coding agents
access to AWS — MCP servers, skills and plugins pointing *into* AWS. This project
builds an MCP server over a pricing engine. The two are opposite directions
through the same protocol, and it is not a dependency of anything here.
