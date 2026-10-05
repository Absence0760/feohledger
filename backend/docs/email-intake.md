# Email-to-invoice intake

Vendors email PDF invoices to a per-tenant address. We receive them via
an email provider webhook, create invoice rows, and dispatch extraction
— no UI interaction required. The AP team tells their vendors "send
invoices to this address" and the queue fills itself.

## Architecture

```
Vendor's mailbox
     │
     ▼ SMTP
 MX record → SES / Mailgun / Postmark
     │
     ▼ webhook (JSON / multipart)
POST /api/email-intake/inbound/{provider}
     │
     ├── HMAC signature check (rejects spoofed webhooks)
     │
     ├── Provider adapter parses → InboundEmail (normalized)
     │
     ├── Token(s) in the ENVELOPE recipients → resolve each Organization
     │     (one message to two tenants' addresses = one delivery per tenant)
     │
     ▼
 process_inbound_email(ctrl_db, payload)
     │
     ├── Open tenant session
     ├── For each PDF / image / XML attachment:
     │     ├── Create Invoice(status=pending, uploaded_by_id=NULL)
     │     ├── Freeze the WorkflowInstance snapshot (create_workflow_instance)
     │     ├── Upload file to S3
     │     └── Commit
     └── Dispatch extraction (one job per invoice)
```

**The invoice gets a frozen workflow snapshot at ingest**, exactly like every
other ingress (`POST /api/invoices/upload`, manual create, the supplier portal,
`recurring_invoices`, `intercompany`): `create_workflow_instance` runs right
after the `flush()` that assigns the PK. Skipping it left email invoices with
no `WorkflowInstance` at all — so a later edit to the tenant's workflow
definition retroactively governed them, they carried no `WorkflowStep` rows,
and they were invisible to the step-based approval-queue reads and to
`GET /api/invoices/{id}/workflow`. See `backend/docs/workflow-snapshots.md`.

**Accepted attachment types:** `application/pdf`, `image/png`, `image/jpeg`,
`image/tiff`, and — for structured e-invoices — `application/xml` / `text/xml`.
A UBL 2.1 or UN/CEFACT CII XML attachment is parsed deterministically by the
`einvoice` adapter (Factur-X / ZUGFeRD arrive as PDF and are covered by
`application/pdf`). See `backend/docs/e-invoicing.md`.

## Endpoints

### Public (no JWT)

`POST /api/email-intake/inbound/{provider}` — providers: `ses`, `mailgun`,
`generic`. Security is HMAC on the body (header: `X-Signature`,
`X-Webhook-Signature`, or provider-specific).

### Admin (JWT + `admin` role)

- `GET /api/organization/email-intake` — `{address, enabled}`. `address` is
  `null` whenever no address can be rendered — see "Two shapes of no address"
  under "The UI" below.
- `POST /api/organization/email-intake/rotate-token` — invalidates the
  old address immediately and returns `{address}`. It is also the *only*
  writer of the token, so it doubles as the first-time provisioning call.

Both are surfaced in the product — see "The UI".

## What you (the founder) need to do

### 1. Pick an email provider

- **AWS SES** — Cheap, reliable, works with inbound via SNS. Requires
  domain verification (DKIM + SPF + DMARC) and receiving-rule setup.
  Best if you're already on AWS.
- **Mailgun** — Simplest onboarding, form-data webhooks. Free tier
  covers 1k emails/month, then ~$35/mo.
- **Postmark** — Clean JSON webhooks, best docs. Pair their
  inbound-email feature with the `generic` adapter (small forwarder
  Lambda). ~$15/mo for inbound.
- **SendGrid Inbound Parse** — Works but support-tax is high; avoid
  unless you already use SendGrid.

Recommended: **SES** if on AWS, **Mailgun** otherwise.

### 2. Pick and verify a domain

You need a domain where the MX records can point at the email provider.
Options:
- Subdomain of your app domain: `ap.feohledger.com`. Keeps the primary
  domain's reputation isolated from email-intake's spam signals.
- Dedicated domain: `feohledger-invoices.com`. More work to manage but
  easier to sunset if a provider goes sideways.

Set `FEOH_EMAIL_INTAKE_DOMAIN=ap.feohledger.com` in the backend env.

### 3. Point MX at the provider

**SES:**
```
ap.feohledger.com.  MX 10 inbound-smtp.us-east-1.amazonaws.com.
```
Then create a receipt rule in SES console → store to S3 *or* publish to
SNS. The SNS path is the one the `ses` adapter understands. Subscribe a
Lambda (or our webhook directly, via HTTPS subscription) to the SNS
topic.

**Routing reads the SMTP envelope, not the `To:` header.** The `ses` adapter
takes the recipients from the notification's `receipt.recipients` (the
addresses the receipt rule matched), then `mail.destination`, and falls back
to the MIME `To:` header only when the notification carries neither. The
header is the wrong source for the common setups: a vendor that Cc's or Bcc's
the intake address, or an AP mailbox with a redirect rule pointing at it
(Outlook / Gmail redirects keep the original `To: ap@customer.com`), leaves no
intake address in `To:` at all, and reading it dropped every such message as
"unknown intake address". A Lambda forwarder that rewrites the SNS payload
must keep `receipt` / `mail` intact. (Mailgun's `recipient` field is already
the envelope recipient of the matched route.)

**Mailgun:**
```
ap.feohledger.com.  MX 10 mxa.mailgun.org.
ap.feohledger.com.  MX 10 mxb.mailgun.org.
```
Then create a Route in the Mailgun console:
- Match recipient: `.*@ap.feohledger.com` (regex route)
- Action: `forward("https://api.feohledger.com/api/email-intake/inbound/mailgun")`
- Store and notify: unchecked (we don't need the S3 copy)

### 4. Set the signing secret

Generate a random 32-byte secret and set it both in Mailgun/SES config
*and* in the backend env:

```
openssl rand -hex 32 | tee /dev/stderr | head -c 64
# copy to both places
```

```
FEOH_EMAIL_INTAKE_SIGNING_SECRET=<the-hex-string>
```

For Mailgun, their built-in `signature` parameter is HMAC-SHA256 of
`timestamp + token` using your API key — **not** the same as our check.
Easiest: use the Mailgun webhook signing key as `FEOH_EMAIL_INTAKE_SIGNING_SECRET`
and verify via the provider's standard signature header
(`X-Mailgun-Signature-V2`).

For SES/SNS, configure the Lambda forwarder to HMAC the body before
calling our webhook.

For local dev / testing, leave the secret empty — the webhook will
accept anything (logged as a warning).

### 5. Provision tokens for each tenant

**Nothing provisions a token automatically** —
`provision_intake_token(org)` has exactly one caller,
`POST /api/organization/email-intake/rotate-token`. Signup does not run
it, so every tenant starts with no intake address at all. Calling it as
part of the welcome flow would change that; until then, minting the
first address is an admin action taken in the product (the panel labels
it **Create intake address**, because creating one invalidates nothing).

The tenant's admin reads the address off the **Email Intake** panel in
Organization Settings and tells their vendors:
> "Send invoices to `invoices+a1b2c3d4@ap.feohledger.com`."

### 6. Tell the tenant their address is sensitive

The token is a bearer secret. Anyone who knows it can drop PDFs into
that tenant's AP queue. Rotating the token invalidates the old address
instantly — that is the containment control for a leak, and it is
reachable from the same panel.

Good practice: rotate the token annually, and immediately after a
suspected leak (e.g. a vendor forwards our intake email to someone
outside the approved recipient list).

## The UI

`/organization` → the **Email Intake** panel
(`frontend/src/routes/organization/+page.svelte`, over
`frontend/src/lib/api/emailIntake.ts`). It is the only place either admin
endpoint is called from; before it, the whole inbound channel was
undiscoverable and a leaked token could not be rotated from the product
at all.

The panel renders the address in a monospace block with a copy control,
says plainly that it is a bearer secret, and offers **Rotate address**
as an **armed two-click**: the first click only arms it and reveals the
consequence ("the current address stops accepting email the moment you
confirm — anything a vendor sends to it afterwards is dropped, with no
bounce and no invoice"); the second commits, and the replacement is shown
through the shared `ui/SecretReveal.svelte` with a copy button. That
dialog's warning is about the OLD address dying, not about the new value
being unrecoverable — unlike an API key, this address is re-readable from
the panel, and the reveal must not claim otherwise. Clicking anywhere
outside the control un-arms it.

`/organization` is **read-only for a non-admin**: every panel sits inside
one disabled `<fieldset>` under an explanatory banner. Every mutating
endpoint behind that page is already `require_roles(ROLE_ADMIN)` and the
server remains the authority; what was missing was any acknowledgement of
it client-side, so a clerk got a fully editable form whose every Save
403'd. `GET /api/organization/email-intake` is admin-only too, so the
panel says so for a non-admin rather than firing a request that 403s and
rendering the failure as if something were broken.

### Two shapes of "no address", and why the UI asks before it tells

`GET` answers `address: null` in two different situations, and the
payload alone cannot tell them apart:

1. `FEOH_EMAIL_INTAKE_DOMAIN` is unset — `intake_address_for` returns
   `None` before it ever looks at the token, so the whole channel is off
   for this deployment. This is the committed default, so it is what a
   fresh clone sees.
2. This org has no token yet (see §5 — nothing mints one for you).

`enabled` separates them, but only *after* a token exists: provisioning
is what sets it, so `enabled: true` with a null `address` can only mean
the platform has no intake domain. Before that the honest answer is "not
known yet". The panel therefore:

- offers **Create intake address** (non-destructive) in the ambiguous
  state, which is also how it finds out;
- renders a standing explanation naming `FEOH_EMAIL_INTAKE_DOMAIN` and
  the MX step once the server has *proven* the domain is missing, with
  **no rotate control at all** — another token would still address
  nothing.

The durable fix is for `GET` to report whether the platform intake domain
is configured, separately from whether this org has a token; then the
disabled-feature state would be knowable on the first read instead of
costing a throwaway token to establish.

E2E: `frontend/tests-e2e/organization/email-intake.spec.ts`. The
disabled-feature and non-admin cases run against the real backend; the
configured-deployment states are replayed through `page.route`, because
the intake domain is a backend env var read at import time and no test
can turn it on.

## Local testing

No MX record needed — just POST a normalized JSON body at the `generic`
adapter:

```bash
curl -X POST http://localhost:8000/api/email-intake/inbound/generic \
  -H 'Content-Type: application/json' \
  --data '{
    "to": "invoices+<your-token>@ap.example.com",
    "from": "ap@vendor.example.com",
    "subject": "Invoice INV-1234",
    "attachments": [
      {
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "content_base64": "'"$(base64 < sample.pdf)"'"
      }
    ]
  }'
```

Grab a token from:
```sql
SELECT slug, settings->'email_intake'->>'token'
FROM organizations;
```

The HTTP response is a deliberately opaque `200 {"status": "received"}` ack —
identical whether the token resolved, was unknown, was a duplicate delivery,
or processing failed internally (see "What the response does NOT tell you"
below). Watch the backend log for the real outcome (`Email intake processed:
provider=... tenant=... invoices_created=N error=...`) or just check the
invoice queue in the tenant UI.

## What the intake does NOT do

- **No reply emails.** We don't acknowledge receipt to the vendor. Add a
  provider-side auto-reply if vendors need confirmation.
- **No spam filtering.** Anything hitting a valid intake address becomes
  an invoice in that tenant's queue. Lean on the provider's built-in
  spam score before our webhook.
- **No body parsing.** The email body text is stored in the invoice
  description; the extraction pipeline only looks at the attachments.
- **No sender allowlist.** If your tenant wants to restrict intake to
  specific sender domains, that's a future enhancement — the current
  model is "any email with our token gets processed".
- **No support for inline attachments.** Only `Content-Disposition:
  attachment` parts are processed. Images embedded inline in the body
  are ignored (avoids turning email signature logos into invoices).

## What the response does NOT tell you

The webhook response is intentionally uninformative. Pre-signature rejections
(unknown `{provider}`, bad/missing HMAC signature, a payload the adapter can't
parse) return a bare `204`. Once the signature verifies, **every *decision***
— an unresolved or disabled intake token, a duplicate delivery of a message
already processed, no usable attachments, or a genuine success — returns the
identical `200 {"status": "received"}` ack. This is deliberate: the HMAC signing
secret is shared across every tenant on the platform (the email provider has no
notion of tenants), so if the response body or status code varied by outcome,
anyone who can produce a validly-signed request could grind through candidate
tokens and watch for the response to change — an oracle for a per-tenant intake
token, which is a bearer secret. See "Redelivery / duplicate handling" below for
how retries are still handled correctly despite the opaque ack.

Distinguish outcomes from the **backend log** (`Email intake processed:
provider=... tenant=... invoices_created=N error=...`) or by watching the
tenant's invoice queue, never from the HTTP response.

### The one thing it does tell you: that WE broke

A **decision** is a final answer about the message. A failure of **ours** — S3
unreachable, the tenant DB down, Redis flapping — is not an answer at all; the
message is still the vendor's unprocessed invoice. Those two get different
responses:

| Outcome | Response |
|---|---|
| Unknown provider / bad signature / unparseable body | `204`, bodyless |
| Unknown or disabled intake token | `200 {"status": "received"}` |
| Duplicate delivery (same `message_id`) | `200 {"status": "received"}` |
| No usable PDF / image / XML attachment | `200 {"status": "received"}` |
| Invoice(s) created | `200 {"status": "received"}` |
| **An exception inside our own processing** | **`503`, bodyless** |

`services/email_intake.process_inbound_email` releases its Redis dedup claim and
re-raises on exactly those failures, so "the NEXT delivery of the same
message_id actually retries the work". Acking `200` there told SES / Mailgun the
message *was* delivered, so there was no next delivery: the release-on-failure
code was preparing for a retry that could never come, and the invoice was gone
with only a log line behind it.

The trade-off is bounded and deliberate. A `5xx` on our own failure narrows the
token-enumeration oracle down to "while the platform is already broken" — an
attacker learns only that something inside us threw, never whose token they
guessed, and only during an outage. Losing every invoice that arrives during a
blip is the larger harm. `api/billing_webhook.py` faced the identical choice and
already went this way; `api/erp_webhook.py` matches. The 503 is **bodyless**, so
the response still carries no detail, no stack trace and no tenant.

Operationally: a provider dashboard showing 5xx deliveries for the intake
endpoint means *the platform is failing*, not that anyone sent a bad message —
the two used to be indistinguishable from the outside.

### …and neither does the log carry the address

The recipient address never reaches a log line either. Its `+<token>` part IS
the tenant's bearer credential, and the unresolved branch is hit with a **live,
correct** token every time an org simply toggles intake off — so logging the
address wrote a working credential (plus a third party's email address) into
the application log, where it is retained and shipped like any other line. The
miss is logged by *shape* instead:

```
Email intake: recipient did not resolve to an enabled intake address (token_present=True)
```

`token_present=False` means the address carried no `invoices+<token>@` part at
all — a wrong address or a mis-pointed MX — which is the distinction an
operator actually acts on. To check a specific tenant's token, read it from
`GET /api/organization/email-intake` (admin-gated), not from the log.

## Redelivery / duplicate handling

Every inbound message is deduped by the provider's `Message-ID`, **per
tenant** (via the shared `is_event_already_processed("email_intake",
"<org_id>:<message_id>")` Redis guard — the same primitive the
payment/card/ERP webhooks use; the key is built by
`email_intake.dedup_event_id`). The org scope is load-bearing: `Message-ID`
names the email, not the delivery, so one message addressed to two tenants'
intake addresses carries one id — Mailgun forwards it once per matched
recipient, SES lists both recipients in one notification. With a
platform-global key the first tenant claimed it and the second tenant's
invoice was dropped as a "duplicate delivery" it had never received. A
notification naming several intake addresses is processed tenant by tenant,
each with its own transaction and claim, so if one tenant fails (503) the
redelivery dedupes the tenants that already committed and retries only the
failed one.

**Transitional:** a claim written before the per-tenant change has the bare
`email_intake:<message_id>` key and does not record which tenant it reached, so
while one is still live (the 72h dedup TTL) `_legacy_claim_live` treats the
message as already processed for every tenant — the only rule that cannot turn
a provider's redelivery of a pre-deploy message into a second payable. Its
removal, once the TTL has elapsed after deploy, is tracked in
`docs/followups.md`. A provider retry or a
genuine duplicate delivery of the same message creates **zero** additional
invoices; the second delivery still gets the same opaque `200` ack. Providers
that don't set `Message-ID` (or set an empty one) can't be deduped and are
always processed — call out to your provider's docs to confirm they always
populate it.

If invoice creation fails partway through — **before the tenant transaction
commits** (e.g. a transient S3 or tenant-DB outage) — the dedup claim is
released so the *next* redelivery of that same `Message-ID` retries the work
instead of the message being silently dropped for the dedup TTL window.

**Past that commit the claim is kept, deliberately.** The invoices are durable
by then, so a failure after it is not "this message went unprocessed" — and
releasing would let the redelivery sail past the dedup check and create a
SECOND payable per attachment for the same email. The realistic post-commit
failure is `dispatch_extraction`, which under `FEOH_EXTRACTION_MODE=lambda` is
a real boto3 SQS round trip; it is logged by exception class name and
swallowed, and the route acks rather than asking for a redelivery.

Nothing is lost by swallowing it: the invoice sits at `pending`, the
`extraction_reaper` sweep ages it to `failed` with an `extraction_timeout`
warning, and a reviewer re-runs extraction from the invoice. A duplicate
payable, by contrast, is a money-path defect with no automatic remedy. This is
the same line `api/payments.py`'s processor webhook draws — its post-commit ERP
sync sits *outside* the `try` that releases the claim. Guarded by
`tests/test_email_intake_processing.py::test_post_commit_failure_keeps_the_dedup_claim_so_a_retry_cannot_duplicate`.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| 204 with no body | Unknown `{provider}` in the URL, a bad/missing signature, or a payload the adapter couldn't parse. Wrong URL — `{provider}` must be `ses`, `mailgun`, or `generic`. Check the backend log for the specific rejection reason (never surfaced in the response). |
| 200 `{"status": "received"}` but no invoice appears | Could be an unresolved/disabled token, a duplicate delivery, no usable attachments, or an internal failure — the response can't tell you which. Check the backend log line for the real `error` / `invoices_created` count. |
| "recipient did not resolve to an enabled intake address (token_present=True)" | The address carried a `+token` that matched no *enabled* tenant. Either the token is wrong, or that org's `email_intake.enabled` is false. Compare against `GET /api/organization/email-intake`; the log deliberately never prints the token. |
| "…(token_present=False)" | The address had no `invoices+<token>@` part — a mis-pointed MX, or someone mailing the bare domain. |
| Invoice created but stays in `pending` | Extraction worker not running / tenant ERP config broken. Same as any other upload failure — check `extraction_reaper` logs. |
| "No usable PDF / image / XML attachments" (in the log) | Vendor attached `.docx` or `.zip`. Tell the vendor to send PDF, an image, or a structured e-invoice XML. |
| "Duplicate delivery" (in the log) | The provider redelivered a message with a `Message-ID` already processed — expected behavior, not a bug. |
