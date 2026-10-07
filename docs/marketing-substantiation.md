# Marketing substantiation

How every specific figure on the public marketing page is derived, so a
challenge is answered from a record rather than a re-derivation. A specific
number is an objectively verifiable factual claim, not puffery, and the page has
published invented ones before (below, § Withdrawn).

**Scope:** the no-tenant landing page — `frontend/src/lib/components/marketing/`
(`Landing.svelte`, `Pricing.svelte`, `HeroPipeline.svelte`, `AdapterRail.svelte`).
The published legal pages are governed separately (`docs/decisions.md` §175).

**The rule:** a number on the page either comes from a generated module, or has
a row here naming its source. Change a figure and its row in the same commit; if
the source count moves, change the figure. A figure with no derivation is
removed, not kept "until someone checks". Last re-derived: 2026-10-07.

## Pricing — generated, not substantiated by hand

Every price, AI-read allowance, overage rate and feature on the pricing grid is
**generated from the plan catalogue**, so it cannot say anything the billing
code does not:

| Figure | Source | Guard |
|---|---|---|
| Monthly price per tier | `DEFAULT_PLAN_CATALOG[*].monthly_price` in `backend/app/services/billing/plan_catalog.py` | `pnpm check:pricing` (CI, Backend lint) |
| Currency | `plan_catalog.CATALOG_CURRENCY` — the code `ensure_plan_catalog` stamps on every `Plan` row | same |
| AI-read invoices included | `usage_components["ai_invoices"]["included"]` | same |
| Overage per extra AI-read invoice, or "AI reading pauses" | `usage_components["ai_invoices"]["overage_unit_price"]` (`null` = pause) | same |
| Features per tier | `entitlements`, keyed by `plan_catalog.ALL_FEATURES` | same + `pnpm check` (a feature with no label is a type error) |
| "Unlimited users" | No catalogue plan carries a seat entitlement, and the generator refuses any entitlement key outside `ALL_FEATURES`, so a seat cap cannot be added without the page failing to generate | same |

`scripts/gen_pricing_catalog.py` writes `frontend/src/lib/marketing/plans.generated.ts`;
`lib/marketing/pricing.ts` turns it into the grid; `pricing.test.ts` refuses a
digit in the template or in any pricing string in any locale, so no figure can
be typed in beside the generated ones. The tiers themselves are decisions §253.

**What the grid claims about behaviour, and where it is implemented.** "Past
that, AI reading pauses. Everything else keeps working" (Free) and the per-tier
feature gates are the §253 design. The catalogue that states them landed with
§253; the extraction-side pause and the entitlement checks are separate work. A
claim of the form "Free does not include X" must not be relied on as a marketing
point until the gate for X exists — until then the page errs in the customer's
favour (a feature is available, not withheld), which is the safe direction.

**Enterprise** shows no figure: "Custom", "negotiated contract". Its two bullets
are qualitative (negotiated terms; help with a security review — answered from
`docs/soc2-readiness.md` and the published legal set).

## Landing page figures

| Claim | Figure | Derivation | Note |
|---|---|---|---|
| "payment rails, ACH to CHAPS" | **11** | `payment_methods.KNOWN_PAYMENT_METHODS` in `backend/app/services/payment_methods.py`: `ach`, `wire`, `check`, `virtual_card`, `bacs`, `faster_payments`, `chaps`, `rtp`, `sepa`, `international_ach`, `international_wire` — every rail the platform models and records payments on. | Raised from 7 on 2026-10-07 at the operator's call. Seven are user-selectable on a payment (`PAYMENT_METHODS` in `frontend/src/lib/types/payment.ts`); `rtp`, `sepa`, `international_ach` and `international_wire` are reached through the international corridor selector. If a rail is removed from `KNOWN_PAYMENT_METHODS`, lower this figure in the same change. |
| "workflow step types" | **9** | `backend/app/services/workflow_step_types.py`: `CANONICAL_STEP_TYPES` (4: extraction, approval, erp_export, done) + `BUILDER_STEP_TYPES` (5: condition, parallel, webhook, email, delay). | The three backwards-compatible aliases are not counted. |
| "languages, fully localized" | **6** | `SUPPORTED_LOCALES` in `frontend/src/lib/i18n/locale.ts` (en, de, fr, es, pt-BR, ja); `messages_parity.test.ts` fails a locale missing any key. | "Fully" excludes the two documented English-only surfaces: the legal text (§174) and the help-centre guide prose (§244). |
| "typical rebate on card payments" | **1–2%** | Lower bound: `_DEFAULT_REBATE_RATE = 0.0100` in `backend/app/api/cards.py`, the rate applied with no negotiated rate on file. Upper bound: an org-negotiated `settings.cards.rebate_rate`, which the same function accepts up to 10%. | The 2% is a typical negotiated rate, not a platform default; the qualifier "typical" and the "Illustrative" note beside the worked example carry that. Rebates depend on the issuer program, so this is the figure most worth re-checking when a card issuer is actually contracted. |
| "$500k/month → $60–120k/yr" | worked example | $500,000 × 12 months × 1% = $60,000; × 2% = $120,000. Pure arithmetic on the rebate row above, assuming all spend moves to card. | Labelled "Illustrative" in the markup beside it, with the three variables that move it. |
| "Free to start · No credit card" | — | Signup (`/api/signup/start`, `/complete`) asks for no payment method, and every new workspace is bound to the first catalogue plan, priced at zero (`tenant_provisioning` → `ensure_subscription`). | Re-check if signup ever gains plan selection or a card step. |
| Hero card (vendor, invoice number, amount, due date, confidence bars) | — | A drawing of an invented invoice (`HeroPipeline.svelte`), `aria-hidden`; confidences render only as bar widths, never as numbers. | Not a performance claim. Never add a numeric confidence or timing to it. |
| Adapter rail | — | Provider names only, each a registered adapter in this repo (`AdapterRail.svelte`'s own comment). | Names, not customers. |

Qualitative copy ("in minutes, not days", "in minutes") makes no specific
numeric claim and is not tracked here.

## Withdrawn

Kept so the same figure is not reintroduced without a source.

| Figure | Why withdrawn |
|---|---|
| "3.2s avg. extraction time" | No benchmark, eval or fixture behind it anywhere in the repo. |
| "97% field accuracy on typed invoices" | No benchmark. The only 97% in the tree is Basware's published *touchless processing rate* in `docs/competitive-analysis.md` — a competitor's number for a different metric. **Publish no accuracy figure until a real extraction benchmark exists**, with its method recorded here. |
| "12 workflow step types" | Reached 12 only by counting three aliases; 9 is the honest count. |
| "Provision in 30 seconds" / "Spin up your workspace in 30 seconds" | Never measured, and the flow includes the visitor verifying their email, which no timing of ours can promise. Replaced with "Self-service signup" / "in minutes". |
| "SOC 2 attestation", "99.9% uptime SLA" | Neither exists (removed with the legal-pages change). |
| Per-seat pricing ($29/$24 per seat, 5-seat minimum), annual toggle, "50 invoices / month, 2 seats" free caps, "Start 14-day trial", "Most popular" | The billing code modelled none of it (issue #426). Replaced by the generated grid above; nothing grants `trial_days` yet, and there is no customer data behind a popularity badge. |
