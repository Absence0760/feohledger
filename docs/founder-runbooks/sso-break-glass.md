# SSO break-glass — reopening password sign-in for a locked-out tenant

**When you need this**: a tenant has **Require SSO** (`settings.sso.sso_only`)
turned on, and its identity provider has stopped working. Typical causes:

- the customer's Entra client secret expired (Entra secrets last one to two years),
- the IdP rotated its SAML signing certificate and nobody updated ours,
- the IdP is down, or the customer deleted or reconfigured the app registration.

Every member is locked out, admins included. The setting that would reopen the
password lives on the `/organization` page, which needs a sign-in, and the SSO
button now fails at the IdP. The app cannot detect this on its own: whether a
tenant is SSO-only is decided by a *local* completeness check of the IdP
settings, because probing the IdP from the sign-in path would put its latency
and outages on our login (`docs/decisions.md` §204). A complete configuration
that points at a broken IdP still closes the password.

This is a **per-incident operator procedure**, not a launch step.

## What the script does, and what it does not

`backend/scripts/sso_break_glass.py --slug <slug>`:

- clears `settings.sso.sso_only` for that one tenant, **and nothing else**. SSO
  stays enabled and the IdP configuration (including the client secret and the
  SCIM settings) is untouched, so the SSO button stays on the login page beside
  the password form;
- writes an `organization.sso_only_lifted` row to the tenant's audit trail
  **before** it changes anything, with `actor_id` null, `source:
  operator_break_glass`, whether the password was actually closed, and your
  `--reason`. If that row cannot be written, nothing is changed (exit code 2).
  The row goes straight into the tenant database in **every** audit mode,
  `FEOH_AUDIT_MODE=lambda` included (`audit_dispatch.record_auth_audit_or_raise`):
  in lambda mode ordinary audit rows are only queued to SQS, and "queued" is
  not "recorded" — a dead-lettered message would leave the lift made with no
  row. So "nothing changed without a row" holds in lambda mode too;
- does nothing, and writes nothing, if the tenant does not have `sso_only` set.

It does not sign anyone in, reset any password, or touch any other tenant.
Members who were JIT-provisioned by SSO or SCIM have **no password**, so the
lift alone does not let them in; they can set one through **Forgot password**
on the login page (`POST /api/auth/forgot-password` mails any active account a
reset link). If no admin of the tenant has a password either, that is the first
thing the admin does.

## 1. Verify the request

A request to drop a tenant's SSO requirement is exactly what an attacker who
wants a password door would ask for. Before you run anything:

1. The request comes from a contact you already know at the customer (the
   signer of the order form, or an admin on file), through a channel you
   already have — not from an address in the request itself.
2. Call them back on a number you already hold and confirm.
3. Open a ticket and use its reference as `--reason`. No personal data in the
   reason: it is shipped to the WORM audit store.

## 2. Run it

On the production host, inside the `api` container (the same way
`deploy/add-tenant.sh` runs `create_tenant.py`):

```bash
docker compose -f compose.prod.yml exec -T api python scripts/sso_break_glass.py --slug acme --reason "TICKET-123 Entra client secret expired"
```

Expected output:

```
Cleared sso_only for tenant 'acme'; audited as organization.sso_only_lifted.
  Password sign-in is open again. SSO stays enabled; the IdP config is unchanged.
```

Exit codes: `0` lifted (or nothing to lift), `1` no tenant with that slug,
`2` the audit row could not be written, so nothing changed — fix the audit path
and re-run. That path is the tenant database itself (reachable, migrated,
`audit_log` writable) whatever `FEOH_AUDIT_MODE` says; SQS is not involved, so
an SQS outage cannot cause exit 2 and a healthy SQS does not rule it out.

If it also prints *"the IdP block did not resolve, so password sign-in was
already open"*, the password was never closed and the lockout has another cause
(a deactivated account, the per-account failure throttle, MFA). Investigate
that instead; the lift was harmless.

## 3. Hand back to the customer

Tell the admin:

1. Sign in with your password at the tenant URL.
2. Open **Organization → Single Sign-On**, fix the provider settings (paste a
   new client secret, or the new signing certificate), and save. Leaving the
   secret field blank keeps the stored one.
3. Test the SSO button in a private window **before** turning **Require SSO**
   back on — and keep this tab signed in while you do.

Every save on that panel is audited as `organization.sso_updated`, so the trail
shows the lift, the fix and the re-enable in order. That row is written the same
way as the lift's — first, synchronously into the tenant database in every audit
mode — and a save whose row cannot be written answers `503` and changes nothing.

## Reference

- Mechanism: `docs/authentication.md` § SSO-only mode.
- Code: `backend/app/services/sso_break_glass.py`, tests
  `backend/tests/test_sso_break_glass.py`.
