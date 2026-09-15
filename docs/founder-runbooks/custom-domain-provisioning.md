# Custom domains — TLS + DNS for a customer's vanity hostname

**Why this matters**: The white-label sale is "our AP portal, on our
domain". A partner reselling FeohLedger, or a direct customer with a
brand team, will ask for `acme.acmecorp.com` instead of
`acme.feohledger.com`. The app code for this ships — the tenant
resolver maps an inbound `Host` back to a tenant, and the Custom
Domains panel registers it. What does **not** ship is the DNS record
and the TLS certificate. Those are yours, and they are different work
depending on which deployment shape you are running.

> This runbook is the **founder-facing procedure** — what to ask the
> customer for, what to change, in what order. For the engineering
> rationale (why a `Host` only resolves a *candidate* tenant, why the
> JWT `org`-claim cross-check still gates everything, the cross-org
> uniqueness guard), see
> [`docs/white-label.md`](../white-label.md) § Custom domains and
> § Partner / reseller admin.

## Current state

- **Ships**: the custom-domain resolver (`backend/app/tenant.py` —
  `normalize_custom_domain` / `resolve_tenant_slug_by_custom_domain`),
  the admin endpoint pair `GET`/`PUT
  /api/organization/branding/custom-domains`
  (`backend/app/api/organization.py`), the **Custom Domains** panel on
  `/organization`, and partner provisioning
  (`POST /api/partner/children/provision`). Also: **SSO on a vanity
  host** — `?slug=` is optional on the SSO/SAML config, authorize and
  login endpoints, with the tenant resolved from the request `Host`
  through that same resolver, plus the opt-in
  `settings.brand.sso_callback_base_url` this runbook's Step 7 sets.
- **Does not ship**: any DNS or certificate automation. `infra/` today
  is **KMS + S3 buckets only** (`infra/main.tf`, `kms.tf`, `s3.tf`) —
  there is no Terraform for CloudFront, ALB, or ACM. Everything in the
  AWS branch of Step 3 is console/CLI work you do by hand the first
  time.

---

## Read this first — the hostname is not free-form

The vanity host **must** be `<tenant-slug>.<customer-domain>`. This is
not a style preference; it is what the shipped code does.

The SPA derives the tenant from the browser's own hostname
(`frontend/src/lib/tenant.ts` — `getTenantSlug()`): for a hostname of
three or more labels it takes **the first label** and sends it as
`X-Tenant-Slug`. The backend (`backend/app/tenant.py` —
`get_tenant_slug`) uses that header **verbatim when present**, and only
falls back to matching the request `Host` against the registered
custom domains when the header is **absent**.

| Vanity host | Tenant slug | What happens |
|---|---|---|
| `acme.acmecorp.com` | `acme` | ✅ Works. SPA sends `X-Tenant-Slug: acme`; resolves normally. |
| `ap.acmecorp.com` | `acme` | ❌ SPA sends `X-Tenant-Slug: ap` → `404 Unknown tenant: ap` on every call. |
| `acmecorp.com` (bare apex) | `acme` | ❌ `getTenantSlug()` returns `null` for a two-label host, and the SPA still calls the fixed `PUBLIC_API_URL` origin — so the backend never sees the vanity `Host` and answers `400 Missing X-Tenant-Slug header`. **Not supported today.** |

**So: pick the slug to match the label the customer wants.** The slug
is chosen at provisioning time — `POST /api/partner/children/provision`
takes `{name, slug, admin_email}` — so a customer who wants
`ap.acmecorp.com` gets a tenant whose slug *is* `ap`.

The catch, and you should say it out loud in the sales conversation:
**the slug is platform-global** (it is the tenant database name,
`feoh_<slug>`), while the label the customer picks is local to their
domain. `ap.acmecorp.com` and `ap.othercorp.com` cannot both exist —
the second customer cannot have slug `ap`. In practice, steer every
customer to `<their-company-slug>.<their-domain>`, which reads
naturally and never collides.

If a customer hard-requires a bare apex (`acmecorp.com`), the honest
answer today is **no** — see *Known limitations* below for why, and
treat it as a product ask, not an ops workaround.

---

## Who does what

| Party | Does |
|---|---|
| **Partner / reseller** (or your sales lead) | Agrees the hostname with the customer *before* provisioning, so the slug can be chosen to match. Provisions the child tenant (`POST /api/partner/children/provision`) and hands over the one-time admin password. Cannot register the domain itself — see below. |
| **Child tenant's own admin** | Registers the hostname in the app, via the **Custom Domains** panel on `/organization` (or `PUT /api/organization/branding/custom-domains`). This is admin-only **on that tenant**. |
| **Customer's IT / DNS owner** | Creates the DNS record, and (AWS shape only) the ACM validation `CNAME`. Must be told never to delete the validation record — renewal depends on it. |
| **You, the operator** | Certificate + edge config, the CORS env change, the redeploy, and the end-to-end verification. |

> **A partner cannot set a child's custom domains.** `PUT
> /api/partner/children/{id}/branding` *preserves* the child's
> `custom_domains` list but never writes it
> (`backend/app/api/partner.py`). Registration is deliberately the
> child's own admin action. If the partner is operating the child
> account on the customer's behalf, they do it with the child admin
> credentials from provisioning.

## Prerequisites

- [ ] The tenant exists and its slug equals the first label of the
      agreed hostname.
- [ ] The customer controls the domain and can create DNS records in
      it (or can delegate a subdomain to you).
- [ ] You know which deployment shape you are on — the minimal
      single-VM Caddy stack
      ([`docs/minimal-deployment.md`](../minimal-deployment.md)) or the
      AWS CloudFront/ALB build-out
      ([`docs/production-deployment.md`](../production-deployment.md)).
- [ ] You can redeploy (the CORS change in Step 5 needs one).

---

## Step 1 — Agree the hostname and confirm the slug

1. Confirm the exact hostname with the customer, lowercase, no port,
   no path. The app normalizes what you type (`normalize_custom_domain`
   strips a `:port`, lowercases, and rejects empty / IPv6-literal /
   whitespace / slash-bearing values), so `AP.AcmeCorp.com:443` is
   stored as `ap.acmecorp.com` — but agree the normalized form so
   nobody is surprised.
2. Check the tenant's slug matches the first label (`GET
   /api/partner` lists each child's slug; the `/organization` page
   shows the tenant's own).
3. If it doesn't match and the tenant is not yet live, **re-provision
   with the right slug**. If the tenant *is* live, changing the slug
   means a new tenant database — treat that as a migration project,
   not a runbook step.

## Step 2 — Customer creates the DNS record

The record's target depends on your shape.

| Shape | Record the customer creates |
|---|---|
| **Minimal (Caddy on one VM)** | `acme.acmecorp.com` → `CNAME` to your app host (`feohledger.com`), or an `A` record to the VM's IP. Either is fine — Caddy follows DNS for the HTTP-01 challenge. |
| **AWS (CloudFront)** | `acme.acmecorp.com` → `CNAME` to the CloudFront distribution's domain name (`dxxxxxxxxxxxxx.cloudfront.net`, from the distribution's General tab). **CONFIRM ON FIRST RUN** — the distribution does not exist yet; nothing in `infra/` creates it. |

Because the vanity host lives in the *customer's* zone, your own
wildcard record (`*.feohledger.com`, the one that makes
`deploy/add-tenant.sh` need no DNS work) does **not** cover it. Every
vanity domain is a per-customer DNS step.

Wait for propagation before Step 3 — the certificate cannot issue until
the name resolves to your edge:

```bash
dig +short acme.acmecorp.com
```

## Step 3 — Issue the certificate

### 3a. Minimal shape — Caddy (Let's Encrypt, HTTP-01)

There is **no separate validation record**. The `A`/`CNAME` from Step 2
*is* the proof of control: Caddy answers the HTTP-01 challenge on port
80 for any hostname it is configured to serve.

1. Add a site block to the per-VM `deploy/tenants.caddy` (gitignored;
   the same file `deploy/add-tenant.sh` appends to — but that script
   only ever writes `<slug>.<APP_DOMAIN>`, so a vanity host is added by
   hand):

   ```caddy
   acme.acmecorp.com {
   	import spa
   }
   ```

2. Reload without downtime (the command from
   `deploy/tenants.caddy.example`):

   ```bash
   docker compose -f compose.prod.yml exec caddy \
     caddy reload --config /etc/caddy/Caddyfile
   ```

3. Caddy requests the certificate on first request to that host and
   **renews it automatically**. Nothing to diarise.

Constraints worth knowing:

- **Ports 80 and 443 must both be open** (80 for the HTTP-01
  challenge, 443/TCP + 443/UDP for HTTP/3 — the security-group shape
  in `docs/minimal-deployment.md` § 1).
- **One certificate per hostname.** Let's Encrypt's headline rate limit
  is per *registered domain* — and each customer's vanity domain is its
  own registered domain, so the "~50 certs/week" ceiling
  `docs/minimal-deployment.md` cites for your own `*.feohledger.com`
  hosts effectively never binds here. The limit that *can* bite is
  repeated **failed** validations for one hostname (typically while DNS
  is still wrong). Fix DNS, then retry — don't loop the reload.
  **CONFIRM ON FIRST RUN**: check current limits at
  <https://letsencrypt.org/docs/rate-limits/> before a bulk onboarding.
- The wildcard-certificate escape hatch in
  `docs/minimal-deployment.md` (xcaddy + the Route 53 DNS plugin) does
  **not** help here — it can only cover zones you control in Route 53,
  not the customer's.
- The `spa` snippet in `deploy/Caddyfile` sets
  `Strict-Transport-Security: max-age=31536000; includeSubDomains`.
  Serving that on `acme.acmecorp.com` commits every subdomain *of that
  host* to HTTPS for a year in visitors' browsers. That is normally
  fine and desirable — but tell the customer, because it is their
  namespace, not yours.

### 3b. AWS shape — ACM + CloudFront

**CONFIRM ON FIRST RUN — none of this is in `infra/`.** The whole
CloudFront/ALB stack is described as the target architecture in
`docs/production-deployment.md` but is not provisioned by Terraform
today (`infra/` is KMS + S3). Do it in the console the first time and
write down what you clicked.

1. **Request a public ACM certificate in `us-east-1`.** CloudFront only
   accepts certificates from `us-east-1`, regardless of where the rest
   of the stack runs (`docs/founder-runbooks/production-deployment.md`
   § Step 2 already says this for your own domain).
2. **Put every vanity hostname on it as an alternate name (SAN).** Your
   own `*.feohledger.com` wildcard does **not** cover a customer's
   domain — a wildcard only spans one label of one zone. So the
   certificate attached to the SPA distribution needs
   `acme.acmecorp.com` explicitly listed.
3. **Validate by DNS.** ACM issues one `CNAME` per name on the
   certificate — a `_<token>.acme.acmecorp.com` → `<value>.acm-validations.aws`
   pair, shown in the ACM console. **The customer creates it.** Tell
   them explicitly: *leave it in place forever* — ACM re-uses it for
   automatic renewal, and deleting it turns a silent renewal into an
   outage 13 months later.
4. **Add the hostname as an alternate domain name (CNAME) on the
   CloudFront distribution**, and attach the new certificate.
5. **Certificates are immutable.** You cannot add a SAN to an existing
   ACM certificate. Adding customer #2 means requesting a *new*
   certificate carrying customers #1 **and** #2, validating it, then
   swapping it onto the distribution. Plan for that: batch vanity
   domains where you can, and expect a re-issue per onboarding
   otherwise.

**Caps that decide how many vanity domains one deployment carries.**
These are AWS quota defaults at the time of writing; several are soft
and raisable on request. **CONFIRM ON FIRST RUN in Service Quotas** —
do not design a pricing tier around a number you have not looked up in
your own account:

| Cap | Default | Consequence |
|---|---|---|
| Alternate domain names (CNAMEs) per CloudFront distribution | 100 | Hard ceiling on vanity domains per distribution. Past it: a second distribution, and a per-customer routing decision. |
| Domain names (SANs) per ACM public certificate | 10, raisable | The binding limit long before CloudFront's 100. Raise it early — the request is routine but not instant. |
| Certificates per ALB HTTPS listener | 25, raisable | Only relevant if you ever terminate the vanity host at the ALB (you don't today — see *Known limitations*). |
| Alternate domain name uniqueness | Global across all of CloudFront | One hostname can be served by exactly one distribution, in any AWS account. This is the infrastructure mirror of the app's cross-org `409`. |

## Step 4 — Register the host in the app

Nothing above tells FeohLedger which tenant the host belongs to. That
is this step, and it is done **by the child tenant's own admin**.

**UI**: `/organization` → **Custom Domains** → add
`acme.acmecorp.com`. The field validates client-side against the same
rules the backend applies, so a typo surfaces inline rather than as a
`422`.

**API** (same effect; full-replace semantics — send the whole list):

```bash
curl -X PUT https://api.feohledger.com/api/organization/branding/custom-domains \
  -H "Authorization: Bearer <child-admin-jwt>" \
  -H "X-Tenant-Slug: acme" \
  -H "Content-Type: application/json" \
  -d '{"custom_domains": ["acme.acmecorp.com"]}'
```

What the responses mean:

- **`422 Invalid custom domain`** — the host failed
  `normalize_custom_domain`: it was empty, an IPv6 literal, or carried
  a slash or a space. Send a bare hostname.
- **`409 One or more requested custom domains is already registered to
  another tenant`** — **another org has already claimed this
  hostname.** The message is deliberately generic and never echoes the
  host back, so it cannot be used to probe which hostnames other
  tenants own. Two legitimate causes: (a) the customer previously ran
  on a different tenant of yours and it was never de-provisioned —
  find it and clear the entry (Step 8), or (b) somebody typo'd another
  customer's hostname. There is no override; the guard is
  serialized by an advisory lock precisely so two orgs cannot both win
  a race for the same host.
- **`403`** — you are not an admin *of that tenant*. A partner's own
  admin JWT will not do; use the child's.

The mutation is audited into the tenant trail as
`organization.custom_domains_updated`, **count only** — the hostnames
themselves are never written to the audit log, so don't expect to
reconstruct the list from it.

## Step 5 — Platform config: CORS (and a redeploy)

The SPA is served from `acme.acmecorp.com` but calls the API at the
single fixed `PUBLIC_API_URL` origin, so every API call is
cross-origin. Until the customer's domain is allowed, the browser
blocks them and the app looks broken with no server-side error.

Add the **registrable domain** to `FEOH_CORS_PRODUCTION_DOMAIN` — it is
comma-separated (`backend/app/main.py::_build_cors_origin_regex`):

```
FEOH_CORS_PRODUCTION_DOMAIN=feohledger.com,acmecorp.com
```

The regex built from this allows `https?://([\w-]+\.)?<domain>`, i.e.
the domain itself plus **one** leading label. So `acmecorp.com` covers
`acme.acmecorp.com`. A deeper name (`a.b.acmecorp.com`) is **not**
covered — another reason to keep vanity hosts to one label.

Security note, worth a moment: this grants CORS to *any* single-label
subdomain of that domain. Only ever add a domain the customer actually
controls — never a domain a customer merely *claims*, and never a
shared hosting domain.

Then redeploy so the backend picks up the env change (minimal shape:
`deploy/deploy.sh`; AWS shape: a published release through
`aws-deploy.yml`).

## Step 6 — Verify end to end

Do all of these. The first three prove infrastructure; the rest prove
the tenant actually resolved.

```bash
# 1. DNS points at your edge
dig +short acme.acmecorp.com

# 2. TLS terminates and the cert covers this name
curl -sSI https://acme.acmecorp.com | head -1
openssl s_client -connect acme.acmecorp.com:443 \
  -servername acme.acmecorp.com </dev/null 2>/dev/null \
  | openssl x509 -noout -subject -dates -ext subjectAltName

# 3. The SPA is served (not a 404 from the default host)
curl -sS https://acme.acmecorp.com/ | grep -i '<title'
```

4. **The tenant resolves by `Host`, with no header.** This is the one
   check that exercises the custom-domain resolver itself rather than
   the SPA's header. `GET /api/portal/branding` is public-by-design and
   resolves through the same `get_tenant` chokepoint:

   ```bash
   curl -sS https://api.feohledger.com/api/portal/branding \
     -H "Host: acme.acmecorp.com"
   ```

   Expect the tenant's `BrandConfig` (`product_name`, `logo_url`,
   `accent_color`, …). A `400 Missing X-Tenant-Slug header` means the
   host is not registered (Step 4) or was normalized differently than
   you typed it. *(Overriding `Host` against a fronted API endpoint may
   be rejected by the edge before it reaches the app — if so, run this
   from the VM against the backend container instead.)*

5. **Login page themes to the tenant's brand.** Open
   `https://acme.acmecorp.com/login` in a fresh private window. The
   product name, logo and accent color must be the *customer's*, not
   the platform default — that read happens before any login, over the
   public branding endpoint.
6. **Supplier portal themes too.** Open
   `https://acme.acmecorp.com/portal/login`. Same check: buyer's brand,
   `<title>` reading `<Product> — Supplier Portal`. This is the surface
   the customer's *own* suppliers see, so it is the one they will
   notice.
7. **Log in as a tenant user** and confirm real data loads. In devtools
   → Network, confirm requests carry `X-Tenant-Slug: acme`. If they
   carry something else, the hostname violates the naming rule.
8. **Confirm the platform subdomain still works.**
   `https://acme.feohledger.com` must keep working — a custom
   domain is *additive*, not a move, and it is your fallback when the
   customer's DNS breaks.
9. **Cross-tenant negative check.** Log in to a *different* tenant and
   point a request at the vanity host; expect `403 Token does not match
   the requested tenant`. The `Host` only picks a candidate tenant; the
   JWT `org` claim is the authority (`docs/white-label.md` § Trust
   model). Worth running once, on your first vanity domain, so you have
   seen the guard fire.

---

## Step 7 — Re-register SSO at the customer's IdP

**Skip this if the tenant does not use SSO.** For a tenant that does, the
SSO/SAML buttons on `https://acme.acmecorp.com/login` already work as soon as
Step 4 lands — `?slug=` is optional on the entry points and the backend
resolves the tenant from the `Host` header. What does *not* follow
automatically is where the IdP sends the user **back**: the OIDC `redirect_uri`
and the SAML bridge URL are values registered inside the customer's own Okta /
Entra / Keycloak app, so until this step the login round-trips through
`acme.feohledger.com` and lands the user there. It works; it just isn't
white-label.

This is an **operator-sequenced migration, not a config flip**, and the order
below is what keeps SSO working the whole way through. Doing it backwards —
setting the app-side value before the IdP knows the new URI — breaks every SSO
login for that tenant with an `invalid redirect_uri` from the IdP.

1. **Customer ADDS the new callback URI at their IdP, keeping the old one.**
   Every mainstream IdP allows a list. The two values are:

   | Protocol | Value to add |
   |---|---|
   | OIDC | `https://acme.acmecorp.com/login/sso-callback` |
   | SAML | `https://acme.acmecorp.com/login/saml-callback` |

   (Those paths are `FEOH_SSO_REDIRECT_PATH` / `FEOH_SAML_ACS_PATH`; confirm
   against your deployment if you have changed them. The SAML **SP EntityID**
   and **ACS URL** are *not* affected — both stay derived from
   `FEOH_API_PUBLIC_URL` — so the SAML app's trust config needs no other edit.)

2. **Confirm with the customer that the change is live at the IdP.** Not "was
   submitted" — live. This is the one step you cannot verify from your side.

3. **Set the app-side override.** As a tenant admin, `PUT` the branding config
   with the new base URL. This is an API-only field today — the `/organization`
   Branding panel does not yet render it, and because `PUT /branding` replaces
   the whole block, **a save from that panel will clear this value**. Set it
   after any branding work, and re-check it if an admin edits branding later:

   ```bash
   curl -sS -X PUT https://api.feohledger.com/api/organization/branding \
     -H "Authorization: Bearer <tenant-admin-jwt>" \
     -H "X-Tenant-Slug: acme" -H 'Content-Type: application/json' \
     -d '{"sso_callback_base_url": "https://acme.acmecorp.com"}'
   ```

   `sso_callback_base_url` is a **base URL**, not a full callback URL — the
   protocol-specific path is appended. It is admin-only, validated as an
   http(s) URL, and audited PII-free. Empty (the default) means the global
   `FEOH_TENANT_URL_TEMPLATE`, so *not* setting it is a supported end state.

   ⚠️ Send the whole branding object if the tenant has other branding set —
   `PUT /branding` replaces the block, so a partial body blanks the rest of it.
   `GET /api/organization/branding` first, edit the one field, `PUT` it back.

   ⚠️ It is deliberately **separate** from `tenant_url_template` (the field that
   re-points invite / password-reset / portal links). Setting that one must
   never silently move an IdP-registered callback, which is exactly why there
   are two fields — see `docs/decisions.md` §91.

4. **Verify a real login.** Fresh private window →
   `https://acme.acmecorp.com/login` → the SSO button → authenticate → you must
   land back on `acme.acmecorp.com`, signed in. If the IdP shows an
   `invalid redirect_uri` / `AuthnRequest` destination error, step 1 has not
   actually gone live: clear the override (send `null`) to fall straight back
   to the platform subdomain, and retry when it has.

5. **Only then, customer removes the old callback URI** from the IdP app — and
   only if they want to. Leaving it registered costs nothing and keeps
   `acme.feohledger.com` working as a fallback, which is the same posture
   Step 6.8 takes for the rest of the app.

Reference: [`../authentication.md`](../authentication.md) § SSO on a white-label
vanity host.

---

## Known limitations — tell the customer up front

These are properties of the shipped code, not oversights in this
procedure. Say them during the sale, not after go-live.

- **Emails and deep links need one more opt-in.** Notification emails,
  supplier-portal links, approval deep links and virtual-card reveal links are
  built from `settings.brand.tenant_url_template` when the tenant sets it and
  the global `FEOH_TENANT_URL_TEMPLATE` when it does not — so they keep saying
  `acme.feohledger.com` until an admin sets that field (the
  `/organization` Branding panel). Left unset, that is a supported end state,
  not a bug.
- **SSO callbacks need the IdP re-registration in Step 7.** Same shape, a
  different field (`sso_callback_base_url`) and a different reason: the value
  lives in the *customer's* IdP app, so it cannot be moved from our side alone.
  Until Step 7 the SSO buttons work on the vanity host but the round-trip lands
  the user on the platform subdomain.
- **A passkey is bound to the host it was registered on.** WebAuthn binds a
  credential to a Relying Party ID, and the RP is now resolved per request from
  the tenant's own registered custom domains — so passkeys *do* work on
  `acme.acmecorp.com`, but one registered on `acme.feohledger.com` is a
  different RP and will not work here (and vice versa). Users on both hosts
  register once per host, or use **TOTP**.
- **The API is not served under the vanity host.** `PUBLIC_API_URL` is
  baked into the frontend at build time, one value for all hosts, so
  the vanity host serves the SPA only. Anyone integrating against the
  public API uses `api.feohledger.com` with an API key, as normal.
- **Bare apex domains are unsupported** (see the naming rule). Making
  them work needs a runtime-resolved API base *and* a same-origin `/api`
  route per vanity host — a product change, not a config.

---

## Rollback / de-provisioning

Order matters: remove the app registration **last**, or the host
resolves to nothing while DNS still points at you.

1. **Customer removes the DNS record.** Once it stops resolving, the
   host is unreachable regardless of anything below.
2. **Remove the edge config.**
   - *Minimal*: delete the site block from `deploy/tenants.caddy` and
     reload Caddy. The certificate simply stops renewing.
   - *AWS*: remove the alternate domain name from the distribution.
     Re-issue the ACM certificate without that SAN at your next
     onboarding rather than immediately — an unused SAN is harmless,
     an unnecessary certificate swap is a change window.
3. **Un-register the host in the app**: `/organization` → Custom
   Domains → remove (two-click armed confirm), or `PUT` the list minus
   that host. **Do this even if the tenant is being deleted** — a
   lingering registration is what makes a future customer's `409` in
   Step 4 mysterious.
4. **Drop the domain from `FEOH_CORS_PRODUCTION_DOMAIN`** and redeploy.
   Leaving it grants CORS to a domain you no longer serve, and one that
   may change hands.
5. Confirm the platform subdomain still serves the tenant, if the
   tenant itself is staying.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Every API call returns `404 Unknown tenant: <label>` | The hostname's first label isn't the tenant slug. The SPA sent that label as `X-Tenant-Slug` and the custom-domain fallback never fired (the header takes priority and skips the DB lookup entirely). | Rename the host to `<slug>.<domain>`, or re-provision the tenant with the slug the customer wants. No amount of custom-domain registration fixes this. |
| `400 Missing X-Tenant-Slug header` | Two-label host: `getTenantSlug()` returned `null`, and the API call still went to the fixed API origin, so the backend saw its own `Host`. | Unsupported shape — use a three-label host. |
| `409 …already registered to another tenant` on Step 4 | Another org's `settings.brand.custom_domains` already contains this host — often a stale entry from a tenant you retired. | Find and clear the old registration (an old tenant of the same customer, or a typo'd neighbour). The message never names the other tenant, by design. There is no force flag. |
| Host registered, but `GET /api/portal/branding` with `Host:` still 400s | Stored form differs from what you sent — `normalize_custom_domain` lowercases and strips `:port`, and rejects anything with a `/` or a space outright. | `GET /api/organization/branding/custom-domains` and compare the stored string byte-for-byte with the `Host` you're testing. |
| Browser shows a certificate warning / `ERR_CERT_COMMON_NAME_INVALID` | *Minimal*: the host isn't in `tenants.caddy`, or DNS didn't resolve when Caddy tried HTTP-01. *AWS*: the hostname isn't a SAN on the certificate attached to the distribution. | Fix DNS first, then re-run Step 3. Check Caddy's logs for the ACME error rather than reloading in a loop — repeated failed validations are themselves rate-limited. |
| Page loads, then every request fails in the console with a CORS error | Step 5 missed, or the redeploy didn't happen. | Add the registrable domain to `FEOH_CORS_PRODUCTION_DOMAIN` and redeploy. Remember it covers only **one** leading label. |
| Vanity host serves the *platform* login page with default branding | *Minimal*: the request fell through to the bare `{$APP_DOMAIN}` site block because no vanity block exists. *AWS*: the alternate domain name isn't on the distribution, so the edge served the default. | Add the host block / alternate domain name and reload. |
| Login page themes correctly, supplier portal doesn't | Almost always a brand config issue, not a domain one — the two read the same endpoint through the same resolver. | Check `GET /api/organization/branding` for the tenant; then `docs/white-label.md` § Supplier-portal theming. |
| Passkey enrollment or login fails only on the vanity host | Expected — `FEOH_WEBAUTHN_RP_ID` is bound to your domain. | Use TOTP on vanity hosts. See *Known limitations*. |
| Renewal fails ~13 months in (AWS) | The customer deleted the ACM validation `CNAME` after issuance. | Recreate it from the ACM console. Then add "never delete this record" to the handover email you send at Step 2. |

---

## Checklist

- [ ] Hostname agreed, and it is `<tenant-slug>.<customer-domain>`
- [ ] Tenant provisioned with the matching slug
- [ ] Customer created the DNS record; `dig` resolves to your edge
- [ ] Certificate issued (Caddy HTTP-01 **or** ACM SAN + validation CNAME)
- [ ] AWS only: alternate domain name added to the distribution;
      quotas checked in Service Quotas
- [ ] Child admin registered the host in **Custom Domains** (no `409`)
- [ ] `FEOH_CORS_PRODUCTION_DOMAIN` updated and redeployed
- [ ] `Host:`-only branding read returns the tenant's brand
- [ ] Login page **and** supplier portal login theme to the customer's brand
- [ ] Logged in as a tenant user; real data loads
- [ ] Platform subdomain still works
- [ ] Cross-tenant negative check returns `403` (once, on your first one)
- [ ] SSO tenants only: new callback URI added at the customer's IdP **and
      confirmed live**, then `sso_callback_base_url` set, then a real SSO login
      verified end to end (Step 7 — in that order)
- [ ] Customer told: validation record is permanent; outbound links stay on the
      platform subdomain until `tenant_url_template` is set; a passkey is bound
      to the host it was registered on

Time: ~30 minutes of your work per customer, plus however long the
customer's DNS team takes (usually the long pole). First time on the
AWS shape, budget half a day — the distribution and certificate don't
exist yet.
Cost: $0 on the minimal shape (Let's Encrypt). On AWS, ACM public
certificates are free; the cost is the distribution you already run.
