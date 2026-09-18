# Payment rails — paying your customers' vendors

**Why this matters**: This is the longest-lead-time item on the
critical path. Real ACH out of a real bank takes 3–6 weeks of KYB +
bank onboarding. Start this in **week 1** even if everything else is
parallel.

**But read this first**: only the intro call is startable before the
entity exists. KYB needs formation documents, an EIN and beneficial
owners; the bank account needs both. So `legal-entity.md` and this
runbook are one serial chain, not two parallel ones — the issue
tracker presents them as separate sections and that is the trap. Book
the call now anyway: it is free, and what the KYB packet asks for
tells you what the incorporation has to produce.

## Current state

Code-side: the Modern Treasury adapter is implemented and covered by
the backend suite. The payment-run flow (create → submit → webhook →
settle) works end to end against Modern Treasury's sandbox. See
`backend/docs/payments.md`.

What's missing is **the real bank relationship** — the thing Modern
Treasury fronts.

## You are not limited to Modern Treasury

Six payment adapters ship, all against the same `PaymentAdapter`
interface, so the choice below is a business decision and not an
engineering one. Switching later is a settings change, not a port.

| Adapter | Rails | Shape |
|---|---|---|
| `modern_treasury` | ACH, wire, RTP, check | Abstraction layer; you still onboard a partner bank underneath |
| `column` | ACH, wire, book transfer | A chartered bank with a direct API — no separate partner bank |
| `increase` | ACH, wire, check | Same: direct bank API, one relationship |
| `dwolla` | ACH only | ACH specialist, funding-source model |
| `stripe_treasury` | ACH, wire | BaaS on Stripe Issuing FinancialAccounts; no RTP |
| `checkeeper` | Check only | Prints and mails paper checks |

**Column and Increase collapse the two-step.** Modern Treasury is an
abstraction over banks, which means MT onboarding *and* bank
onboarding — two KYB processes, and the runbook's 3–6 weeks is mostly
the second one. A direct bank is one relationship and one KYB. MT
earns its place when you expect to need several banks, several rails,
or its ledgering; for pilot #1 on ACH alone, price the direct banks
before assuming MT.

The rest of this runbook uses Modern Treasury as the worked example
because it is the most involved path. The steps map onto the others
with the partner-bank stage removed.

## The three roles

1. **Modern Treasury** — an abstraction layer over banks. Gives you a
   clean API for ACH / wire / RTP / book transfers, reconciliation,
   ledgering. Pricing is **not published** — it is usage-based and
   quoted per customer on volume, rails and account structure, so get
   a written quote on the call rather than budgeting from a rumour.
2. **Your bank** — where your operating cash sits. Becomes the
   funding account for customer payments.
3. **Your customers' banks** — where customer cash sits. Each
   customer authorizes money movement from their bank account into
   their vendor's.

How money flows (typical "sender-pays" model):
```
Customer's operating bank account
    ↓ (NACHA Same-Day ACH pull, or wire)
Your pooled operating account at your bank (Mercury, Brex, etc)
    ↓ (ACH / wire out via Modern Treasury)
Vendor's bank account
```

Alternative: **direct funding** — the customer's own account is the
originating account on every payment, and you never take custody.
Lower regulatory burden; the customer has to authorize the transfers.

**Recommendation for pilot #1**: direct funding. Less money-movement
licensing exposure. Read the next section before treating that as a
licensing exemption — it is a reduction, not an escape.

## Step 1 — Decide sender-pays vs. direct funding

- **Direct funding**:
  - You never take custody of customer funds.
  - Customer links their bank; signs an ACH authorization per payment
    or a standing authorization.
  - Materially lower money-transmission exposure — but **not an
    automatic exemption**. Whether you are a money transmitter turns
    on *control* over the funds, not on whose name is on the account,
    and states differ on where that line sits. This is exactly the
    question the legal opinion in Step 4 answers. Do not record
    "direct funding" as a decision that closes the licensing question.
  - Simpler legally. Harder UX (every payment needs authorization).
- **Sender-pays**:
  - Customer prefunds a pooled account, typically FBO.
  - You move money on their behalf.
  - You are a money-service business in most states → FinCEN MSB
    registration + state-by-state money transmitter licences.
    Six figures a year and up, once you count the surety bonds.
  - Better UX. Higher legal cost. Generally not worth it until you
    are selling 7-figure ACV.

**Do not model this on Bill.com.** An earlier draft of this runbook
cited BILL as the direct-funding exemplar; it is the opposite one.
BILL is registered with FinCEN and licensed as a money transmitter in
47 US states and territories, and holds customer funds in FBO
accounts — it is the sender-pays model, carrying the full licensing
cost. If you want the direct-funding shape, design it deliberately
and get it blessed; there is no incumbent whose homework you can copy.

**For pilot #1: direct funding.** Revisit when you hit $1M ARR.

## Step 2 — Modern Treasury onboarding

1. Fill the Modern Treasury pre-sales form on their site.
2. First call covers: product fit, expected volume, compliance
   posture.
3. They send a Letter of Intent + pricing.
4. Sign MSA + DPA. Modern Treasury publishes its own DPA; whether
   they will paper on Common Paper's standards is a question for the
   call, not an assumption — an earlier draft of this runbook
   asserted they accept it, unverified.
5. Complete KYB (Know Your Business): entity docs, EIN, beneficial
   owners, expected transaction volume.
6. They help you pick + onboard a partner bank.

Time: 3–6 weeks.

### Four questions for the intro call

Ask these on the first call. Each one determines work you would
otherwise discover late:

- Under **direct funding**, does each customer's bank account become
  an originating account under our MT organization, and does MT's own
  account verification cover the linking — or do we need our own
  aggregator (Plaid-style) relationship? *(This decides whether the
  banking-aggregator item on the critical path is a blocker here or
  only powers balance display.)*
- Which partner banks, and what is each one's KYB turnaround?
- Do you accept Common Paper's MSA/DPA, or do we paper on yours?
- Which SEC codes will we be authorized for — PPD, CCD, WEB?

### On partner banks

Whoever MT proposes, check the bank is currently accepting fintech
programs before you invest weeks in its KYB. This sector churns
hard, and two names this runbook recommended until 2026-09-17 are no
longer options:

- **Silicon Valley Bank** failed in March 2023 and was bought out of
  FDIC receivership by First Citizens. It is a division of First
  Citizens now, not an independent partner bank.
- **Blue Ridge Bank** took an OCC consent order over its BSA/AML
  program in January 2024 and **exited banking-as-a-service entirely
  at the end of 2024**, offboarding roughly 70 fintech partners. The
  order was terminated in late 2025, but the BaaS programme is gone.

**Column** and **Treasury Prime** remain live, and both have shipped
adapters or bank networks. Treat any named bank in a runbook — this
one included — as a lead to verify, not a recommendation to act on.

## Step 3 — Bank onboarding

The partner bank (not MT) actually moves the money.

1. Open an operating account with the partner bank. Typically a
   checking account + the ACH origination agreement.
2. Set up NACHA origination: they give you an ODFI ID + tell you
   which SEC codes (PPD, CCD, WEB) you're authorized for.
3. Set ACH limits — daily/monthly caps they'll allow. Start low
   ($50K/day) and raise as volume grows.
4. Sign the wire agreement separately if you want wires.
5. Get a test account for sandbox. MT ties it to their sandbox.
6. **Confirm the ODFI has registered you as a Third-Party Sender**
   with NACHA — see the next step for why that identity matters.

## Step 4 — Regulatory posture

Direct funding lowers your money-transmission exposure. It does **not**
exempt you from the ACH rules, because the moment you transmit entries
on behalf of a customer through an ODFI you are a **Third-Party
Sender**, whatever the funding model. That identity carries
obligations that are *yours*, not Modern Treasury's:

- **Third-Party Sender registration.** The registration is filed by
  your ODFI, not by you — but you have to confirm it happened, and
  the details must be updated within **45 days** of any change.
- **Annual ACH Rules Compliance Audit, by December 31 each year.**
  This is the obligation an earlier draft of this runbook described
  as "annual attestation — MT runs this for you". Both halves were
  wrong. It is a Rules Compliance Audit, and as a Third-Party Sender
  it is your audit: you must be able to produce proof of completion
  to your ODFI on request. Failure is a Class II Rules violation,
  fineable up to **$100,000 per month** until resolved. Budget for a
  third-party auditor; MT and your bank can support the audit, but
  neither discharges it.
- **Risk-based ACH fraud monitoring — already mandatory.** Since
  **22 June 2026**, NACHA's risk-based fraud-monitoring requirement
  covers *all* remaining Third-Party Senders regardless of volume.
  You must run a risk-based process to detect entries initiated due
  to fraud, screening outbound entries before they enter the network,
  and the monitoring must be **documented and reviewed annually** as
  part of the December audit. Scope this when you scope the audit;
  it is in force today, not a future deadline.
- **OFAC screening.** MT screens counterparties, and that is a
  backstop — not your screening. The app runs its own compliance gate
  on the payment path (`api/payments.py`, the sanctions / KYC / AML
  check before execution) through `services/sanctions_adapters/`,
  which fails closed and needs a real provider key
  (ComplyAdvantage / Dow Jones / Refinitiv) to do anything but mock.
  A live rail with a mock screening adapter is the worst of both:
  real money, no list check. Land the provider key in the same
  window as the rail.
- **Returns and reversals.** NACHA R-code handling is **already
  implemented** — an earlier draft called this a code gap. The
  adapter maps MT's `returned` status to a failed payment and lifts
  the reason out of `return.reason_code` / `reason_description`, so
  a return surfaces on the Payment row with its R-code rather than
  silently sticking. `services/payment_runs.classify_payment_failure`
  is what decides whether `/retry-failed` may re-attempt it.
- **State money transmission**: get a legal opinion letter confirming
  your model is not money transmission. This is the question Step 1
  defers to here — the opinion is what makes "direct funding, so no
  licensing" a defensible position rather than an assumption.
  Skipping it is a reasonable risk for pilot #1 on direct funding,
  but don't raise a Series A without one. Budget $5–15K from a
  payments lawyer.

## Step 5 — Production config

Once Modern Treasury + bank are live, set `Organization.settings.payments`
for the tenant. **The keys are flat and unprefixed** — an earlier draft
of this runbook showed `mt_api_key` / `mt_originating_account_id` /
`mt_webhook_secret` and omitted `org_id` entirely, which is not what
the adapter reads. Following it produced HTTP Basic auth of `("", "")`
and a failed live payment with nothing in the response explaining why:

```json
{
  "provider": "modern_treasury",
  "org_id": "org_...",
  "api_key": "live_...",
  "originating_account_id": "internal_account_...",
  "webhook_secret": "..."
}
```

`backend/docs/payments.md` § Payment processor adapters is the
authoritative shape; this block is a copy and defers to it on any
disagreement. Two notes on keys you may see elsewhere: `sandbox` is
**inert on the payments path** (MT selects sandbox by credential set,
not by URL or flag — the card adapters are what actually read a
`sandbox` key), and `program_type` is not read by any payment adapter
either. Neither does harm; neither does anything.

Then:

- **Verify the credentials before moving money**: `POST
  /api/organization/test-payments`. Do not skip this. A typo'd
  provider name is *named back to you* there, which matters because
  the dispatcher's `mock` adapter reports every payment as
  `completed` without moving money — a single wrong character used
  to flip a whole run to settled and mark the invoices paid.
- For direct funding, the `counterparty_id` on each vendor is the
  vendor's verified bank account; the org's `originating_account_id`
  is the **customer's** account, not yours.
- Register the webhook in the MT dashboard:
  `https://api.feohledger.com/api/payments/webhook/{tenant_slug}/modern_treasury`
- First real payment: pick an internal invoice (pay yourself from
  the operating account to your personal account) before touching
  customer money. Confirm the webhook actually drove
  `submitted → processing → completed` and the invoice reached
  `paid` — the point of the exercise is the webhook, not the debit.

## Checklist

The five bold items are the ones mirrored in `status.md` and issue
#446; the rest roll up under them. Keep the three reconciled.

- [ ] Decision: direct-funding or sender-pays (recommend
      direct-funding for v1)
- [ ] Provider chosen — MT vs. a direct bank (Column / Increase),
      priced against each other
- [ ] **Modern Treasury intro call completed** (four questions above asked)
- [ ] **KYB paperwork submitted**
- [ ] Partner bank selected — confirmed currently accepting fintech programs
- [ ] **Partner bank account opened**
- [ ] **NACHA origination agreement signed**
- [ ] Daily/monthly ACH limits set
- [ ] Third-Party Sender registration confirmed filed by the ODFI
- [ ] Annual Rules Compliance Audit scheduled (December 31 deadline)
- [ ] Risk-based ACH fraud monitoring documented (mandatory since 2026-06-22)
- [ ] Sanctions provider key live — not the mock adapter
- [ ] State MTL legal opinion (if enterprise pipeline demands it)
- [ ] Test payment through sandbox
- [ ] `POST /api/organization/test-payments` green against live credentials
- [ ] **First real payment out successful**
- [ ] Webhook → payment status transitions verified in prod

Time: 4–8 weeks calendar; start on day 1.
Cost: MT quoted per customer (not published) + ~$5–15K for the legal
opinion + bank account fees + the annual ACH audit. An earlier draft
of this runbook footed "~$50K legal", contradicting its own $5–15K
figure two sections above; neither number was sourced.

## Sources

External claims in this runbook were verified 2026-09-17:

- [Nacha — ACH Rules Compliance Audit requirements](https://www.nacha.org/rules/ach-rules-compliance-audit-requirements)
- [Nacha — Third-Party Sender registration](https://www.nacha.org/rules/third-party-sender-registration)
- [Nacha — risk management / fraud monitoring phase 2](https://www.nacha.org/rules/risk-management-topics-fraud-monitoring-phase-2)
- [Regions — what Third-Party Senders should know about annual ACH audits](https://www.regions.com/insights/commercial/article/third-party-sender-ach-audit)
- [BILL — licences and authorizations](https://www.bill.com/legal/licenses)
- [Banking Dive — OCC terminates Blue Ridge Bank consent order](https://www.bankingdive.com/news/occ-terminates-blue-ridge-bank-consent-order/805617/)
- [First Citizens — Silicon Valley Bank acquisition](https://www.firstcitizens.com/m-a/svb)
- [Modern Treasury — pricing](https://www.moderntreasury.com/pricing)
