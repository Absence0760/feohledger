# SOC 2 — pick a vendor and start the clock

**Why this matters**: Every enterprise security questionnaire starts
with "Are you SOC 2 Type II?" The Type II clock is 6+ months of
observation — every week you delay is a week you can't answer "yes"
to that question.

## Current state

Engineering controls are done. See
[`docs/soc2-readiness.md`](../soc2-readiness.md) for the full control
inventory. Your remaining work is **non-code**:

1. Sign with a compliance vendor.
2. Adopt the policy library they provide.
3. Fill out their employee onboarding/offboarding checklists.
4. Pick an audit firm (the vendor's partners are fine).
5. Start the Type I audit once the vendor dashboard is green.
6. Ride out the 6-month Type II observation window.

## Step 1 — Pick a vendor

| Vendor | Price (startup) | Strengths | Pick if... |
|---|---|---|---|
| **Vanta** | $8–15K/yr | Largest integration catalog. Polished UX. | Default choice. YC portfolio uses this. |
| **Drata** | $7–14K/yr | Best continuous-monitoring dashboards. | You want detailed engineering visibility. |
| **Secureframe** | $7–12K/yr | Strong policy templates. | You want more hand-holding on policies. |
| **Sprinto** | $4–8K/yr | Cheapest. Startup-focused. | Tight budget. |

**Recommendation**: Vanta or Drata. Either is fine. Pick based on
which of their sales-engineering teams you prefer (book two intro
calls, decide in a week).

Each vendor signs you up with their preferred audit-firm partners —
you don't source the audit firm separately.

## Step 2 — Sign the contract

1. Fill out their signup form. Don't bother negotiating price for
   year 1; get the "startup tier" quote and move on.
2. Sign their MSA. It's standard; don't bill a lawyer to review a
   $10K/year SaaS contract.
3. Annual prepay usually saves ~10%. Do it.

## Step 3 — Connect integrations

The vendor dashboard has a long list of integrations. Connect at
minimum:
- **AWS** — readonly role for their scanner
- **GitHub** — org-level readonly app install
- **Google Workspace** (or whichever IdP you use)
- **Slack** (if you use it for internal comms)
- **Your code repo for SAST** — they pull from your
  `.github/workflows/security.yml` (CodeQL + Trivy) results

This gets you 70% of evidence collection automated on day 1.

## Step 4 — Adopt the policy library

The vendor hands you 12–18 policy templates. Do NOT skip this.
Auditor will ask for signed copies of:

- Information Security Policy
- Acceptable Use Policy
- Access Control Policy
- Change Management Policy
- Incident Response Policy
- Business Continuity Policy
- Vendor Management Policy
- Data Classification + Retention Policy
- Password Policy
- Encryption Policy
- Risk Assessment
- Employee Onboarding/Offboarding Policy

Process:
1. Read each template. Note anything that doesn't match reality.
2. Edit the discrepancies (they're usually minor).
3. Sign + date each. The vendor UI tracks this.

Time: ~1 week of part-time reading. Don't rush.

## Step 5 — Employee onboarding/offboarding

Even if you're a solo founder: fill out both checklists **for
yourself**. It's dumb but the auditor requires it.

Onboarding:
- Signed policies attestation
- MFA enrolled
- Account provisioning checklist
- Background check (use Checkr or Certn, ~$50/head)
- Signed NDA

Offboarding (in case a future hire leaves):
- Disable SSO → revokes downstream access
- Rotate shared credentials
- Document retrieval
- Final access review

## Step 6 — Vendor risk reviews

The vendor dashboard has a "vendor management" module. Enter each
material vendor and complete a one-page review:

- AWS, Anthropic, Modern Treasury, Lithic/Nium, Merge.dev, Vanta
  itself, your email provider, Stripe.

Each entry takes ~5 min. Don't overthink it.

## Step 7 — Type I audit

Once the vendor dashboard is 95%+ green:

1. Vendor recommends 2–3 audit firms. Common picks: Prescient, A-LIGN,
   Insight Assurance, Johanson Group.
2. Kickoff call with the auditor. They scope the engagement.
3. Type I takes 4–8 weeks. Most is the auditor asking for evidence
   that's already in Vanta/Drata; you answer follow-up questions.
4. Cost: $10–15K for Type I from a startup-focused firm.

Report issued → you can answer "yes" on Type I questionnaires.

## Step 8 — Open the Type II window

Type II is an observation period, not a separate audit. The day Type I
reports, the clock starts. Minimum 6 months; 12 months is more credible
for enterprise deals. Keep the vendor dashboard green the whole time —
the auditor samples evidence from the window retroactively.

The timeline block above used to open this window at T+12wk against a
Type I report at T+12–16wk, and put the Type II report at T+9–12mo —
a window opening before the report that starts it, and a report landing
without time for the closing engagement. Corrected 2026-09-17.

After 6+ months, the auditor does a new engagement to issue the
Type II report. Cost: $10–20K.

## Cost summary

| Item | Year 1 | Annual |
|---|---|---|
| Compliance vendor | $8–15K | $8–15K |
| Audit firm (Type I) | $10–15K | — |
| Audit firm (Type II) | $15–25K | $15–25K |
| Background checks | $50/head | $50/head |
| Penetration test (annual) | $8–15K | $8–15K |
| **Total** | **$40–70K** | **$30–55K** |

## Timeline

```
T+0       Sign vendor + connect integrations
T+2wk     Policies adopted, onboarding flows done
T+6wk     Dashboard 95%+ green
T+8wk     Type I kickoff
T+12–16wk Type I report issued  ← answer "yes" on questionnaires
          └─ the Type II observation window opens on THIS date
T+10–12mo Type II report issued ← the one buyers really want
          (Type I at ~3–4mo + a 6mo minimum window + a 4–8wk engagement.
           A 12-month window, which reads better to enterprise buyers,
           pushes this to ~T+16–18mo.)
```

## Checklist

- [ ] Vendor selected + contract signed
- [ ] Integrations connected
- [ ] Policies signed
- [ ] Onboarding/offboarding checklists complete (for yourself)
- [ ] Vendor risk reviews complete
- [ ] Type I audit firm selected + engaged
- [ ] Type I report issued
- [ ] Type II observation window open
- [ ] Calendar reminder at T+6mo to start Type II audit
- [ ] Annual penetration test scheduled — it is $8–15K in the cost table
      above and was in no step or checkbox until 2026-09-17. Auditors ask
      for it; budgeting for it is not the same as booking it

Related reading: [`docs/soc2-readiness.md`](../soc2-readiness.md) for
the engineering-side control inventory.
