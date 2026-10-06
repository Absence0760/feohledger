# Local SAML 2.0 SSO testing (Keycloak)

SAML SSO is exercised locally against the same **Keycloak** container that backs
local OIDC testing (see [`local-sso-keycloak.md`](local-sso-keycloak.md)) — no
new service. Keycloak speaks SAML out of the box; the realm export ships a SAML
client alongside the OIDC one.

This is the local-first companion to the design in
[`authentication.md`](authentication.md) § SAML SSO. Nothing here needs a cloud
account or a real IdP.

## TL;DR

```bash
pnpm idp:up        # start Keycloak (opt-in `idp` profile, :8088)
pnpm seed          # if you haven't — creates the acme tenant
pnpm saml:seed     # point acme's settings.sso at local Keycloak (protocol=saml)
pnpm dev           # backend :8000 + frontend :7777
# open http://acme.localhost:7777 → "Sign in with SSO" → Keycloak login
#   demo@acme.com / demo      (links to acme's seeded admin)
#   newhire@acme.com / demo   (JIT-provisions a new ap_clerk)
```

`pnpm saml:seed` rewrites acme's `settings.sso` to `protocol=saml`, so it
**replaces** any OIDC block from `pnpm idp:seed` (a tenant uses one protocol at a
time). Run `python scripts/enable_keycloak_saml.py --disable` (from `backend/`)
to turn it back off, or `pnpm idp:seed` to switch back to OIDC.

Like `idp:seed`, this writes the row directly — a dev shortcut that skips the
audited `PUT /api/organization/sso` the `/organization` → **Single Sign-On**
panel uses. Pasting the same IdP entity ID, SSO URL and certificate into that
panel is the product path (`docs/authentication.md` § SSO configuration).

## How it fits together

```
Browser ──"Sign in with SSO"──▶ GET /api/auth/saml/login?slug=acme   (backend)
        ◀──────── 302 AuthnRequest ──────── builds RelayState{tenant,request_id}
Keycloak hosted login (demo@acme.com / demo)
        ──────── HTTP-POST SAMLResponse ──▶ POST /api/auth/saml/acs    (backend)
                                            verify signature vs pinned cert,
                                            conditions/audience/destination,
                                            InResponseTo, replay dedup, JIT
        ◀──── 303 /login/saml-callback?code=<once> ────  (one-time handoff code)
Browser bridge ──── POST /api/auth/saml/exchange ──▶ JWT in response body
        ──────── stores JWT, lands on the dashboard
```

The tenant is recovered from the **server-minted RelayState**, never the
assertion — so one shared Keycloak SAML client serves every local tenant.

## What `realm-export.json` configures

The SAML client (`backend/keycloak/realm-export.json`):

- `clientId` = `http://localhost:8000/api/auth/saml/metadata` — the **shared
  local SP entityId**. `enable_keycloak_saml.py` sets each tenant's
  `settings.sso.sp_entity_id` to this same value, so the assertion's Audience
  matches.
- `saml.assertion.signature = true` — Keycloak signs the assertion. This
  signature, verified against Keycloak's signing cert, is the trust anchor.
- `saml.client.signature = false` — the SP does **not** sign its AuthnRequests
  locally, so no SP keypair is needed to run (set `FEOH_SAML_SP_PRIVATE_KEY` /
  `FEOH_SAML_SP_CERT` via sops + flip the client setting on for a prod-like setup).
- `saml_name_id_format = email` + email/givenName/sn attribute mappers — so the
  assertion carries the email the JIT provisioner needs.
- ACS POST binding → `http://localhost:8000/api/auth/saml/acs`.

Because the realm re-imports on every Keycloak boot, the client **must** live in
`realm-export.json` — admin-console edits are wiped on restart.

## Why the seed script fetches the cert live

Keycloak generates the realm's SAML **signing certificate** on import, and it
changes every boot. So the cert can't be hardcoded the way the OIDC
`client_secret` is. `enable_keycloak_saml.py` reads the live `entityId`, SSO URL,
and signing cert from Keycloak's SAML descriptor:

```
http://localhost:8088/realms/feohledger/protocol/saml/descriptor
```

(parsed with python3-saml's DTD/entity-hardened metadata parser — no XXE). If you
restart Keycloak, re-run `pnpm saml:seed` so the pinned cert matches the new key.

## e2e

```bash
pnpm idp:up && pnpm saml:seed     # bring up + seed
pnpm test:saml                    # frontend/tests-e2e/saml/login.spec.ts
```

The spec drives the full browser handshake against the real Keycloak and asserts
the JWT never appears in the callback URL. It **skips** with an actionable hint
when Keycloak/SAML isn't reachable (gated on `SERVICES.keycloakSaml`); under
`FEOH_REQUIRE_INTEGRATION=1` (CI) an unreachable IdP is a hard failure, never a
silent skip. CI runs it in `sso-e2e.yml` and the `service-e2e` job of `ci.yml`,
after the OIDC specs (SAML seeding rewrites acme's `settings.sso`).

## Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| Login button doesn't render | `pnpm saml:seed` not run, or wrong tenant — it seeds `acme` by default (`--slug`). |
| "SAML login could not be verified" after Keycloak login | Cert drift — Keycloak was restarted; re-run `pnpm saml:seed`. |
| Seed errors reading the descriptor | Keycloak not up yet — `pnpm idp:up` and wait for `:8088` to answer. |
| Both OIDC and SAML buttons show | A prior `idp:seed` + a `saml:seed` shouldn't coexist (each overwrites `settings.sso`); re-run one. |
