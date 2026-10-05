# Microsoft Teams interactive approval — approve / reject from the Teams card

When an invoice is **assigned for review** and the org's chat provider is Teams,
the approval card posted to the channel can carry **Approve / Reject actions**.
An approver clicks an action and the decision runs through the **exact same**
`services.review` path the in-app buttons use — no app login, no JWT. This is the
Teams counterpart of the already-shipped Slack interactive approval and closes
the "Slack/Teams approval" gap under **Advanced Approval Routing** in
`docs/roadmap_shipped.md`.

It builds directly on the same two pieces as Slack and adds nothing weaker:

- the **outbound Teams adapter**
  (`services/chat_notification_adapters/teams_adapter.py`) — posts the approval
  MessageCard / card to the channel; and
- the **email-approval action token** (`services/email_action_token.py`) — reused
  as the credential, bound to a new `teams` channel so the surfaces' tokens are
  not interchangeable.

## The button credential — the action token, bound to a channel

Each action's payload carries a signed, single-use **action token** — the same
primitive the email-approval link and the Slack buttons use. It binds, under an
HMAC-SHA256 signature the platform alone can produce, the exact facts the action
will run against:

```
tenant_slug + invoice_id + actor_id (the intended approver) + action + channel + expiry + jti
```

The `channel` claim (`email` / `slack` / `teams`) is what makes the surfaces
distinct:

- `build_teams_action_tokens(...)` mints `channel="teams"` tokens for the card;
- the Teams endpoint calls `verify_action_token(..., expected_channel="teams")`;
- the Slack endpoint uses `"slack"`; the email endpoint keeps the default
  `"email"`.

So a Teams token presented to the email-confirm or Slack endpoint — or a Slack /
email token fed into the Teams endpoint — fails verification. A token built with
no channel (the original email callers) defaults to `email`, so the email-approval
flow is unchanged.

The token is minted **per intended approver** (same no-privilege-escalation
property as the per-recipient email link). Tokens are only added when
**`FEOH_EMAIL_ACTION_SIGNING_KEY` is set and the chat provider is `teams`**;
otherwise the card stays a plain (non-interactive) post.

`notification_dispatch._build_chat_action_tokens` is the one place that decides
this. It dispatches on the org's chat provider — `slack` →
`build_slack_action_tokens`, `teams` → `build_teams_action_tokens`, anything else
(`mock`, an unknown key) → no tokens at all — so each surface only ever receives
tokens minted on its **own** channel and the two are never interchangeable.

## The outbound card — Approve / Reject actions

`services/chat_notification_adapters/teams_adapter.py` renders the
`invoice_assigned` MessageCard with two `HttpPOST` actions after the existing
`OpenUri` deep link:

```json
{
  "@type": "HttpPOST",
  "name": "Approve",
  "target": "https://<api-host>/api/approvals/teams/interactivity",
  "bodyContentType": "application/json",
  "body": "{\"type\":\"message\",\"value\":{\"token\":\"<action token>\"}}",
  "headers": [
    { "name": "Content-Type", "value": "application/json" },
    { "name": "Authorization", "value": "HMAC <base64 digest>" },
    { "name": "X-Feoh-Card-Signature", "value": "<base64 digest>" }
  ]
}
```

The `body` shape is exactly what `_extract_token` reads (`value.token`), so the
card and the endpoint agree by construction.

### Why the card signs itself

A MessageCard `HttpPOST` action is dispatched **by Microsoft, not by us**, so
there is no shared-secret handshake to ride on the way a Teams *Outgoing Webhook*
has one. What we do control is the action's exact `body` string and its `headers`
— so the card is stamped, at render time, with
`HMAC-SHA256(security token, body)` over that exact string. The endpoint
re-derives the same digest with the same primitive.

What that digest is and isn't:

- it **proves the POST replays a body the platform minted** — it can't be
  produced without the security token;
- it is **not a key**, and it is **body-bound**: it cannot sign a different body,
  so an approver handed the Reject action can't upgrade it to Approve (the test
  suite pins exactly this);
- anyone who could extract it could only **re-fire that one action**, which the
  single-use `jti` already collapses to a no-op. That is the same exposure the
  Slack buttons have — any channel member can click — and authorization still
  rides entirely on the signed, per-approver action token inside the body.

**Two headers, one digest.** Teams may replace an actionable message's
`Authorization` header with its own bearer token (it attaches the acting user's
identity to the POST), which would strip the only proof we have. The card
therefore carries the digest on the dedicated `X-Feoh-Card-Signature` header as
well; the endpoint prefers `Authorization: HMAC …` (the genuine Outgoing-Webhook
spelling, unchanged) and falls through to the card header when `Authorization`
holds something else. Same secret, same digest, same constant-time compare.

**No timestamp header.** The card deliberately does not emit
`X-Teams-Request-Timestamp`: it is stamped at *render* time, so the ±5-minute
window would kill the buttons five minutes after the card is posted. Replay is
bounded by the single-use `jti` and the workflow state machine instead — which
is the posture the endpoint already documents for a timestamp-less POST.

### Fail-closed rungs (each independent)

The card falls back to a plain read-only post — never a broken button — unless
**all** of these hold. A button whose POST the endpoint is guaranteed to reject
is worse than no button: the approver clicks it and is told nothing happened.

| Missing | Result |
|---|---|
| `FEOH_EMAIL_ACTION_SIGNING_KEY` | no tokens minted → no actions |
| chat provider isn't `slack`/`teams` | no tokens minted → no actions |
| event isn't `invoice_assigned` | no tokens minted → no actions |
| zero or several intended approvers | no tokens minted → no actions |
| `FEOH_TEAMS_SECURITY_TOKEN` | nothing to sign with → no actions |
| `FEOH_API_PUBLIC_URL` | no callback target → no actions |

## The inbound webhook

```
POST /api/approvals/teams/interactivity   — PUBLIC, no JWT
```

`app/api/teams_approvals.py`. Public-by-design (Teams POSTs it; the signature +
the token are the gates) and listed in `ALTERNATE_AUTH` in `tests/test_rbac.py`,
exactly like the PEPPOL-inbound, email-approval, and Slack routes.

Two gates, layered, both fail closed:

1. **Teams request signature.** `base64(hmac-sha256(base64decode(security token),
   raw_body))`, compared with a constant-time `hmac.compare_digest`. It arrives on
   `Authorization: HMAC <digest>` — how a Teams **Outgoing Webhook** signs every
   POST — or on `X-Feoh-Card-Signature`, which is how the approval card's own
   `HttpPOST` action carries it (see § The outbound card). Both spellings are
   verified by the shared `services/teams_signature.py` primitive, which is the
   same function that *signs* the outbound card, so the two ends of the round-trip
   cannot drift apart. A security token that is empty — or decodes to nothing —
   fails closed in both directions. When Teams
   includes an `X-Teams-Request-Timestamp` header we additionally **reject a
   timestamp more than `FEOH_TEAMS_REQUEST_MAX_AGE_SECONDS` (default 300s) from
   now** so a captured POST can't be replayed; the header is optional (Teams does
   not always send it), and when absent the single-use `jti` + the workflow state
   machine still bound replay. No secret configured → the feature is OFF and every
   request is rejected (no hardcoded fallback).
2. **Action token.** Parse the JSON Activity envelope, pull the token from the
   action payload (`value.token`, or — for configs that echo it — the Activity
   `text`), and `verify_action_token(..., expected_channel="teams")` (HMAC +
   expiry + channel). Then load the named reviewer (active, right org, holds the
   `invoice.approve` granular permission — the shared `email_actions.may_approve`
   gate, identical to the in-app `require_permission(PERM_INVOICE_APPROVE)`, so a
   custom role granting it works here too), **claim the card's button pair**
   in Redis (`ActionToken.consume_key` — single-use per card, see
   `email-approval.md` § Single-use), and call `review.approve_invoice` / `review.reject_invoice` **as
   the reviewer**. Segregation of duties, the approval thresholds, the CFO gate,
   the `invoice.approved`/`invoice.rejected` immutable audit row, and the approval
   digital signature all apply exactly as if they had logged in — and the org's
   `settings` ride along as `org_settings`, so the tenant's own `fraud_rules` /
   `matching` PO tolerances / `exceptions` routing / structuring window govern
   here too, never the platform defaults. On a **multi-level chain** the invoice
   stays `ready_for_review` for the next approver, and the ack says the approval
   was *recorded*, not that the invoice is approved.

If the action turns out not applicable / not permitted (invoice no longer
`ready_for_review`, segregation block, CFO gate, over the cap), the `jti` claim is
**released** so the reviewer can still act in-app.

### Opaque responses — no enumeration

Every path — success **and** every rejection (bad signature, missing
`Authorization`, stale timestamp, expired / replayed / wrong-channel token,
unknown tenant / invoice, non-approver, feature off) — returns the **same opaque
`200` message Activity** (`{"type": "message", "text": "..."}`). A 4xx, or a
distinct message per failure, would let a probe enumerate tenants, invoices, or
which secret/token shapes are accepted. The handler is fully self-guarded — it
never raises a 500 on the public route — and logs only a PII-free reason code
(never the token, the invoice fields, the webhook URL, or any banking/PII).

### Single-use, layered (replay protection)

- **Teams timestamp window** — when the timestamp header is present, a captured
  POST replayed > 5 min later is rejected before any work.
- **Workflow state machine** — approve/reject move the invoice out of
  `ready_for_review`, so the same decision can't re-fire.
- **Redis `jti` consume** — `SET NX EX` on the token id closes the
  reject→resubmit replay window. The token IS the dedupe — a re-clicked action
  can't double-act. This is the primary guard when Teams omits the timestamp.

## What's PII-free

Nothing sensitive enters the Teams card or the logs. The outbound card carries
only invoice number, vendor name, amount + currency, the status word, and a deep
link (the same fields the outbound chat adapter already allowed) plus the opaque
HMAC token(s). Never bank details, tax IDs, addresses, or payment-method numbers.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FEOH_TEAMS_SECURITY_TOKEN` | (empty) | Teams **security token** (base64) — the HMAC key for **both** directions: the card's action signature and the interactivity-POST verify. **Empty → feature OFF in both**: no actions are rendered and every inbound POST is rejected (fail-closed, no hardcoded fallback). NON-secret base64 dev value committed in `.env.development`; real secret via sops. The token's presence IS the on/off switch (mirrors `FEOH_SLACK_SIGNING_SECRET`). |
| `FEOH_TEAMS_REQUEST_MAX_AGE_SECONDS` | `300` | Reject a Teams interactivity POST whose `X-Teams-Request-Timestamp` is more than this far from now (replay-window guard; only enforced when the header is present — the card deliberately omits it, see above). |
| `FEOH_EMAIL_ACTION_SIGNING_KEY` | (empty) | Reused to sign the action token (bound to the `teams` channel). Empty → no actions added. |
| `FEOH_EMAIL_ACTION_TTL_HOURS` | `168` | Reused as the action token's validity window. |
| `FEOH_API_PUBLIC_URL` | `http://localhost:8000` | Externally-reachable API base. The card's `HttpPOST` actions target `<this>/api/approvals/teams/interactivity`, so it must be reachable from Microsoft's service for the buttons to work. Empty → no actions added. |

The outbound side also needs the org to have its chat provider set to `teams`
with a configured `webhook_url` on `Organization.settings.chat_notifications`
(see `notifications.md` § Chat notifications). The actions appear on the
`invoice_assigned` event.

## Teams app setup (deployed)

1. In the Teams channel, add an **Incoming Webhook** and put its URL on the org's
   `settings.chat_notifications.webhook_url` with `provider: "teams"` — that is
   how the approval card gets posted at all.
2. Set `FEOH_TEAMS_SECURITY_TOKEN` (sops) to a base64 value. It is the HMAC key
   for both the card's action signature and the inbound verify. If you also
   register an **Outgoing Webhook** pointed at
   `https://<api-host>/api/approvals/teams/interactivity`, use the token Teams
   generates for it, so both wirings share one key.
3. Set `FEOH_API_PUBLIC_URL` to the externally-reachable API base — Microsoft's
   service POSTs the card's actions there, so a loopback value means the buttons
   go nowhere.
4. Set `FEOH_EMAIL_ACTION_SIGNING_KEY` (sops) if not already set for email / Slack
   approval.

No real Teams account is needed for tests — the test suite constructs
correctly-signed interactivity requests in-process.

## Tests

- `tests/test_teams_card_actions.py` (pure) — the outbound half and the
  round-trip: the shared sign/verify primitive and its fail-closed rungs, the
  rendered `HttpPOST` actions (target / body / both signature headers), each
  read-only fallback, PII-freeness of the action block, a drift guard that the
  adapter's target constant is a route the app actually mounts, and the
  provider dispatch in `_build_chat_action_tokens` (teams → `teams` channel,
  slack → `slack` channel, `mock`/unknown → nothing).
- `tests/test_teams_approvals.py` (realdb) — signed approve/reject happy paths +
  immutable audit / exception rows, token-in-`value` and token-in-`text`,
  single-use replay, bad Teams signature → opaque no-op, missing `Authorization`
  → reject, stale timestamp → reject, expired token → reject, feature-off
  (no secret) → reject, segregation + non-approver gates, the
  `teams`/`slack`/`email` channel-binding (a teams token is rejected under the
  email and slack expectations and vice versa), and the
  `build_teams_action_tokens` primitive — plus the **closed loop**: a card
  rendered by the real production path (the notification chokepoint mints the
  tokens, the adapter signs the body) is posted verbatim to the real endpoint and
  approves / rejects; it still works when Teams substitutes its own bearer token
  on `Authorization`; and Approve's signature over Reject's body is refused with
  the same opaque ack.
- Auth-gating is covered by `tests/test_rbac.py` (the route is in
  `ALTERNATE_AUTH`).

## Deferred

- **Richer message updates** — on success we return a simple message ack rather
  than rewriting the original card in place. That polish is a follow-up.
- **Adaptive Cards** — the outbound card is the legacy `MessageCard` format,
  because that is what a Teams **incoming webhook** accepts (Office connectors do
  not accept Adaptive Cards). Moving to `Action.Execute` / `Action.Submit` would
  mean registering a real Bot Framework app and replacing the shared-secret gate
  with Bot Framework JWT validation — a different auth model, not an increment on
  this one.
