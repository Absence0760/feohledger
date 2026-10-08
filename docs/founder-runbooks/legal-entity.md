# Legal entity + contracts

**Why this matters**: you cannot legally invoice a customer until you
exist as a business, and you should not take customer payments into a
personal bank account. A sole proprietor *is* a business for both
purposes — it just has no separate legal person behind it.

## Current path: sole proprietor (decisions §252)

**Decided 2026-10-07: the pilot is signed by the operator as a sole
proprietor, in their own name — "Jared Howard" — with FeohLedger as the
product's name.** Every contract on the critical path — Stripe, insurance,
the EU/UK Art 27 representatives, the SCCs with each sub-processor, a SOC 2
vendor, counsel — can be signed in your own name now. Nothing waits on
incorporation.

The legal pages already reflect this (`frontend/src/lib/legal/operator.ts`):
no separate company, no published postal address — they state "based in
Virginia, United States" and make email the contact channel — and no DPO.

**Required:** only E&O + cyber insurance, before customer #1, and a local
business licence if your city or county requires one. **Everything else in
the table is optional.**

| Step | Why | Notes |
|---|---|---|
| Bind **E&O + cyber insurance** — **required, hard gate before customer #1** | With no entity, a claim outside the Terms' cap reaches personal assets | See `insurance.md`. Tell the broker you are a sole proprietor |
| State/local business licence — **required only if** your city or county requires one | Varies | Check with your city/county |
| _Optional:_ register **FeohLedger** as a trade name (DBA / "assumed name") | Needed only if you invoice, contract or bank **as "FeohLedger"** rather than in your own name | County clerk or state. Usually $10–100. Not needed while contracts and invoices say "Jared Howard" |
| _Optional:_ get an **EIN** from the IRS | Keeps your SSN off customer W-9s, Stripe and the bank | Free, online, minutes |
| _Optional:_ open a **separate business bank account** | Clean books; Stripe payouts land somewhere that is not personal | A personal-name account works without a DBA |
| _Optional:_ a **mailing address** that is not your home | Only if you later want to publish one — the pages are email-only today | Virtual mailbox or PO box; then set `postalAddress` in `operator.ts` |
| Sales-tax nexus review | Same as for a company | See Step 2 below — not urgent for pilot #1 |

Steps 3 (contracts) and 4 (lawyer review) below apply unchanged. Steps 1,
5 and 6 apply only if you form a company.

**When to form an entity** (decisions §252): a customer's procurement
requires it; a customer wants FeohLedger to move money itself (payment-rail
KYB needs a business — `payment-rails-onboarding.md`); you raise money; or
your exposure outgrows what insurance comfortably covers. A **single-member
LLC** is the cheap step (state filing ~$50–500, taxed as a sole proprietor
by default); the Delaware C-corp below is for fundraising. Terms §1 already
allows assigning customer agreements to a company formed later.

---

The rest of this runbook is the **company** path, kept for when one of
those triggers fires.

## Step 1 — Incorporate _(company path only)_

Default choice for a US SaaS: **Delaware C-corp**, formed through
Clerky or Stripe Atlas.

| Option | Cost | Time | Notes |
|---|---|---|---|
| Clerky | $427 company setup (+$299 post-incorporation, or $819 lifetime) + DE fees | 1–2 weeks | Cleanest for fundraising. Includes 83(b), stock issuance, bylaws. |
| Stripe Atlas | $500 one-time, **then $100/yr** | 1 week | Includes government fees and year 1 of registered agent; the $100/yr is that agent renewing. Bundles an FDIC bank account + EIN. Slightly fewer legal docs than Clerky. |
| Firstbase | $399 | 1–2 weeks | Similar to Atlas. |

Prices verified 2026-09-17 and they move — confirm on each provider's own
page before filing. This table carried a flat "$500" for Clerky until then.
| DIY via DE Division of Corps | ~$200 | 1–4 weeks | Don't. The $300 you save costs days of admin pain. |

**If you're solo and not fundraising soon**: Atlas is faster to launch.
You can migrate to Clerky later.

**If you plan to raise in the next 6 months**: Clerky from day one.

## Step 2 — EIN, bank account, and tax

Atlas/Clerky both obtain the EIN for you. You'll also need:

- **Business bank account** — Mercury is the default startup choice
  (no minimums, clean API, SOC 2-compliant). Atlas bundles one.
- **State registrations** — Register to do business in your home state
  if different from Delaware. Clerky's add-on handles this for ~$200.
- **Sales tax nexus review** — SaaS sales tax rules vary by state. If
  you have customers in multiple states, use TaxJar or Anrok. Not
  urgent for pilot #1; do it before customer #3.

## Step 3 — Core contract library

You need these before your first customer signs:

| Document | Source | Notes |
|---|---|---|
| Terms of Service | [Termly](https://termly.io) / [Common Paper](https://commonpaper.com) | B2B SaaS template; have a lawyer review before signing with a $50K+ ARR customer. |
| Privacy Policy | Termly / Common Paper | Must include GDPR + CCPA sections even for US-only customers. |
| Data Processing Agreement (DPA) | Common Paper's [DPA standard](https://commonpaper.com/standards/data-processing-agreement/) | Every enterprise customer will ask for this. Sign their DPA or provide yours. |
| Master Services Agreement (MSA) | Common Paper's [standards index](https://commonpaper.com/standards/) | The customer-facing commercial contract. Standard terms, negotiable attachments. |
| Order Form / SOW | Template inside Common Paper | Per-customer commercial details — price, users, term. |
| Mutual NDA | Common Paper's mNDA | For conversations that precede the MSA. |

Common Paper is free. Use it. Rewriting B2B SaaS contracts from scratch
is a $15K legal bill with zero differentiation.

## Step 4 — Bind review with a lawyer

Before the first contract you sign for > $25K ARR, pay a startup lawyer
$2–5K to review:
- Your TOS + Privacy + DPA
- The customer's redline of your MSA
- Liability caps + indemnification clauses

Startup-friendly firms: Cooley GO, Fenwick, Wilson Sonsini, Gunderson.
Most will do flat-fee first-round reviews.

## Step 5 — Founder paperwork _(company path only)_

Don't skip these — they have one-way consequences.

- **83(b) election** — File within 30 days of receiving founder
  stock. Clerky reminds you; Atlas sometimes doesn't. Missing this
  costs you a 6-figure tax bill when you exit.
- **Founder stock vesting** — 4-year vest, 1-year cliff. Sets
  expectations if a co-founder ever joins.
- **Operating agreement / bylaws** — Clerky/Atlas generate them.

## Step 6 — The recurring costs this runbook used to omit _(company path only)_

A Delaware C-corp is not a one-time fee, and the bill that surprises
founders is the franchise tax. Delaware offers two calculation methods and
you may use **whichever produces the lower tax**:

- **Authorized Shares Method** — minimum **$175**. This is the default the
  state's notice computes, and on a typical 10M-authorized-share startup cap
  table it produces a number in the **tens of thousands of dollars**. That
  notice is not a bill you owe; it is a bill you recalculate.
- **Assumed Par Value Capital Method** — minimum **$400**, computed from
  issued shares and total gross assets (the "total assets" line on Form 1120
  Schedule L). For an early-stage company with few assets this is almost
  always the lower of the two, and it is what turns a five-figure notice into
  a few hundred dollars.

Plus the **annual report**, filed with the tax. Both are due **on or before
March 1** each year. Clerky and Atlas will prompt you; the calculation is
still yours to get right.

Budget annually, not once:

| Recurring | Cost |
|---|---|
| DE franchise tax (Assumed Par Value, early stage) | ~$400+ |
| DE annual report | $50 |
| Registered agent | ~$100/yr (Atlas) to ~$300/yr elsewhere |
| Home-state registration renewal | varies |

## Checklist

Sole-proprietor path (current — decisions §252):

- [x] Legal pages name the operator, location (Virginia) and email-only contact (`operator.ts`)
- [ ] E&O + cyber cover bound (hard gate before customer #1)
- [ ] Local business licence, if required
- [ ] _Optional:_ EIN, separate bank account, DBA (only to trade as "FeohLedger")
- [ ] TOS + Privacy + DPA published on the marketing site
- [ ] MSA + Order Form templates ready to send
- [ ] Startup lawyer on retainer (or flat-fee relationship)

Company path (only when a §252 trigger fires):

- [ ] Entity formed (single-member LLC, or DE C-corp if fundraising)
- [ ] EIN issued for the entity, bank account moved to it
- [ ] 83(b) filed (C-corp only)
- [ ] Franchise-tax method chosen (Assumed Par Value, almost certainly)
- [ ] March 1 franchise tax + annual report reminder in the calendar
- [ ] Customer agreements assigned to the entity (Terms §1)

Sole-proprietor path: nothing required beyond the insurance premium, a local
licence if one applies, and the eventual lawyer. No franchise tax, no
registered agent.

Company path total cost: ~$500–1500 one-time, plus **~$550+/yr recurring** (franchise
tax + annual report + registered agent), plus the eventual lawyer retainer.
This footer read "one-time" only until 2026-09-17, which is how the
franchise tax becomes a surprise.
Total time: days for the sole-proprietor path (insurance quotes are the
slow part); 1–3 weeks for a company.

## Sources

Verified 2026-09-17:

- [Delaware Division of Corporations — how to calculate franchise taxes](https://corp.delaware.gov/frtaxcalc/)
- [Wolters Kluwer — Delaware annual report and franchise tax due March 1](https://www.wolterskluwer.com/en/expert-insights/delaware-corporations-annual-franchise-report-and-tax-requirement)
- [Clerky — calculating the Delaware franchise tax](https://help.clerky.com/article/2796-calculate-delaware-franchise-tax)
- [Common Paper — standard contracts](https://commonpaper.com/standards/)
