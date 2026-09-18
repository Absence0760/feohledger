# Legal entity + contracts

**Why this matters**: You cannot legally invoice a customer until you
exist as a business. You cannot accept payments into a personal bank
account without material tax pain. You cannot sign a SOC 2 auditor
engagement letter as a natural person.

## Step 1 — Incorporate

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

## Step 5 — Founder paperwork

Don't skip these — they have one-way consequences.

- **83(b) election** — File within 30 days of receiving founder
  stock. Clerky reminds you; Atlas sometimes doesn't. Missing this
  costs you a 6-figure tax bill when you exit.
- **Founder stock vesting** — 4-year vest, 1-year cliff. Sets
  expectations if a co-founder ever joins.
- **Operating agreement / bylaws** — Clerky/Atlas generate them.

## Step 6 — The recurring costs this runbook used to omit

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

- [ ] Entity incorporated (DE C-corp)
- [ ] EIN issued
- [ ] Business bank account opened
- [ ] 83(b) filed
- [ ] TOS + Privacy + DPA published on the marketing site
- [ ] MSA + Order Form templates ready to send
- [ ] Startup lawyer on retainer (or flat-fee relationship)
- [ ] Franchise-tax method chosen (Assumed Par Value, almost certainly)
- [ ] March 1 franchise tax + annual report reminder in the calendar

Total cost: ~$500–1500 one-time, plus **~$550+/yr recurring** (franchise
tax + annual report + registered agent), plus the eventual lawyer retainer.
This footer read "one-time" only until 2026-09-17, which is how the
franchise tax becomes a surprise.
Total time: 1–3 weeks.

## Sources

Verified 2026-09-17:

- [Delaware Division of Corporations — how to calculate franchise taxes](https://corp.delaware.gov/frtaxcalc/)
- [Wolters Kluwer — Delaware annual report and franchise tax due March 1](https://www.wolterskluwer.com/en/expert-insights/delaware-corporations-annual-franchise-report-and-tax-requirement)
- [Clerky — calculating the Delaware franchise tax](https://help.clerky.com/article/2796-calculate-delaware-franchise-tax)
- [Common Paper — standard contracts](https://commonpaper.com/standards/)
