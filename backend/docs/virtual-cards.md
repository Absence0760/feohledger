# Virtual Cards

## Overview

Virtual cards are single-use credit card numbers generated per invoice payment. Instead of sending ACH or wire, the system creates a virtual card with a spending limit matching the invoice amount, sends the card details to the vendor, and the vendor charges the card. The card expires after use.

**Why this matters:**
- **Revenue**: 1-2% cashback rebate on every payment — this is the primary monetization channel for AP platforms
- **Security**: Single-use cards can't be reused or overcharged
- **Speed**: Instant payment — no waiting for ACH settlement
- **Control**: Per-card spending limits, merchant restrictions, auto-expiry
- **Reconciliation**: Card charges auto-match to invoices — no manual matching

## Multi-Region Hybrid Strategy

Virtual card issuance is region-specific due to banking regulations. The system uses a hybrid approach with multiple providers:

| Region | Default Provider | Interchange Sharing | Notes |
|---|---|---|---|
| **US** | Lithic | 0.5-1.0% from day one | Best API, fastest integration |
| **UK / EU** | Lithic | 0.5-1.0% from day one | EU coverage via Lithic's European entity |
| **South Africa** | Nium | 0.3-0.8% | Nium covers 40+ countries |
| **Australia, Asia-Pacific** | Nium | 0.3-0.8% | SG, HK, AU, NZ, JP, IN |
| **Canada, Latin America** | Nium | 0.3-0.8% | BR, MX, CA |

**Auto-selection:** The system auto-selects the provider based on the org's region. Admins can override in Organization Settings.

**Graduation path:**
1. **Start** with Lithic (US/UK/EU) + Nium (rest of world) — interchange sharing from day one
2. **Scale** — as card volume grows, negotiate better rates with providers
3. **Graduate** to BIN sponsorship when volume exceeds $10M/month — higher rebates (1.5-2.0%) but requires sponsor bank relationship

### BIN Sponsorship (Future — High Volume)

At >$10M/month card spend, a direct BIN sponsor relationship yields higher interchange:

| Region | Sponsor Banks | Setup Time | Rebate Rate |
|---|---|---|---|
| US | Sutton Bank, Celtic Bank, Column | 3-6 months | 1.5-2.0% |
| UK | Railsr, ClearBank, Modulr | 3-6 months | 1.5-2.0% |
| EU | Railsr, Swan, Treezor | 3-6 months | 1.5-2.0% |
| Other | Not widely available | — | Use Lithic/Nium |

BIN sponsorship is only available in US/UK/EU. Other regions continue using Nium.

### Markets Without Virtual Cards

Some regions don't support virtual card issuance or have low vendor card acceptance. For these markets, fall back to local payment methods:

| Market | Alternative Payment Method |
|---|---|
| South Africa | EFT via local banks, Stitch, Peach Payments |
| India | UPI, NEFT, RTGS |
| Brazil | Pix, boleto |
| Australia | BPAY, direct debit |

Monetize these markets via subscription + per-invoice processing fees instead of card rebates.

## How It Works

```
Invoice Approved
    |
    v
Payment Run (method: virtual_card)
    |
    v
Generate Card                    (via Stripe Issuing / Marqeta / Lithic)
    - Card number, expiry, CVV
    - Limit = invoice amount
    - Linked to invoice + vendor
    |
    v
Send Card to Vendor              (email with card details, or supplier portal)
    |
    v
Vendor Charges Card              (at their payment terminal or online)
    |
    v
Charge Posts                     (webhook from card provider)
    - Payment status → completed
    - Invoice status → paid
    - Rebate earned → tracked
    |
    v
Card Auto-Expires                (single-use, no further charges possible)
```

## Card Lifecycle

| Status | Meaning |
|---|---|
| `created` | Card generated, not yet sent to vendor |
| `sent` | Card details sent to vendor (email or portal) |
| `active` | Vendor has the card, waiting for charge |
| `charged` | Vendor charged the card, payment processing |
| `completed` | Charge settled, payment confirmed |
| `expired` | Card expired without being charged (auto-expire after N days) |
| `cancelled` | Card manually cancelled before use |
| `declined` | Charge attempted but declined (over limit, wrong merchant, etc.) |

## Data Model

### VirtualCard

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| invoice_id | UUID | FK to the invoice being paid |
| payment_id | UUID | FK to the payment record (nullable until charged) |
| vendor_id | UUID | FK to the vendor receiving the card |
| correlation_id | UUID | Links to invoice correlation ID |
| card_provider | String | `stripe`, `marqeta`, `lithic` |
| provider_card_id | String | External card ID from the provider |
| last_four | String(4) | Last 4 digits of card number |
| amount_limit | Decimal | Spending limit (= invoice amount) |
| amount_charged | Decimal | Actual charge amount (nullable until charged) |
| currency | String(3) | Card currency |
| status | String | Card lifecycle status |
| expires_at | DateTime | When the card auto-expires |
| sent_at | DateTime | When card details were sent to vendor |
| charged_at | DateTime | When the vendor charged the card |
| merchant_name | String | Merchant name from the charge (nullable) |
| decline_reason | String | Reason if declined (nullable) |
| organization_id | UUID | Tenant scoping |
| created_at | Timestamp | |
| updated_at | Timestamp | |

### CardRebate

| Field | Type | Description |
|---|---|---|
| id | UUID | Primary key |
| virtual_card_id | UUID | FK to the virtual card |
| amount | Decimal | Rebate amount earned |
| rate | Decimal(5,4) | Rebate rate (e.g., 0.0150 = 1.5%) |
| status | String | `pending`, `confirmed`, `paid_out` |
| period | String | Rebate period (e.g., "2026-04") |
| organization_id | UUID | Tenant scoping |
| created_at | Timestamp | |

**One rebate per card (hard backstop).** A single-use card settles exactly once
→ exactly one rebate. On top of the webhook's `card.status == "charged"` guard +
event-id dedup, a UNIQUE index `uq_card_rebates_virtual_card` on
`card_rebates(virtual_card_id)` (migration `0069_card_rebate_unique`, fanned out
to every tenant DB — `card_rebates` is tenant-scoped) is the DB-level last line
against a double-rebate under a race / Redis-outage. The settlement branch in
`api/cards.py` inserts the rebate inside a savepoint (`begin_nested`), so a
duplicate is silently skipped (`rebate_created: false` on the `card.settled`
audit row) **without aborting** the card completion + audit write — the
money-state transition still lands.

## User Interface

### Cards Page (`/cards` or tab within `/payments`)

**Card Dashboard (top)**
- Total active cards (count + value)
- Total spend this month via virtual cards
- Rebates earned this month
- Rebates earned YTD
- Card acceptance rate (% of vendors that accept cards)

**Card List (main)**
| Column | Description |
|---|---|
| Card | Last 4 digits + status badge |
| Vendor | Vendor name |
| Invoice | Invoice number (link) |
| Amount | Card limit |
| Charged | Actual charge (blank if not yet charged) |
| Status | Created / Sent / Active / Charged / Completed / Expired |
| Created | Date created |

**Card Actions**
- **Send to Vendor** — email card details (or make available in supplier portal)
- **Cancel** — void the card before it's charged
- **Resend** — resend card details email
- **View Details** — card number (masked), expiry, linked invoice, charge history

**Rebates (below the card list)** — the `pending` → `confirmed` → `paid_out`
lifecycle table. See § Rebate status lifecycle → The UI.

> **What actually ships on the `/payments` Cards tab today**: the dashboard
> KPIs, the card list, **View Details** (the PAN reveal — `GET /api/cards/
> {id}/details`, `tests-e2e/payments/card-details-reveal.spec.ts`) and the
> **Rebates** table with its two transitions. *Send to Vendor*, *Resend* and
> *Cancel* above are design sketch — the frontend calls neither
> `POST /api/cards/generate` nor `POST /api/cards/{id}/cancel`.
>
> **Cancel is deliberately not wired**, and that is not the same gap as the
> rebate lifecycle was. Every card the app itself can mint comes from the
> payment run's `virtual_card` leg and therefore has a `Payment` row, and
> `POST /api/payments/{id}/void` — already on the History tab — is the
> sanctioned reversal: it cancels the card provider-first *and* reverses the
> books in one operation (§ Cancel → "Voiding a card payment cancels the card
> too"). A standalone Cancel button would kill the card while leaving that
> `Payment` row and its invoice claiming money is in flight on a rail that is
> now dead — two controls that both close a card but leave the ledger in
> different states. The residual gap was narrower and was a *visibility* one:
> the void's card leg is best-effort and its `card_outcome` landed only on the
> `payment.voided` audit row, so an operator could not tell from the app whether
> the card was actually closed — and a failed leg left a live, bearer-spendable
> card with no reachable remedy.
>
> **That gap is closed, on the void rather than beside it.** `PaymentResponse`
> now carries `void_card_outcome` plus the verdict `void_card_disposition`
> (`closed` / `no_card` / `not_closed_final` / `not_closed_retryable`), the void
> dialog renders it, and `POST /api/payments/{id}/void/retry-card-cancel`
> re-attempts the card close for an **already-voided** payment — same
> `payment.void` gate, idempotent, audited, moves no money, and 409s on anything
> still live, so it cannot become the divergent second control §96 refused. See
> `payments.md` § The outcome is on the response, and so is the remedy.

### Card Generation in Payment Run

When creating a payment run:
1. User selects invoices and chooses "Virtual Card" as payment method
2. System checks which vendors accept card payments (from vendor record)
3. For eligible invoices, cards are generated in batch
4. Summary shows: X cards generated, total limit $Y, projected rebate $Z
5. User confirms → cards are created and sent to vendors

### Vendor Card Acceptance

Not all vendors accept credit card payments. Track this per vendor:

| Field on Vendor | Type | Description |
|---|---|---|
| accepts_virtual_cards | Boolean | Whether vendor accepts card payments |
| card_contact_email | String | Email to send card details to (may differ from main contact) |
| card_merchant_name | String | Expected merchant name on charges (for matching) |

The payment run UI should:
- Show which invoices can use virtual cards (vendor accepts)
- Suggest ACH/wire fallback for vendors that don't
- Allow the user to override (try card anyway, or force ACH)

## Card Provider Integration

### Provider resolution — a named unknown provider is refused, never `mock`

`card_adapters/dispatcher.get_card_adapter` resolves a **missing**
`settings.cards.provider` through `get_default_provider` (see § An unset provider
resolves `mock`, below — an org that has never named an issuer is a normal
state) and raises `UnknownCardProviderError` for a **named** provider it has no
adapter for.

It used to fall back to `mock`, and `mock` is not an inert stub:

| `MockCardAdapter` method | What it returns |
|---|---|
| `create_card` | `success=True`, a synthetic `mock_card_…` id, `last_four="4242"` |
| `get_card_details` | the fixture PAN `4242424242424242` |
| `cancel_card` | `True`, unconditionally |

So one typo in an admin-entered provider name (a BYOK tenant writing
`"marqeta"`) made every issuance "succeed": `VirtualCard` rows landed with
`card_provider="mock"`, the payment-run card leg marked each payment `completed`
and each invoice `payment_scheduled`, `POST /api/cards/generate` reported cards
minted, and vendors were emailed single-use reveal links resolving to a fixture
PAN. No money moved and nothing failed. This is the one dispatcher family
`decisions.md` §29 missed (§36 did the same for sanctions).

Because a dispatcher cannot know whether its caller is moving money or drawing a
screen, **each caller decides what the refusal means**:

| Call site | On refusal |
|---|---|
| `card_issuance.issue_card_for_invoice` (the batch endpoint AND the payment-run card leg) | `CardIssueResult(success=False, failure_reason="card_provider_not_configured")`. No provider call is made, so no card exists anywhere — the `_not_configured` suffix puts it in `payment_runs`' existing "per-adapter pre-flight refusal" bucket, i.e. `RETRY_SAFE`. The reason names the *condition*, never the admin's raw settings value: every AP user reads `failure_reason` while only an admin owns the setting (same split as `fx_provider_unsupported`). |
| `POST /api/cards/generate` | 409 up front, naming the bad value and the registered alternatives. Per-invoice refusal alone would report `total: 0`, indistinguishable from "nothing was eligible" — which is how the misconfiguration stayed invisible. |
| `GET /api/cards/{id}/details` | 409 — the caller must never receive the fixture PAN believing it came from a real issuer. |
| `POST /api/cards/{id}/cancel` | 409, and the row stays `active`. `mock.cancel_card` returns `True`, so the fallback marked the row cancelled while the real card stayed live and chargeable — the exact direction this endpoint's provider-first ordering exists to prevent. |
| `card_issuance.cancel_card_at_provider` (payment void) | `card_provider_not_configured`, distinct from `cards_not_configured` (cards switched off). Best-effort like every other outcome here: recorded on the `payment.voided` audit row, never raised, and never a claimed cancel. Both classify as `not_closed_retryable`, so the void dialog surfaces them and offers the card-close retry once the provider name is fixed. |
| `GET /portal/cards/{token}` (vendor reveal) | The PII-free degraded body (`pan`/`cvv` `None` + a warning), same as a provider outage. The single-use token stays spent — a link that survives a failed reveal is indistinguishable from a twice-revealable one. |

The dispatcher imports the three built-in adapter modules itself, so the refusal
does not depend on each call site remembering its
`import app.services.card_adapters.lithic  # noqa` preamble.

Guard: `tests/test_card_provider_resolution.py`.

### An unset provider resolves `mock`, not a real issuer

Guard rail 7 (local-first): the whole app has to run on a dev laptop with no
cloud account. Every other adapter registry honours that by resolving an
*unconfigured* provider to its fixture adapter. Cards did not — an unset
`settings.cards.provider` went straight through `REGION_DEFAULTS` to `lithic`
(or `nium` outside the US/EU), and `scripts/seed.py` seeded every demo and e2e
tenant with `cards.enabled: true` and no provider. A fresh clone's
`POST /api/cards/generate` therefore called Lithic's real host with an empty API
key.

`REGION_DEFAULTS` is now a **preference**, not the resolution:

| Function | Answers |
|---|---|
| `region_preference(region)` | which issuer this region *prefers* — the pure map lookup, unchanged (EU/EEA/US → Lithic, rest of world → Nium) |
| `get_default_provider(region)` | what an org that named nothing actually gets: the preference **if a credential for it exists**, else `LOCAL_FIRST_PROVIDER` (`mock`) |

A credential counts from either of the two places one can live: the org's own
BYOK keys on the card config (`api_key` for Lithic, `client_id` /
`client_secret` for Nium) or this deployment's platform keys
(`FEOH_LITHIC_API_KEY`, `FEOH_NIUM_CLIENT_ID` / `FEOH_NIUM_CLIENT_SECRET`). An
operator who has configured a real issuer keeps region-based selection exactly as
it was; a fresh clone, which has neither, gets `mock`.

This is **not** the §29 / §56 substitution in disguise. Those forbid resolving a
provider someone *named* to the fixture adapter, and that still raises —
including a named real issuer whose key is missing, which resolves to its own
adapter and surfaces the missing credential at the provider call rather than
quietly minting a fixture PAN. Picking a default where nobody expressed a
preference is a different question, and `mock` is its answer.

Both halves shipped, because they protect different tenants:

- The **dispatcher** default protects every tenant created without the seed
  script — self-service signup, `scripts/create_tenant.py` — which is the actual
  fresh-clone path.
- **`scripts/seed.py`** now states `"provider": "mock"` on acme, techflow and
  every `e2e<N>` tenant, so the seeded row says what it uses. Without it, a
  developer who sets `FEOH_LITHIC_API_KEY` for one experiment silently re-points
  every demo tenant at Lithic.

Guard: `tests/test_card_provider_local_first.py` (both halves, including a source
scan over `seed.py`'s `cards` blocks).

### Stripe Issuing (Alternative)

**Why**: Best documentation, good if you already use Stripe. However, Stripe does not share interchange by default — requires negotiation at scale.

**Setup:**
1. Apply for Stripe Issuing (requires Stripe account + Issuing approval)
2. Store API keys in Organization settings
3. Create a cardholder (represents your company)
4. Generate cards per invoice via API

**Create a virtual card:**
```python
import stripe

card = stripe.issuing.Card.create(
    cardholder="ich_...",
    type="virtual",
    currency="usd",
    spending_controls={
        "spending_limits": [
            {"amount": 150000, "interval": "per_authorization"}  # $1,500.00
        ],
    },
    metadata={
        "invoice_id": "...",
        "correlation_id": "...",
        "vendor_name": "Office Supplies Co",
    },
)
```

**Get card details (to send to vendor):**
```python
details = stripe.issuing.Card.retrieve(
    card.id,
    expand=["number", "cvc"],
)
# details.number = "4242424242424242"
# details.cvc = "123"
# details.exp_month = 12
# details.exp_year = 2026
```

**Webhooks:**
- `issuing_authorization.created` — vendor is attempting to charge
- `issuing_authorization.updated` — charge approved/declined
- `issuing_transaction.created` — charge settled

**Rebate:** Stripe doesn't pay rebates directly. You'd negotiate interchange rebates through a Stripe Issuing partner program or use a BIN sponsor.

### Marqeta (Enterprise)

**Why**: Better spend controls, more card program customization, interchange rebate programs.

**Create card:**
```
POST /cards
{
    "card_product_token": "...",
    "user_token": "...",
    "metadata": {
        "invoice_id": "..."
    }
}
```

**Webhooks:** Real-time authorization decisions via JIT (Just-in-Time) funding.

**Rebate:** Marqeta offers interchange revenue sharing as part of their card program.

### Lithic (Developer-Focused)

**Why**: Simple API, good for startups, fast onboarding.

**Create card:**
```
POST /cards
{
    "type": "SINGLE_USE",
    "spend_limit": 150000,
    "spend_limit_duration": "TRANSACTION",
    "memo": "INV-2024-001 - Office Supplies Co"
}
```

**Webhooks:** Authorization events, settlement events.

**Rebate:** Lithic offers interchange sharing programs.

## Organization Settings

Stored in `Organization.settings.cards`:

```json
{
    "cards": {
        "enabled": true,
        "provider": "stripe",
        "api_key": "sk_live_...",
        "cardholder_id": "ich_...",
        "default_expiry_days": 30,
        "auto_send_to_vendor": true,
        "rebate_rate": 0.015,
        "notification_email_template": "default"
    }
}
```

## Rebate Economics

Virtual card rebates are based on interchange fees. When a vendor charges a virtual card, the card network (Visa/Mastercard) charges the vendor's bank an interchange fee (typically 1.5-3% for commercial cards). A portion of that comes back as a rebate.

**Typical rebate rates:**

| Provider | Rebate Rate | Notes |
|---|---|---|
| Stripe Issuing | 0% (direct) | Need partner program for interchange |
| Marqeta | 0.5-1.5% | Revenue share on interchange |
| Lithic | 0.5-1.5% | Revenue share on interchange |
| Direct BIN sponsor | 1.0-2.0% | Highest rates, most complex setup |

**Example economics:**
- $1M monthly AP spend via virtual cards
- 1.5% rebate rate
- = $15,000/month rebate revenue
- = $180,000/year

This revenue often covers the entire cost of the AP platform for the customer, making virtual cards a strong sales tool ("the platform pays for itself").

## Vendor Communication

### Email Template

When a card is generated and sent to a vendor:

```
Subject: Payment for Invoice INV-2024-001 — Virtual Card Details

Hi Office Supplies Co,

Payment for Invoice INV-2024-001 ($1,500.00) has been issued via virtual card.

Card Details:
  Card Number: 4242 4242 4242 4242
  Expiry: 12/2026
  CVV: 123
  Amount: $1,500.00

Please charge this card within 30 days. The card is single-use and will expire after the first charge.

If you have questions, reply to this email or contact our AP team.

— Acme Corp Accounts Payable
```

### Supplier Portal Integration

If the supplier portal is implemented, vendors can:
- View pending card payments
- Copy card details securely (masked until clicked)
- Mark when they've charged the card
- See payment history

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/cards` | List virtual cards (filterable by status, vendor, date) |
| `GET` | `/api/cards/{id}/details` | Get full card number + CVV (admin/ap_manager/cfo, audit logged — see § Security) |
| `POST` | `/api/cards/generate` | Generate cards for selected invoices |
| `POST` | `/api/cards/{id}/cancel` | Cancel an unused card (`require_permission(payment.void)` — admin/cfo by default, **not** `ap_manager`; see § Cancel → The gate is `payment.void`) |
| `POST` | `/api/cards/webhook/{provider}` | Receive charge/settlement webhooks (public-by-design, HMAC-gated) |
| `GET` | `/api/cards/rebates` | List rebates (paginated `page` / `page_size`, optional `period`) — see § The rebate list is paginated |
| `POST` | `/api/cards/rebates/{id}/confirm` | Advance a rebate `pending` → `confirmed` (admin/ap_manager/cfo) — see § Rebate status lifecycle |
| `POST` | `/api/cards/rebates/{id}/mark-paid` | Advance a rebate `confirmed` → `paid_out` (admin/ap_manager/cfo) — see § Rebate status lifecycle |
| `GET` | `/api/cards/dashboard` | Card program KPIs |

### Rebate status lifecycle

`CardRebate.status` (`pending` → `confirmed` → `paid_out`) never advanced past
`pending` — nothing transitioned it. Real rebate confirmation/payout from
Lithic/Nium arrives out-of-band (a periodic statement, not a webhook event
already ingested here), so the settlement webhook can only ever create a
rebate at `pending`; it can't itself learn when the processor later confirms
or pays it out. Found by exploratory persona-driven testing (card-processor
persona); recorded as a "minor / out of scope" gap since it wasn't a money
correctness bug (nothing was miscounted, the feature was simply never wired
end-to-end).

The two endpoints above give AP a human-driven way to record that
confirmation/payout when it happens (e.g. reconciling against the processor's
statement): `confirm` requires `pending`, `mark-paid` requires `confirmed` — a
rebate can't skip straight to `paid_out`. Both 404 on an unknown rebate, 409
on a status that isn't the required predecessor, and write an append-only
`card_rebate.confirmed` / `card_rebate.paid_out` audit row.

#### The UI

The `/payments` **Cards** tab is the surface (`frontend/src/routes/payments/
+page.svelte`, over `#lib/api/cards.ts` + `#lib/types/cardRebate.ts`). It
renders a **Rebates** table — period, card, rate, amount, status — above the
cards table, and offers **exactly one** transition per row: `Confirm` on a
`pending` rebate, `Mark paid out` on a `confirmed` one, and nothing at all on
`paid_out`. The offered step is derived from the row's status
(`nextRebateTransition`), so the UI never proposes a transition this endpoint
would 409 — but the backend stays the authority, and a 409 from a row that went
stale under a concurrent update renders **persistently** in the dialog
(`[data-testid="rebate-error"]`) rather than fading in a toast.

Both actions are confirm-then-act through the same shared `Modal` the
compliance-hold and settlement-accept controls use — but deliberately **not**
armed or danger-tinted, and the dialog copy leads with the fact that **neither
transition moves money**: it records what the processor already did on its own
statement. `Mark paid out` is the one control here that is easy to misread as a
button that pays someone, so it says in the dialog that the payout has *already
landed* (e.g. as a line on the org's own bank statement) and that this is a
bookkeeping record of something that happened outside the app.

Read and write share ONE role gate (`admin` / `ap_manager` / `cfo`) — there is
no role that can see the table without being able to act on it — so the UI
mirrors it with `auth.hasAnyRole(...)`. `/payments` is nonetheless reachable by
a custom role holding only `payment.execute` / `payment.void` (see
`frontend/src/lib/nav.ts`), which is why the check is still made client-side.

**A rebate row states the currency of the card that earned it.**
`card_rebates` has no currency column, so a rebate's currency is knowable only
through its `virtual_cards` row — which is why every path that returns a rebate
(`list_rebates`, `confirm`, `mark-paid`) joins that table and resolves the code
through `currency_conversion.card_currency_sql`, the one owner of that
expression (it uppercases: the column is a free-form `varchar(3)`). The result
is `RebateResponse.currency`, a **required** field with no default — the org's
reporting currency is exactly the wrong guess for a foreign-currency card.

This replaced a rendering that was honest but useless. `RebateResponse` used to
carry no currency at all, so the UI could only infer one from the envelope: a
**zero** `excluded_rebate_count` proved the listed rows were homogeneous and
could be labelled, and a **non-zero** count meant at least one was not and
nothing said which — so a mixed-currency programme rendered every amount as a
bare figure with no code. The table now labels every row under its own code,
mixed list included.

`excluded_rebate_count` survives with a narrower job: `total_amount` is a
SINGLE-currency figure (rows outside `currency` are filtered out of it, not
converted — an FX fetch on a list read would make a historical realised figure
non-deterministic), and the count is what stops that total reading as complete
beside a mixed list.

### The rebate list is paginated

`GET /api/cards/rebates` returns a PAGE on the canonical `page` / `page_size`
contract (`app/api/pagination.py`), like every other list here. It used to
return everything, and the table it backs grows by one row per settled card
forever, so the response size was a function of how long the tenant had been
running.

Two totals, deliberately named apart:

| Field | What it is |
|-------|-----------|
| `total` | Row COUNT of the whole filtered set — the canonical list envelope, what the Load-more control counts against. It used to be the money, which made this the one list endpoint whose `total` meant something different from all the others. |
| `total_amount` | Summed rebate amount over that same WHOLE filtered set, in `currency` — never over the loaded page. |

Both come from `_rebate_list_filters`, the one builder the row query, the count
and the rollup all pass through, so the figure and the set it claims to describe
cannot drift — the defect family `docs/decisions.md` §79 / §82 records, and the
reason `backend/tests/test_whole_set_kpi_rollups.py` exists. **That guard does
not discover this surface**: it pairs a list with a `GET .../summary` sibling,
and this rollup rides inside the list envelope instead. Ordering is
`created_at DESC, id DESC` — `created_at` alone is not a total order, and a tie
split across a page boundary drops one row and repeats another.

E2E: `frontend/tests-e2e/payments/rebate-lifecycle.spec.ts` — both transitions,
the terminal row, the persistent 409, per-row currencies on a mixed list, the
whole-set total under a single loaded page, Load-more, the hidden control for a
clerk, and the real backend's 403 on all three routes. Backend:
`tests/test_list_pagination_rebates_inspections.py` +
`tests/test_rebate_currency_denomination.py`.

### Dashboard "Rebates Earned" is REALIZED money only

`GET /api/cards/dashboard`'s `rebates_this_month` / `rebates_ytd` /
`projected_annual_rebates` used to blend `pending` + `confirmed` + `paid_out`
`CardRebate` rows into one SUM with no breakdown — so 100% of a displayed
"Rebates Earned" figure could be entirely unconfirmed money that might never
be paid out. Each is now the sum of `confirmed` + `paid_out` rows ONLY (the
two statuses the lifecycle above calls realized), with the full split
surfaced alongside on `rebates_this_month_by_status` /
`rebates_ytd_by_status` — `{pending_total, confirmed_total, paid_out_total}`,
computed in the same query via `case()` rather than a second round trip. The
`/payments` Cards tab renders the realized headline plus a muted "+{amount}
pending confirmation" hint whenever the pending bucket is non-zero, so a large
unconfirmed balance is visible rather than silently inflating the headline —
and the Rebates table below it is where that pending balance is actually
advanced (§ Rebate status lifecycle → The UI). Each KPI is denominated in the
`currency` the response itself declares, not the client's own resolution of the
org's reporting currency.
`tests/test_card_dashboard.py` pins the split with a mix of all three
statuses.

## Security

- **PAN-reveal role gate**: `GET /api/cards/{id}/details` is `require_roles(admin, ap_manager, cfo)`
  at the route decorator — the single source of truth (matches every other
  endpoint in `cards.py`, including the money-adjacent rebate `confirm`/
  `mark-paid` mutations). `ap_clerk` is refused. This is consistent with the
  read/write split elsewhere in the codebase (e.g. `positive_pay._READ_ROLES`
  includes `cfo`, `_WRITE_ROLES` doesn't) — `cfo` gets read access to
  sensitive financial data, including PAN reveal, as an oversight role.
- Full card numbers are only shown on explicit request (with audit log entry)
- Card details are never stored in our database — retrieved from provider on demand
- **The vendor-facing PAN reveal is single-use under concurrency.**
  `GET /portal/cards/{token}` (public-by-design — the emailed token is the
  credential) claims the `card_reveal_tokens` row with one atomic
  `UPDATE … SET used_at = now() WHERE token_hash = … AND used_at IS NULL …
  RETURNING card_id`, so simultaneous requests carrying the same token cannot
  both pass the single-use check, and the claim is **committed before** the
  outbound provider call — a slow, failing, or crashing provider round-trip can
  never leave an already-revealed link re-usable. Fail-closed: a degraded reveal
  still spends the link. Full semantics in
  [supplier-portal.md](supplier-portal.md) § Single-use virtual-card reveal.
- Cards have strict spending limits matching invoice amounts
- Cards auto-expire after configurable period (default: 30 days)
- Declined charges trigger alerts
- All card operations require authentication
- Provider API keys stored encrypted in org settings (future: secrets manager)
- **Card issuance moves money, so it's gated like any other payment rail.**
  There are two mint entry points — `POST /api/cards/generate` (direct,
  admin/ap_manager/cfo) and the `virtual_card` leg of `execute_payment_run`
  (`api/payments.py`) — and both route through the same
  `services/card_issuance.py::issue_card_for_invoice` helper so the gates
  can't drift between them:
  - **Approval gate** — only invoices in `PAYABLE_INVOICE_STATUSES`
    (`approved` / `posted_in_erp` / `payment_scheduled`, the same set the
    payment queue uses) are eligible; an unapproved invoice is silently
    excluded from the batch, never minted.
  - **Compliance gate** — `services/compliance.check_payment_compliance`
    (sanctions/KYC/AML) runs per invoice before the adapter call, exactly
    like the ACH/wire path; a `hold`/`refuse` verdict — including a vendor
    with `payments_blocked=True` — skips that invoice without minting.
  - **Claim gate (the other direction)** — the direct endpoint books no
    `Payment` row and leaves the invoice `approved`, so a card minted there is
    a money claim nothing keyed on `Payment` could see, and the invoice stayed
    payable by ACH: the vendor held a card for the face amount *and* received a
    wire. Every path that books a payment now refuses an invoice holding a live
    card unless the rail is `virtual_card` (which converges on it) — see
    `payments.md` § A live card is a claim on its invoice. Cancelling the card
    releases the claim.
  - **Audit trail** — a successful mint writes a `card.generated` audit row
    (invoice id, last_four, string-Decimal `amount_limit`) via
    `dispatch_audit`, matching every other card-lifecycle event
    (`card.cancelled`, `card.charged`, `card.settled`, `card.details_viewed`).

### Audit trail (every card state change is logged)

Card lifecycle transitions write an append-only `audit_log` row
(`entity_type="virtual_card"`) so the money + PII path is fully
reconstructable for SOX. **No row ever carries the full PAN or CVV** —
the `details` payload records only `last_four` plus the from/to status
and (for charges/rebates) the exact **string-Decimal** amount, never a
float.

| Action | When | `details` |
|---|---|---|
| `card.details_viewed` | PAN reveal (`GET /{id}/details`) | `last_four` |
| `card.revealed_via_token` | vendor-facing single-use PAN reveal (`GET /portal/cards/{token}`) — written when the token is **claimed**, committed before the provider is called, `actor_id=None` (no internal user) | `last_four` |
| `card.cancelled` | manual cancel (`POST /{id}/cancel`) | `last_four`, `from`, `to` |
| `card.charged` | authorization webhook applies a charge | `last_four`, `from`, `to`, `amount_charged` (string Decimal) |
| `card.settled` | settlement webhook completes + accrues the rebate | `last_four`, `from`, `to`, `rebate_amount`, `rebate_rate`, `rebate_base` (string Decimals), `rebate_base_source` (`settled` \| `charged` \| `unknown` — which figure the rebate priced off, see § Rebate base), `rebate_created` (bool — `false` if the one-per-card unique index skipped a duplicate) |
| `card_rebate.confirmed` | `POST /rebates/{id}/confirm` (`pending` → `confirmed`) | `amount` (string Decimal), `from`, `to` |
| `card_rebate.paid_out` | `POST /rebates/{id}/mark-paid` (`confirmed` → `paid_out`) | `amount` (string Decimal), `from`, `to` |

### Issue — idempotent at the provider, not just in our DB

A virtual card is spendable money, so issuance carries the money invariant
*idempotency on writes that move money* — and it needs **two** layers, because
they cover different failures:

| Layer | Mechanism | Catches |
|---|---|---|
| Ours | partial unique index `uq_virtual_cards_one_live_per_invoice` (migration `0067`) + a pre-check on **both** issuance entry points | a duplicate that reached our database |
| Provider's | a stable idempotency key on the create call | a card the provider made that **never** reached our database |

Both entry points — `POST /api/cards/generate` and the `virtual_card` leg of
`execute_payment_run` — pre-check for an existing live card
(`card_issuance.find_live_card_for_invoice`) before calling the provider, and
insert through `card_issuance.persist_card`, which flushes inside a SAVEPOINT so
a racer that claims the slot in between is a recoverable `False` rather than a
poisoned transaction. See *Persisting the row* below.

The provider layer is the one that matters for the nastiest case: `httpx` times
out *after* Lithic/Nium already provisioned the card. We hold no
`provider_card_id`, so the live card is orphaned and ungoverned — and the DB
index can't see it, because no row was ever written. An unkeyed retry then mints
a **second** live card. With a key, the provider replays the original response
and the retry converges on the card that already exists.

The two providers do **not** share a convention, so each adapter sends the key
on its own channel:

| Provider | Channel | Constraints |
|---|---|---|
| Lithic | `Idempotency-Key` header on `POST /v1/cards` | must be a valid **UUID**; keys retained 30 days |
| Nium | `x-request-id` header on the card-create POST | ≤255 chars; keys purged after 24 hours |
| mock | honours the key by deriving the card id from it (and echoes it on `raw_response`) | local-first: the retry path is exercisable with no provider account |

The key itself is minted by `services/card_issuance.py::build_card_idempotency_key`
— pure, deterministic, **never** a fresh `uuid4`:

```
uuid5(CARD_IDEMPOTENCY_NAMESPACE, f"virtual_card:{correlation_id or invoice_id}:{reissue_seq}")
```

- `correlation_id` (falling back to `invoice_id` for rows that predate it)
  anchors the key to the *payable*, so every attempt at the same issuance
  computes the same value.
- `reissue_seq` is how many `VirtualCard` rows the invoice already has — read
  from the tenant DB, which is why `issue_card_for_invoice` now takes `db`. A
  timed-out attempt persists nothing, so a retry recomputes the same sequence
  and therefore the same key. A deliberate **cancel-then-reissue** does leave a
  row behind, so it advances the sequence and gets a fresh key — without that,
  the provider would replay the original (now closed) card inside its retention
  window and the vendor would receive a dead card.

`VirtualCardPayload.idempotency_key` is `None`-able; an adapter given `None`
sends no key at all rather than inventing an unstable one (a per-attempt key
would give false confidence while still double-issuing).

#### Persisting the row — `persist_card`, and the `begin_nested` ordering trap

Every card insert goes through `card_issuance.persist_card(db, card)`, which
returns `True` when the row landed and `False` when a concurrent writer already
claimed the invoice's live-card slot. Both issuance entry points share it, so
the recovery semantics can't drift apart.

The reason it is a helper and not five inlined lines is a genuine SQLAlchemy
trap. `SessionTransaction._take_snapshot` **flushes the session** when a
`begin_nested()` boundary opens. So this:

```python
db.add(card)                 # WRONG — pending
try:
    async with db.begin_nested():   # ← flushes `card` HERE, before the SAVEPOINT
        await db.flush()
except IntegrityError:
    ...
```

issues the INSERT *before* the SAVEPOINT exists. The `IntegrityError` escapes
the block that was supposed to contain it, and — worse than an obvious crash —
it leaves the enclosing transaction in a needs-rollback state, so the *next*
statement on that session raises `PendingRollbackError` somewhere unrelated. The
`add` must be **inside** the block:

```python
try:
    async with db.begin_nested():
        db.add(card)
        await db.flush()
except IntegrityError:
    ...
```

No explicit `expunge` of the loser's row is needed: rolling back to the savepoint
runs `SessionTransaction._restore_snapshot`, which expunges `session._new` to
transient, so a later `flush`/`commit` cannot re-attempt the failed insert.
(`recurring_invoices.generate_one` relies on the same behaviour — the two
savepoints are deliberately identical in shape.) Regression coverage:
`tests/test_payment_card_duplicate_recovery.py` (both entry points, against a
real Postgres so the partial index actually fires).

### Cancel (`POST /{id}/cancel`) — provider-first + idempotent

#### The gate is `payment.void`, and that deliberately narrows `ap_manager`

This endpoint gates on `require_permission(PERM_PAYMENT_VOID)`. It is the **one**
route migrated off `require_roles` that does not reproduce its prior
admin/ap_manager/cfo matrix for the four system roles — and the mismatch was the
defect, not the migration.

Three routes close a virtual card: this one, `POST /api/payments/{id}/void`, and
`POST /api/payments/{id}/void/retry-card-cancel`. The latter two gate on
`payment.void`, which the default `ap_manager` does **not** hold (it holds
`payment.execute` — initiating money, not reversing it). This one gated on
`require_roles(ADMIN, AP_MANAGER, CFO)`, so an `ap_manager` who could not reverse
a card payment could still kill the card behind a live one, through the widest of
three doors onto the same effect. `docs/decisions.md` §132 already stated the rule
for the retry route — "not the card router's bare roles, which would hand an
`ap_manager` in a duty-split org the half of the reversal the org withheld" — and
this applies it to the door that was left open.

| System role | Before | After |
|---|---|---|
| `admin` | allowed | allowed |
| `cfo` | allowed | allowed |
| `ap_manager` | allowed | **403** |
| `ap_clerk` | 403 | 403 |

Nothing in the UI regresses: the route is deliberately unwired (`docs/decisions.md`
§96 — see the note under § Card Generation in Payment Run), so the supported
remedy for a live card behind a voided payment remains
`POST /api/payments/{id}/void/retry-card-cancel`. Pinned by
`tests/test_sod_endpoint_wiring.py::test_card_cancel_narrows_ap_manager_by_design`
and `::test_every_card_closing_route_gates_on_payment_void`.

#### Provider-first ordering

The handler cancels at the **provider first**, then reflects it in the DB — never
the other way round. The fail-safe direction is "dead at the provider, maybe
stale in the DB"; the dangerous one is a card the AP team believes is cancelled
while it is still chargeable. So the row is marked `cancelled` only once the
provider **confirms** the close (a raise → `502`, a non-confirming `False` →
`502`, no DB change).

The adapter `cancel_card` is **idempotent**: a card already closed/terminated at
the provider counts as a confirmed cancel (returns `True`), not a failure. Lithic
treats a `404`/`409` or a `200` echoing an already-`CLOSED`/`TERMINATED` state as
success; Nium treats a `404`/`409` on the block call the same; on any other error
both fall back to a live status check (`get_card_status` → `cancelled`),
otherwise stay `False` (fail-safe). This cleanly resolves the retry case where a
first cancel closed the card at the provider but the DB write failed and AP
retries — the second attempt confirms and marks the row cancelled instead of
erroring.

A **charged / completed** card cannot be cancelled (`409`) — the funds have
moved and no provider can un-spend them. That is the single most important
consequence of this endpoint's contract, because it means a spent card occupies
its invoice's live-card slot *permanently* (the partial index counts every
non-`cancelled` row). See "Settling against an existing card" below.

**Voiding a card payment cancels the card too.** `POST /api/payments/{id}/void`
calls the shared provider-side primitive `card_issuance.cancel_card_at_provider`
(same provider-first ordering) so a void actually stops the money instead of
only moving our books — a live card left behind is still bearer-spendable with
no payment naming it. Unlike this endpoint it is *best-effort*: a card-provider
outage is recorded as the `card_outcome` on the `payment.voided` audit row
rather than raised, because a provider outage must not block the accounting
void. It also travels on the response as `void_card_outcome` +
`void_card_disposition`, and a leg that left the card LIVE is retried through
`POST /api/payments/{id}/void/retry-card-cancel` — never through this endpoint,
which is reachable on a live payment and would leave the ledger and the card
disagreeing. See `payments.md` § Voiding a card payment cancels the card.

#### Settling against an existing card

`card_settlement_block(card, amount)` decides whether a payment may converge
onto a card it did **not** mint, and is deliberately separate from
`find_live_card_for_invoice`:

- `find_live_card_for_invoice` answers *"what occupies the slot?"* and must use
  exactly the index's predicate (`status <> 'cancelled'`). Narrowing it would
  make the pre-check miss a row the index still counts, so the caller would mint
  a provider card the index then refuses to persist — an orphaned spendable card.
- `card_settlement_block` answers *"can that card be what settles this
  payment?"* — `None` if yes, else a `Payment.failure_reason`. It rejects a
  card in `CARD_SPENT_STATUSES` (`charged`/`completed`) and one whose
  `amount_limit` cannot cover the payable.

The spend check is the load-bearing one: `amount_limit` is the authorization
ceiling and is **not** reduced by spend (a charge only sets `amount_charged`),
so a limit-only guard would mark a payment `completed` against a card whose
money already moved under a different, voided payment.

### Webhook handler (`POST /cards/webhook/{provider}`)

Provider charge/settlement callbacks are **unauthenticated** (they come
from Lithic / Nium, not a logged-in user) and verified by HMAC over the
raw body against the owning tenant's
`Organization.settings.cards.webhook_signing_secret`. The handler:

0. bounds the body against `card_webhook_max_bytes` (default 4 MiB) BEFORE
   buffering it — a declared `Content-Length` over the cap rejects without
   ever awaiting `request.body()`, and the actual read is re-checked in case
   the header lied or was absent (chunked transfer). The HMAC check can't run
   until the owning tenant is identified from the parsed body, so this is
   what stops an unauthenticated caller from having an arbitrarily large
   payload buffered fully into memory (memory-exhaustion DoS on a public
   route) — mirrors `erp_webhook` / `peppol_inbound` / `payment_webhook`,
1. parses `card_token` + `event_id` from the provider-specific body,
2. finds the owning tenant by `provider_card_id` AND `VirtualCard.card_provider
   == {provider}` (the URL path segment is a filter, not just a hint for
   which field-normalization branch to use — a card issued by one provider
   can't be matched by an event posted to the other provider's URL, even one
   carrying the same token value; defense-in-depth, since a real
   cross-provider token collision is independently negligible),
3. verifies the HMAC (`verify_hmac_sha256`) — missing/forged signature is rejected,
4. dedupes by `event_id` (`is_event_already_processed`, Redis `SET NX`) so a re-delivery is a no-op,
5. applies the state change + writes the audit row in a single committed transaction.

**Event classification (`_classify_card_event`)** decides whether a callback
is a charging authorization or a settlement by substring, but excludes the
non-charging variants FIRST: a `decline` / `reversal` / `void` / `refund` /
`return` / `cancel` / `expire` event is neither — the card is left untouched,
no charge and no rebate. (A naive `"auth" in event_type` match would otherwise
flip the card to `charged` on a *declined* `authorization.decline`.)

**Amount units differ by provider (`_normalize_charge_amount`)**: Lithic sends
charge amounts in **minor units** (cents — `150000` == `$1,500.00`), Nium in
**major units** (`50.00` == `$50.00`). The handler divides Lithic amounts by
100 and takes Nium amounts as-is, so a Nium charge is no longer recorded (and
rebated) at 1/100th of its value.

**Every rejection path returns `204` silently** (bad/missing signature,
unknown card token, missing event id, malformed JSON) — a distinct 4xx
would let an attacker enumerate card tokens or tenant slugs.

E2E coverage: `frontend/tests-e2e/cards/` — `webhook-security.spec.ts`
(HMAC reject + dedup + silent 204 + PII-free charge audit),
`lifecycle.spec.ts` (issue → cancel audit + 409 double-cancel),
`pan-reveal-pii.spec.ts` (role gate + last-four-only audit),
`rebate-and-isolation.spec.ts` (exact-Decimal rebate + tenant scoping).

## Code Structure

```
backend/app/services/card_adapters/
    __init__.py          # Package exports
    base.py              # CardAdapter base class, VirtualCardPayload, CardResult types
    dispatcher.py        # get_card_adapter() — REGION_DEFAULTS preference, gated on credentials
    mock_adapter.py      # Development/testing adapter
    lithic.py            # Lithic adapter (US/UK/EU) — interchange sharing from day one
    nium.py              # Nium adapter (40+ countries) — global coverage

backend/app/models/virtual_card.py    # VirtualCard + CardRebate SQLAlchemy models
backend/app/schemas/virtual_card.py   # Pydantic schemas
backend/app/api/cards.py              # API endpoints (generate, list, cancel, details, webhook, rebates)
```

## Implementation Status

| Phase | Status |
|---|---|
| Card adapter interface + dispatcher | Done |
| Mock adapter | Done |
| Lithic adapter (US/UK/EU) | Done |
| Nium adapter (global) | Done |
| VirtualCard + CardRebate models | Done |
| Card API endpoints (generate, list, cancel, details, webhook, rebates) | Done |
| Card config in organization settings UI | Done |
| Region-based auto provider selection | Done |
| Card list frontend (**Cards** tab on `/payments`, not a separate page) | Done |
| Card generation in payment run UI (`virtual_card` method in the run review; issued on execute) | Done |
| Vendor email notification (`card_issuance.notify_vendor_of_card` — single-use reveal link to `/portal/cards/{token}`) | Done |
| Vendor card acceptance tracking | Planned |
| Rebate UI (rebates table with confirm / mark-paid on `/payments`; Rebates Earned in the summary bar) | Done |
| Supplier portal integration | Planned |
| BIN sponsor graduation | Future (>$10M/month)

## Minor units respect the ISO-4217 exponent — on both halves

Two legs convert between major and minor units on the card rail, and they are
**exact inverses only if they resolve the same exponent**. Both now go through
`payment_adapters.base` — `to_minor_units` on the way out,
`minor_units_to_decimal` on the way back — which owns the one exponent table in
this codebase.

| Leg | Code | Direction |
|---|---|---|
| Card `spend_limit` | `card_adapters/lithic.create_card` | major → minor |
| Webhook charge amount | `api/cards._normalize_charge_amount` | minor → major (Lithic only; Nium reports MAJOR units) |

A flat `× 100` / `÷ 100` is right for the near-universal exponent of 2 and wrong
in both directions elsewhere: ¥150000 is ¥150,000 (exponent 0), not ¥1,500, and
150000 fils is 150 KWD (exponent 3), not 1,500.

The read half was migrated first and the write half was not, which left the
pair **asymmetric** — the state the base module explicitly warns is worse than
the original symmetric error, because it becomes a live mispricing rather than
a cancelling one. A ¥500,000 card went out with `spend_limit: 50000000`, i.e. a
**¥50,000,000** authorization ceiling the vendor could actually spend, while the
charge that came back was de-scaled correctly; `card_settlement_block` compares
only our own `amount_limit`, so nothing downstream could see it. 5.000 KWD is
the mirror image — 500 fils instead of 5,000, a 10x under-limit that declines a
legitimate charge. `to_minor_units` also rounds `ROUND_HALF_UP` where the old
expression truncated with `int()`.

Lithic is USD-only in practice today, so nothing currently in play was
mispriced — which is exactly why both halves were routed through the shared
table *before* a card provider or a non-USD card currency arrives rather than
after. On the read leg the card's own `currency` is passed at the call site; the
parameter stays optional (a webhook body need not carry one) and falls back to
exponent 2, i.e. the previous behaviour.

`card_dashboard`'s `rebate_ytd` is also bounded at both ends now. `period` is a
`YYYY-MM` string, so a bare `>= "{year}-01"` matched every FUTURE year too
("2027-03" sorts above "2026-01") — a forward-dated rebate row leaked into
year-to-date, and `projected_annual` divides that figure by months elapsed, so
it inflated the projection as well.

**Tests:** `backend/tests/test_card_charge_normalization.py`.

## Rebate base — settled, never authorized, never the limit

A rebate is earned on what actually **settled**. `resolve_rebate_base` in
`api/cards` returns that figure plus its provenance:

| Settlement amount | Authorized amount | Base | `rebate_base_source` |
|---|---|---|---|
| present | any | the settled figure | `settled` |
| absent | present | the authorized figure | `authorized` |
| absent | absent | `0` | `unknown` |

The settlement branch previously read `card.amount_charged or
card.amount_limit`, which was wrong twice over:

- `amount_charged` is stamped by the **authorization** event and was never
  updated at settlement, so the rebate was computed on the authorized figure
  while the settlement event's own `amount` sat unused in scope. Card
  settlements routinely differ from the auth they clear (partial capture, tips,
  fuel adjustments), and the processor pays rebate on what settled.
- The `or` fallback reached for `amount_limit` — the card's authorization
  **ceiling**, not spend. A settlement without a usable amount on a card whose
  auth was also missing rebated on the full limit: a $10,000 card that settled
  $100 earned a rebate on $10,000.

The settled figure is now also **persisted** to `amount_charged`, since that is
what the card detail, the spend rollups and the corporate-card feed all read,
and after settlement the settled figure is the true one. `rebate_base` and
`rebate_base_source` ride the append-only `card.settled` audit row so a
reconciliation against the processor's own statement can tell the cases apart
without re-deriving them.

## An expired card cannot settle a payment

`card_settlement_block` refuses a card whose `expires_at` has passed. Converging
a payment onto a pre-existing card marks the payment `completed` and advances
the invoice to `paid` — an assertion the money moved — so the card has to be one
that could actually have moved it.

The guard previously checked only spent-status and limit. An expired card is
reached exactly like the spent case (mint → payment voided → invoice back in the
payable pool → a later run rediscovers the card), except it aged out rather than
being redeemed, so the status check cannot see it. The payment went `completed`,
the invoice went `paid`, and the vendor was never paid at all — worse than the
double-spend the status check exists to prevent, because there is no charge
anywhere to reconcile against.

Ordering is deliberate: a spent card that also expired reports
`card_already_charged` (the money *did* move, and knowing against which payment
is the more actionable fact), and expiry is checked before the limit (raising a
limit would not make an expired card settleable). A card with no `expires_at` is
non-expiring — the column is nullable and several providers do not return one.

## Dashboard figures are single-currency

`GET /api/cards/dashboard` reports one `currency` code, and every money field in
it counts only rows denominated in that code, with `excluded_card_count` /
`excluded_rebate_count` disclosing what was left out. The rollups were
previously bare cross-currency `SUM`s over `amount_limit` / `amount_charged` /
`CardRebate.amount` presented as a single headline with no code at all.

`CardRebate` has **no currency column of its own** — a rebate's currency is only
knowable through the card that earned it — so the rebate rollups join
`VirtualCard` rather than guessing. Figures are filtered rather than converted:
they are historical realised amounts, and an FX fetch during a dashboard read
would make them non-deterministic.

**And they are scoped to the selected entity — rebates included.** The rebate
rollups (`rebates_this_month`, `rebates_ytd`, both status breakdowns and
`excluded_rebate_count`) go through `apply_entity_scope` on the `VirtualCard`
join, matching `GET /api/cards/rebates`, which has always scoped its list that
way. They previously did not: `CardRebate` carries no `entity_id` of its own, so
the entity was only reachable once the currency fix added that join, and until
then the dashboard reported entity-scoped card figures beside org-wide rebate
figures — a subsidiary's rebate showed as the parent entity's YTD, and the
headline could not be reconciled against the list an operator drills into.

A cross-entity rebate is excluded **silently**: the entity selector is itself
the disclosure, so only *currency* exclusions get a count. With no
`X-Entity-ID` (the consolidated read) `apply_entity_scope` is a no-op and every
entity is included, which is what keeps the group view whole.
