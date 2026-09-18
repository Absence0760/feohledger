# Insurance

**Why this matters**: Every enterprise Data Processing Agreement
(DPA) has an insurance clause. Most require cyber liability + E&O at
stated minimum limits before they'll sign. Procurement will stall
without it.

## What to carry

| Policy | Coverage | Startup price | Required by... |
|---|---|---|---|
| **Cyber liability** | Data breach response, forensics, customer notification, ransomware | $1M–$5M limit, ~$1500–5000/yr | Nearly every enterprise DPA |
| **Errors & omissions (E&O / Tech E&O)** | Claims that your software caused customer financial harm (wrong payment, dropped invoice, failed compliance) | ~$3000–8000/yr bundled with cyber | Larger enterprise customers (>$100K ARR) |
| **General liability** | Slip-and-fall / property damage — only matters if you ever host visitors | ~$500/yr | Landlords, some customers |
| **Directors & Officers (D&O)** | Protects founders personally from company-related lawsuits | $2K–$10K/yr | Investors (priced rounds) |

**Minimum viable** for pilot #1: cyber + E&O, $1M each, bundled.

## Where to get quoted

Online-first brokers that specialize in SaaS:
- **Coalition** — Best UX. Strong on cyber specifically. Quote
  online, bind in a day.
- **Vouch** — Built for startups. Easier to bundle cyber + E&O +
  GL into one policy.
- **Embroker** — Similar to Vouch. Slightly more hand-holding.
- **At-Bay** — Cyber-focused, good for tech + SaaS risks.

Traditional brokers (Marsh, Aon, HUB) work too but overkill for
sub-$5M revenue. Stick with Coalition or Vouch.

## How to respond to a DPA's insurance clause

Enterprise DPA typical requirement:

> Vendor shall maintain cyber liability insurance of at least
> $5,000,000 per occurrence and errors & omissions insurance of at
> least $2,000,000 per occurrence, with [named insured / waiver of
> subrogation / 30-day cancellation notice].

If your pilot customer is small-mid-market, push back on limits:
- $5M cyber is typical; $1M is defensible for a pre-revenue pilot
- Named-insured / additional-insured is cheap to add — your broker
  issues a certificate of insurance (COI)
- Waiver of subrogation is cheap
- 30-day notice is standard

If the enterprise customer won't budge, you can buy up the limits
mid-year; a pure limit increase is a pro-rata premium bump.

## When to raise limits

- After customer #3: consider raising cyber to $2M.
- After $500K ARR: raise E&O to $5M.
- After a security incident (even minor): re-quote everything.
- Before a Series A: D&O becomes required; add it.

## Ongoing

- **Annual renewal** — premium shifts based on revenue + incidents.
  Budget 15% increase year-over-year.
- **COI on demand** — customers periodically ask for fresh COIs.
  Your broker issues them; keep a form letter template ready.
- **Claims** — report anything that *might* trigger a claim within
  48h. Delayed reporting voids coverage on some policies.

## Checklist

- [ ] Cyber liability quoted + bound
- [ ] E&O quoted + bound (bundle with cyber)
- [ ] COI saved to shared drive for DPA responses
- [ ] Broker contact saved to `support-and-status.md` incident runbook
- [ ] Renewal calendar reminder at T-30d

Time: ~1 day (mostly waiting for underwriting).
Cost: **$3–13K/yr (~$250–1100/mo)** for a cyber + E&O bundle, from the
table at the top of this page. This footer read "~$250/mo" until
2026-09-17 — that is the floor of the range, not the midpoint, and it is
the number that ended up in `support-and-status.md` too.
