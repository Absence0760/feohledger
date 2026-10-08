# Slack interactive approval — approve / reject from the Slack message

When an invoice is **assigned for review** and the org's chat provider is Slack,
the approval message posted to the channel carries **Approve / Reject buttons**.
An approver clicks a button and the decision runs through the **exact same**
`services.review` path the in-app buttons use — no app login, no JWT. This closes
the last "Slack/Teams approval" gap under **Advanced Approval Routing** in
`docs/roadmap_shipped.md` for the Slack side (Teams interactivity shipped since).

It builds directly on two existing pieces and adds nothing weaker:

- the **outbound Slack adapter** (`services/chat_notification_adapters/slack_adapter.py`)
  — extended to render an interactive Block Kit `actions` block; and
- the **email-approval action token** (`services/email_action_token.py`) — reused
  as the credential, bound to a new `slack` channel so the two surfaces' tokens
  are not interchangeable.

## The button credential — the action token, bound to a channel

Each button's `value` is a signed, single-use **action token** — the same
primitive the email-approval link uses. It binds, under an HMAC-SHA256 signature
the platform alone can produce, the exact facts the action will run against:

```
tenant_slug + invoice_id + actor_id (the intended approver) + action + channel + expiry + jti + facts
```

`facts` binds an Approve to the figures the post displayed — see
[email-approval.md](email-approval.md) § Why a token, not a login. An edit since
the message was sent acks "This invoice changed after this message was sent"
and approves nothing.

The new `channel` claim (`email` / `slack`) is what makes the surfaces distinct:

- `build_slack_action_tokens(...)` mints `channel="slack"` tokens for the buttons;
- the Slack endpoint calls `verify_action_token(..., expected_channel="slack")`;
- the email endpoint keeps the default `expected_channel="email"`.

So a Slack token presented to the email-confirm endpoint — or an email link token
fed into the Slack endpoint — fails verification. A token built with no channel
(the original email callers) defaults to `email`, so the email-approval flow is
unchanged.

The token is minted **per intended approver**. `review.assign_reviewer` fires the
`invoice_assigned` event with a single reviewer, so the channel post binds the
buttons to that one approver — same no-privilege-escalation property as the
per-recipient email link. If an `invoice_assigned` event ever carries zero or
many recipients, the buttons are omitted (the deep link still works).

Tokens are only added when **`FEOH_EMAIL_ACTION_SIGNING_KEY` is set and the chat
provider is `slack`**; otherwise the message stays a plain (non-interactive) post.

## The inbound webhook

```
POST /api/approvals/slack/interactivity   — PUBLIC, no JWT
```

`app/api/slack_approvals.py`. Public-by-design (Slack POSTs it; the signature +
the token are the gates) and listed in `ALTERNATE_AUTH` in `tests/test_rbac.py`,
exactly like the PEPPOL-inbound and email-approval routes.

Two gates, layered, both fail closed:

1. **Slack request signature.** Slack signs every interactivity POST as
   `X-Slack-Signature: v0=<HMAC-SHA256 over "v0:{X-Slack-Request-Timestamp}:{raw_body}">`
   with the app's *signing secret* (`FEOH_SLACK_SIGNING_SECRET`). We rebuild that
   base string and compare with a constant-time HMAC, and **reject a timestamp
   more than `FEOH_SLACK_REQUEST_MAX_AGE_SECONDS` (default 300s) from now** so a
   captured POST can't be replayed. No secret configured → the feature is OFF and
   every request is rejected (no hardcoded fallback).
2. **Action token.** Parse the `application/x-www-form-urlencoded`
   `payload=<json>` interactive envelope, pull the token from the clicked
   button's `value`, and `verify_action_token(..., expected_channel="slack")`
   (HMAC + expiry + channel). Then load the named reviewer (active, right org,
   holds the `invoice.approve` granular permission — the shared
   `email_actions.may_approve` gate, identical to the in-app
   `require_permission(PERM_INVOICE_APPROVE)`, so a custom role granting it
   works here too), **claim the token `jti`**
   in Redis (single-use), and call `review.approve_invoice` /
   `review.reject_invoice` **as the reviewer**. Segregation of duties, the
   approval thresholds, the CFO gate, the `invoice.approved`/`invoice.rejected`
   immutable audit row, and the approval digital signature all apply exactly as
   if they had logged in — and the org's `settings` ride along as `org_settings`,
   so the tenant's own `fraud_rules` / `matching` PO tolerances / `exceptions`
   routing / structuring window govern here too, never the platform defaults.
   On a **multi-level chain** the invoice stays `ready_for_review` for the next
   approver, and the ack says the approval was *recorded*, not that the invoice
   is approved.

If the action turns out not applicable / not permitted (invoice no longer
`ready_for_review`, segregation block, CFO gate, over the cap), the `jti` claim is
**released** so the reviewer can still act in-app.

### Opaque responses — no enumeration

Every path — success **and** every rejection (bad signature, stale timestamp,
expired / replayed / wrong-channel token, unknown tenant / invoice, non-approver,
feature off) — returns the **same opaque `200` ephemeral ack**
(`{"response_type": "ephemeral", "text": "..."}`). A 4xx, or a distinct message
per failure, would let a probe enumerate tenants, invoices, or which secret/token
shapes are accepted. The handler is fully self-guarded — it never raises a 500 on
the public route — and logs only a PII-free reason code (never the token, the
invoice fields, the webhook URL, or any banking/PII).

### Single-use, layered (replay protection)

- **Slack timestamp window** — a captured POST replayed > 5 min later is rejected
  before any work.
- **Workflow state machine** — approve/reject move the invoice out of
  `ready_for_review`, so the same decision can't re-fire.
- **Redis consume on the button PAIR** — `SET NX EX` on
  `ActionToken.consume_key`: the message's Approve and Reject buttons share a
  signed pair id, so pressing either spends both, which closes the
  reject→resubmit replay window (see `email-approval.md` § Single-use). The
  token IS the dedupe — a re-clicked button can't double-act.

## What's PII-free

Nothing sensitive enters the Slack message or the logs. The outbound message
carries only invoice number, vendor name, amount + currency, the status word, and
a deep link (the same fields the outbound chat adapter already allowed) plus the
two opaque HMAC tokens. Never bank details, tax IDs, addresses, or
payment-method numbers.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FEOH_SLACK_SIGNING_SECRET` | (empty) | Slack app **signing secret** for the interactivity-POST HMAC. **Empty → feature OFF**: every inbound POST rejected (fail-closed, no hardcoded fallback). NON-secret dev value committed in `.env.development`; real secret via sops. The key's presence IS the on/off switch (mirrors `FEOH_EMAIL_ACTION_SIGNING_KEY`). |
| `FEOH_SLACK_REQUEST_MAX_AGE_SECONDS` | `300` | Reject a Slack interactivity POST whose `X-Slack-Request-Timestamp` is more than this far from now (replay-window guard). |
| `FEOH_EMAIL_ACTION_SIGNING_KEY` | (empty) | Reused to sign the button action token (bound to the `slack` channel). Empty → no buttons added. |
| `FEOH_EMAIL_ACTION_TTL_HOURS` | `168` | Reused as the action token's validity window. |

The outbound side also needs the org to have its chat provider set to `slack`
with a configured `webhook_url` on `Organization.settings.chat_notifications`
(see `notifications.md` § Chat notifications). The buttons appear on the
`invoice_assigned` event.

## Slack app setup (deployed)

1. In the Slack app config, enable **Interactivity & Shortcuts** and set the
   **Request URL** to `https://<api-host>/api/approvals/slack/interactivity`.
2. Copy the app's **Signing Secret** into `FEOH_SLACK_SIGNING_SECRET` (sops).
3. Set `FEOH_EMAIL_ACTION_SIGNING_KEY` (sops) if not already set for email approval.

No real Slack account is needed for tests — the test suite constructs
correctly-signed interactivity requests in-process.

## Tests

- `tests/test_slack_approvals.py` (realdb) — signed approve/reject happy paths +
  immutable audit / exception rows, single-use replay, bad Slack signature →
  opaque no-op, stale timestamp → reject, expired token → reject, feature-off
  (no secret) → reject, segregation + non-approver gates, the `slack`/`email`
  channel-binding (a slack token is rejected at the email expectation and vice
  versa), and the adapter's interactive Block Kit button rendering.
- Auth-gating is covered by `tests/test_rbac.py` (the route is in
  `ALTERNATE_AUTH`).

## Deferred

- **Richer message updates** — on success we return a simple ephemeral ack rather
  than a full `chat.update` that rewrites the original message in place (e.g.
  "Approved by Jane at 14:03"). That `response_url` / `chat.update` polish is a
  follow-up.
