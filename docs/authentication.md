# Authentication

JWT-based authentication using `python-jose` for token handling and `bcrypt_sha256` for password hashing. Tokens are server-side revocable via a Redis blocklist.

**Hash and verify through the awaitable wrappers, never `pwd_context` directly.**
`backend/app/utils/passwords.py` exposes `verify_password` / `hash_password` /
`dummy_verify` as coroutines that run the hash in a worker thread via
`asyncio.to_thread`. bcrypt is deliberately ~200 ms of pure CPU per call, so an
inline `pwd_context.verify` in a login handler occupies the event loop for that
whole window and every other in-flight request on the worker waits behind it —
on the most concurrently-hit endpoint in the app, and on *every* attempt, since
the not-found branch pays the same cost through `dummy_verify` for timing
equalisation. The equalisation guarantee is unchanged: both paths take the same
thread hop and the same bcrypt cost. `pwd_context` itself stays the single hash
context (`bcrypt_sha256`); `backend/tests/test_password_hashing_offloaded.py`
asserts the work leaves the loop thread and AST-scans `app/` for direct calls.

### The hash scheme — `bcrypt_sha256`, implemented in-repo

The password is pre-hashed with HMAC-SHA256 before bcrypt sees it, which
side-steps bcrypt's 72-byte truncation: without the pre-hash, any two passwords
sharing their first 72 bytes verify against each other's hash, so an attacker who
guesses the prefix is done. Stored form:

```
$bcrypt-sha256$v=2,t=2b,r=12$<22-char salt>$<31-char checksum>
```

`v=2` is `bcrypt(base64(HMAC-SHA256(key=salt_text, msg=password)))`; `v=1` (read
only, never written) is the pre-2017 `bcrypt(base64(sha256(password)))`, where a
stolen `sha256(password)` lookup table could be replayed against the column.
Plain `$2b$...` hashes written before commit c6a91396 also still verify — the
legacy arm reproduces bcrypt 4.0's 72-byte truncation deliberately, since bcrypt
4.1+ raises on a long secret and that would lock those accounts out of their own
password. `pwd_context.needs_update(hash)` reports which stored hashes are on an
older scheme.

**This is our code, not passlib's.** passlib 1.7.4 has been the last release
since 2020 and cannot import against bcrypt 4.1+ (it reads a deleted
`__about__` attribute and probes the backend with a >72-byte secret), which
froze the project on bcrypt 4.0.1 — a hashing library receiving no upstream
fixes. `app/utils/passwords.py` now implements the scheme directly against
`bcrypt`, byte-for-byte compatible, so no stored credential changed and nobody
was asked to reset a password. `backend/tests/test_bcrypt_sha256_compat.py`
holds hashes passlib itself produced as fixed literals and is the proof;
`docs/decisions.md` §151 has the reasoning.

**CodeQL flags the pre-hash, and the finding is a false positive.** Moving the
scheme in-repo put the SHA-256 step where `py/weak-sensitive-data-hashing` can
see it, so `codeql-python` reports two high-severity alerts on
`_prehash` — "sensitive data (password) is used in a hashing algorithm (SHA256)
that is insecure for password hashing, since it is not a computationally
expensive hash function". The query stops at the SHA-256 call and does not model
what consumes its output. That digest is never stored: `_prehash` returns the
*key* handed to `bcrypt.hashpw` at `DEFAULT_ROUNDS = 12`, so the stored
credential is bcrypt over 2^12 rounds, and the pre-hash only reduces the secret
to a fixed length bcrypt will not truncate. The composition is strictly stronger
than plain bcrypt and is the same one Django ships as
`BCryptSHA256PasswordHasher`. The alerts are dismissed as false positives rather
than worked around: contorting the dataflow to satisfy the query would change
the digest, and the digest matching passlib's byte-for-byte is the only thing
keeping every stored credential verifiable. If they reappear on a later run,
re-dismiss them — do not "fix" them.

## Auth Flow

1. User visits a tenant subdomain (e.g., `acme.localhost:7777`)
2. Frontend extracts the tenant slug from the subdomain
3. User submits email/password at `/login`
4. Frontend POSTs `{ email, password }` to `/api/auth/login`
5. Backend validates credentials against the **control-plane DB** (where users live)
6. Backend returns `{ access_token, token_type }` (JWT with a unique `jti` claim)
7. Frontend stores the JWT in `localStorage`
8. All subsequent requests include both:
   - `Authorization: Bearer <token>` header
   - `X-Tenant-Slug: <slug>` header
9. On 401 responses, the token is cleared and the user is redirected to `/login`

## Backend Implementation

### Login endpoint

`POST /api/auth/login` — accepts email and password, verifies against the hashed password in the **control-plane database**. Returns either a signed JWT (`{access_token, ...}`) or, when MFA is in play, a short-lived challenge token (`{mfa_required: true, mfa_challenge_token, methods, must_enroll}`). See the **MFA** section below for the full flow. This endpoint uses `get_control_db` (not the tenant DB).

### Logout endpoint

`POST /api/auth/logout` — revokes the current token by adding its `jti` (unique token ID) to a Redis blocklist. The blocklist entry expires automatically when the token would have expired, so Redis doesn't accumulate stale entries.

### Current user endpoint

`GET /api/auth/me` — returns the authenticated user's profile including their roles (e.g. `["admin", "ap_manager"]`). Used by the frontend layout to validate the session on page load and determine role-based UI visibility.

`PATCH /api/auth/me` — self-service endpoint for updating name and password (requires current password).

### Protected routes

All endpoints except the explicit allowlist (login flow, OIDC handshake, signup, webhooks, SCIM, health) require a valid Bearer token AND the right role. Authentication and role checks are enforced via FastAPI dependencies in `app/api/deps.py` (`get_current_user`, `require_roles`). See **Role-based access control** below for the full permission matrix.

## Token Revocation (Blocklist)

Stateless JWTs can't be invalidated server-side by default — once issued, they're valid until they expire. To support secure logout, the app uses a Redis-backed token blocklist.

### How it works

1. Every JWT includes a `jti` (JWT ID) — a unique UUID generated at token creation
2. On logout, `POST /api/auth/logout` adds the `jti` to Redis with a TTL equal to the token's remaining lifetime
3. On every authenticated request, `get_current_user` checks Redis for the `jti` — if found, the request is rejected with 401
4. Redis entries auto-expire when the token would have expired, so no cleanup is needed

### Why Redis

- Fast O(1) lookups on every request (sub-millisecond)
- TTL-based auto-expiry keeps the blocklist small
- Already running in the stack for cache/queue duties

### Data stored in Redis

```
Key:    token:blocked:<jti>
Value:  "1"
TTL:    remaining seconds until token expiry
```

### Failure mode

If Redis is unavailable, the blocklist check is skipped — tokens behave as standard stateless JWTs. This is a deliberate choice: a Redis outage should not lock out all users. The trade-off is that tokens cannot be revoked during the outage.

## Session management (concurrent cap + forced logout)

Every successful login, MFA verify, and SSO callback **also** registers the newly-minted JTI in a per-user Redis sorted set (`active_jtis:<user_id>`, scored by issue time). This gives the app two SOC 2-relevant controls on top of the blocklist:

### Concurrent session limit

Configured by `FEOH_MAX_CONCURRENT_SESSIONS` (default `5`; set to `0` to disable). On each login, `track_session` adds the JTI and — if the user is over the cap — evicts the oldest entries and adds them to the blocklist. The evicted sessions stop authenticating on their next request.

### Forced logout on role change

`PATCH /api/admin/users/{id}` snapshots the target's role set before applying changes. If roles change or `is_active` flips from true to false, the admin path calls `revoke_user_sessions(user_id)`, which blocklists every tracked JTI and clears the set. `DELETE /api/admin/users/{id}` does the same on deletion so a tombstoned user can't keep calling the API.

`POST /api/admin/users/{id}/revoke-sessions` is the same forced logout **on its own** — for incident response ("that laptop was stolen; keep the account, kill the sessions"), where the alternative was deactivate-and-reactivate, which locks the user out for the duration and records a suspension that never happened. Org-scoped (a user outside the caller's org is the same 404 as a missing one), gated on `user.manage`, idempotent, and audited as `user.sessions_revoked` with a count.

### Session set TTL

The sorted set carries a TTL equal to the access-token lifetime, refreshed on every login. Inactive users' sets age out naturally — Redis never accumulates stale entries.

Because the TTL is refreshed on *every* login, a set kept alive by recent sign-ins can outlive the individual tokens inside it. `list_sessions` therefore treats an entry whose `issued_at + access-token lifetime` has passed as expired: it is pruned (from both the set and the metadata hash) rather than listed. Pruning does **not** blocklist — there is nothing left to revoke.

The lifetime used is the one **recorded for that session at sign-in**, not the current setting. Shortening `FEOH_ACCESS_TOKEN_EXPIRE_MINUTES` would otherwise make every session minted under the old value look already-expired, pruning it while its token keeps authenticating — leaving a live session the user can neither see nor revoke, which is precisely what this surface exists to prevent. A session tracked before the field existed falls back to the current default.

### How long a revoked token stays blocklisted

A blocklist entry that expires **before** the JWT's own `exp` hands the revoked token straight back — the `is_token_blocked` check simply stops finding it. So the duration is not the current configured lifetime either: `session_management.blocklist_ttl_for(user_id)` derives it from the longest-lived session the user actually has, floored at the current default. Over-blocking is harmless (a Redis key outliving its token costs nothing); under-blocking is a revocation bypass, so this deliberately errs long. It is called *before* the revoke, while the records still exist, and is shared by all three revoke paths (self-service single, self-service others, admin forced logout). `POST /api/auth/logout` doesn't need it — it reads the token's own `exp` directly.

### Session metadata

Alongside the sorted set, each sign-in writes a small JSON record into a companion hash (`session_meta:<user_id>`, field = JTI) holding the client IP, a coarse device label, and the sign-in method (`password`, `password+mfa:totp`, `sso:<provider>`, `saml:<provider>`). Both structures are torn down together — an evicted, logged-out, revoked or pruned session loses its metadata in the same operation, so the descriptive layer can never outlive the membership it describes.

The device label comes from the pure `session_management.describe_user_agent`, which reduces a `User-Agent` to `"Chrome on macOS"`-style text. The **raw** User-Agent is never stored or logged — it is a high-entropy fingerprint, and the label is all the user needs to recognise their own devices. Anything unrecognisable (a CLI, a bot) records no label rather than a guess. The IP is clipped to 64 characters before storage.

### Self-service session visibility + revocation

The threat model throughout this document is a **leaked or stolen access token**. The account holder can now act on it directly:

| Endpoint | What it does |
|---|---|
| `GET /api/auth/sessions` | The caller's own live sessions, newest first — JTI, sign-in time, expiry, IP, device label, method, and `current: true` on the session making the request. |
| `DELETE /api/auth/sessions/{jti}` | Ends one of them. `404` when the JTI isn't in the caller's set — the same answer for "belongs to someone else" and "already gone", so it can't be used to probe whether a JTI is live elsewhere. |
| `POST /api/auth/sessions/revoke-others` | Signs out everywhere except the current session. Idempotent — `revoked: 0` when nothing else is signed in. |

Every operation is keyed on `user.id`, so membership in the caller's own set **is** the authorization check: a JTI from another account is unreachable, not merely refused. Revoking blocklists the JTI (the token stops authenticating on its next request), it does not merely forget it.

There is deliberately **no step-up gate**: all three only ever *remove* access, and the person who needs them needs them fast. Revoking your own current session is allowed — it is equivalent to logging out, so the backend doesn't second-guess which row the user clicked. Both revoke paths write a PII-free `auth.session.revoked` audit row (`{scope, revoked}`); a refused revoke writes none.

`get_current_user` stashes the requesting token's JTI on `user.session_jti` — the same transient-attribute pattern it uses for `effective_permissions` — which is how `current` is marked and how "sign out everywhere else" spares the caller. A caller whose JTI can't be resolved (a legacy token minted before the claim existed) fails toward *less* access: revoke-others signs out everywhere, including them.

The UI is the **Signed-in devices** card on `/profile`, next to the MFA and passkey cards.

### A password change signs out every other session

Changing your password is the action a user takes precisely *because* they
believe a token has been stolen — so it must end the stolen session. The
admin-initiated reset already did (`revoke_user_sessions`, with the stated
rationale that a reset closes exactly that window); the three **self-service**
paths did not, so an attacker's JWT kept authenticating for the rest of its
lifetime while the user reasonably believed they had locked them out. On the
supplier portal it was worse: there is no `/sessions` surface at all, so a
vendor had no way to end a stolen portal session.

`POST /api/auth/change-password` and the `password` branch of
`PATCH /api/auth/me` now call `revoke_other_sessions(user.id, <caller's jti>)`
after the commit — sparing the caller's own session, so they are not logged out
of the tab they just used — and write the existing PII-free
`auth.session.revoked` audit row. `POST /api/portal/auth/change-password`
blocklists the vendor's current JTI and its tracked siblings.

`PATCH /api/auth/me` was also the one password-setting path that never ran
`validate_password_complexity`, and its schema allowed `min_length=6` against
`ChangePasswordRequest`'s 12 — so any user could quietly drop to a
six-character all-lowercase password and defeat the policy tenant-wide. All
four paths now share one `_set_password` helper so a fifth cannot diverge.

### Logout

`POST /api/auth/logout` now both blocklists the current JTI **and** drops it from the tracking set (via `end_session`). A user who logs out gains a free slot under the concurrent-session cap for their next login.

## Brute-force protection — two axes, not one

Every credential-checking endpoint is throttled on **two independent axes**, both
in `app/services/rate_limit.py`. Adding only one of them leaves a usable attack.

**Per client IP** (`check_rate_limit`) — a plain request budget, 10/min on
`/api/auth/login`, `/api/auth/mfa/verify` and their `/api/portal/auth` twins.
Stops one host hammering an endpoint. It cannot see the commodity shape of a
real password spray: one account, thousands of residential-proxy addresses,
each staying comfortably under the cap.

**Per identity** (`check_auth_failures` / `record_auth_failure` /
`clear_auth_failures`) — a **failure** budget keyed on the identity being
authenticated, so it is shared across every source address.

| Surface | Bucket key | Budget |
|---|---|---|
| `POST /api/auth/login` | submitted email | 10 failures / 15 min |
| `POST /api/auth/mfa/verify` | user id | 5 failures / 15 min |
| `POST /api/portal/auth/login` | tenant slug + submitted email | 10 failures / 15 min |
| `POST /api/portal/auth/mfa/challenge` | tenant slug + vendor-user id | 5 failures / 15 min |

The second-factor rows are the load-bearing ones. A TOTP code is six digits
(10^6), and an attacker who reaches `/mfa/verify` already holds the password —
so they can mint fresh challenge tokens indefinitely and spread the guessing
over as many addresses as they care to rent. Only a budget keyed on the account
puts that keyspace out of reach.

Four properties make this safe to put in front of a login form:

- **Failures only.** `check_auth_failures` is read-only; only
  `record_auth_failure` writes, and a correct credential calls
  `clear_auth_failures`. Signing in correctly never accumulates a budget, and a
  legitimate user is never penalised for earlier typos.
- **Keyed on the *submitted* identifier, checked *before* the account lookup.**
  An address with no account throttles identically to one that has an account,
  so the 429 can't become the enumeration oracle the equalised-timing 401 path
  exists to avoid.
- **Hashed** (`auth_identity_key`, SHA-256, case/whitespace-normalised). An
  email address never lands in the Redis keyspace, and varying the
  capitalisation doesn't buy a fresh budget.
- **Tenant-scoped on the portal.** A vendor address is unique only *within* a
  tenant DB; keying on the address alone would let one tenant's traffic throttle
  another tenant's supplier.

**Accepted trade-off:** because the bucket is keyed on the identity, someone who
knows an address can burn that account's *password* budget and delay a
legitimate sign-in. It is a rolling window (never a sticky lock), it self-heals,
and it does not touch the SSO or passkey paths — the same call every mainstream
implementation makes, and strictly better than leaving the spray unbounded.

Both axes honour the `FEOH_RATE_LIMIT_ENABLED` master switch (CI's e2e job turns
it off — every shard's workers log in from one loopback address).

Emailed MFA backup codes are capped separately, per **account**, at 10/hour
(`EMAIL_OTP_PER_ACCOUNT_PER_HOUR`) on both surfaces: the per-IP limit there
exists to stop the user's inbox being weaponised, and one valid challenge token
replayed from rotating addresses walked straight past it.

Separately, changing an existing second factor is throttled 5/min per **account**
(`_throttle_step_up`) — see [Per-user enrollment](#per-user-enrollment).

## Auth event audit logging

Every auth action writes a row to the tenant `audit_log` table via `app/services/audit_dispatch.py::dispatch_auth_audit`. Because auth endpoints run on the control-plane DB (where users live) but the audit log is tenant-scoped, the helper resolves the tenant DB from the user's `organization_id` and opens a short-lived session to write the row. Failures are caught + logged at WARN level (class name only — `details` carries the submitted address) — auth itself never errors because the audit write couldn't complete. `queue_auth_audit` is the same write scheduled as a task instead of awaited, for the one branch whose response time must not depend on whether the account exists — see [The failure row is written off the response path](#the-failure-row-is-written-off-the-response-path).

Action names:

| Event | Action |
|---|---|
| Successful password login | `auth.login.success` |
| Failed password login (bad password / no password) | `auth.login.failure` |
| Logout | `auth.logout` |
| Self-service session revoke (one, or everywhere else) | `auth.session.revoked` — PII-free, records only `{scope: "single"\|"others", revoked: <count>}` |
| Admin force-logout of another user | `user.sessions_revoked` — PII-free, records only the revoked count |
| MFA challenge issued during login | `auth.mfa.challenge_issued` |
| Successful MFA verify | `auth.mfa.verify.success` |
| Failed MFA verify | `auth.mfa.verify.failure` |
| Failed step-up against a second-factor change (enroll / passkey register / passkey delete / disable) | `auth.mfa.step_up.failure` (employee) · `portal.mfa.step_up.failure` (supplier portal) — PII-free, records only the operation name |
| TOTP enrollment completed (`/mfa/enroll/verify`) | `auth.mfa.enrolled` (employee) · `portal.mfa.enrolled` (supplier portal) — PII-free, records `{factor: "totp", replaced: <bool>}`. `replaced` says whether it displaced a live authenticator |
| TOTP turned off (`/mfa/disable`) | `auth.mfa.disabled` (employee) · `portal.mfa.disabled` (supplier portal) — PII-free, records `{factor: "totp"}` |
| Passkey registered (`/mfa/passkey/register/verify`) | `auth.mfa.passkey.registered` — PII-free, records `{factor: "passkey", credential, name, rp_id}`. `credential` is the API-visible passkey id (`webauthn_credentials.id`), never the authenticator's credential handle or its public key |
| Passkey removed (`DELETE /mfa/passkey/{id}`) | `auth.mfa.passkey.removed` — same shape, so the removal row joins to the registration row on `credential` |
| Failed supplier-portal password login | `portal.login.failure` — records `{ip, reason}` (`bad_password` \| `no_password` \| `inactive`) and identifies the account by `entity_id`, never by address (a supplier contact's email is third-party PII we don't restate on every guess). Queued off the response path, like its employee twin |
| Successful supplier-portal password login | `portal.login.success` — PII-free, records `{ip, method: "password"}` and identifies the account by `actor_id`/`entity_id`, never by address (same rule as the failure row). **Awaited**, unlike the failure row: the account provably exists by then, so the tenant-DB round trip cannot become the account-existence oracle `queue_auth_audit` avoids. Written on BOTH sign-in paths: the password-only one, and `/portal/auth/mfa/challenge` once a second factor is enrolled (as `password+mfa:{factor}`, beside that stage's own `portal.mfa.verify.success`). One action is therefore the whole population — an earlier version wrote it only on the password path, which made the action a biased sample rather than a gap, silently omitting exactly the best-protected accounts. Mirrors the employee twin, which writes `auth.login.success` alongside `auth.mfa.verify.success` |
| Supplier-portal second factor verified / rejected (`/portal/auth/mfa/challenge`) | `portal.mfa.verify.success` · `portal.mfa.verify.failure` — PII-free, records `{method, ip}` where `method` is `totp` \| `email`. The portal twin of `auth.mfa.verify.*`; see [the note below](#the-supplier-portals-second-factor-stage-is-on-the-trail-issuing-a-backup-code-is-not) for why issuing an email backup code has no row of its own |
| Successful SSO login | `auth.sso.login.success` |
| Failed SSO login (code exchange / ID token / domain blocked) | `auth.sso.login.failure` |
| Successful SAML login | `auth.saml.login.success` |
| Failed SAML login (assertion invalid / issuer / unsolicited / replay / domain blocked) | `auth.saml.login.failure` |

Login-failure rows for unknown emails are dropped — without an `organization_id` there is no tenant DB to route to. Failures for known users carry the email, IP (when the client is reachable), and a machine-readable `reason`. (The supplier-portal twin, `portal.login.failure`, deliberately omits the address — see the action table above.)

#### The supplier portal's second-factor stage is on the trail; issuing a backup code is not

`/portal/auth/login` audits both outcomes — `portal.login.failure` on rejection
and `portal.login.success` once the session is minted. Once a supplier enrols a
second factor it stops minting the token altogether and hands back a challenge,
so the sign-in *completes* at `/portal/auth/mfa/challenge` — which meant that
turning MFA on for a supplier account took that account's sign-in off the trail
entirely, in exchange for a stronger credential. That handler now writes
`portal.mfa.verify.success` / `.failure`, matching its employee twin
(`api/auth.verify_mfa`), which has audited both outcomes since it was built. The
per-account failure budget that throttles guessing there is a Redis rolling
window — a brake, not evidence — and a portal account can stage a vendor
bank-detail change, so a campaign against its second factor is exactly what an
auditor is looking for.

**`POST /portal/auth/mfa/challenge/email` is deliberately NOT audited**, and
that is a decision rather than a gap:

* The auditable event is the backup code being **redeemed**, and the row above
  records it with `method: "email"` — so a code that was issued and never used
  is already visible as an absence, and a row at issue time would only ever
  duplicate a delivery the per-IP and per-account send caps already bound.
* It answers `204` on **every** path so it cannot be used to discover which
  supplier addresses exist and are enrolled. A row would be written for exactly
  that set, rebuilding the oracle inside a trail that is also shipped to a WORM
  store.
* Its employee twin (`POST /api/auth/mfa/challenge/email`) is unaudited on the
  same terms. Auditing only the supplier surface would be a knowingly
  asymmetric control.

The `method` on both rows is derived from the branch the handler actually took,
never echoed from the request. `PortalMFAChallengeVerifyRequest.method` is an
unconstrained `str` (its employee twin pins `^(totp|email)$`), so anything other
than `"email"` falls through to TOTP — restating it would put unbounded
caller-controlled text into an append-only, WORM-shipped trail and label the row
with a factor nobody used.

Pinned by `tests/test_portal_mfa.py`, and by the reason string on the one
remaining `portal_auth` entry in `tests/test_audit_append_only.py`'s exemption
dict — the dict whose sibling `_OPEN_AUDIT_HOLES` exists precisely to keep
"we decided not to" from being filed as "we have not done this yet".

#### The failure row is written off the response path

`/api/auth/login` used to `await dispatch_auth_audit(...)` on a rejection — a
control-plane lookup for the tenant DB name, then a tenant session, connect and
commit. Only an address that *has* an organization can do any of that, so a
rejection against a known account (wrong password, no password hash,
deactivated) was a whole DB round trip slower than a rejection against an
unknown one: an account-existence oracle sitting one line below the
`dummy_verify()` call that exists to close exactly that channel.

Neither obvious repair is acceptable. Dropping the row loses the only record
that an account is being attacked. Padding the unknown-address branch to match
is masking a defect rather than removing it. So the write moved **off the
response path**: `audit_dispatch.queue_auth_audit` spawns it as a task and
returns without awaiting, so *neither* branch pays for it, and both rejections
now funnel through one shared tail (`api/auth.py::_reject_login`) — one awaited
call (`record_auth_failure`), one non-awaited `queue_auth_audit`, one 401 — so
they are identical by construction rather than by review.
`tests/test_login_audit_off_response_path.py` asserts on the recorded sequence
of awaited operations, not on a clock.

Notes on the mechanism:

- **It is not `services/post_commit`.** That queue fires from SQLAlchemy's
  `after_commit`, and a failed login raises `HTTPException`, so `get_control_db`
  rolls its session back — `after_rollback` drains and discards the queue by
  design. A row queued there would never be written. The trigger here is the
  event loop: the task runs while the response is already on its way out.
- **`organization_id=None` is passed, not branched on.** An unknown address
  still has nowhere to route a row, so the drop is real and stays — but the call
  site is reached identically from both rejections, which is the point.
- **A lost row is loud.** `queue_auth_audit` wraps the write in its own guard
  that logs at **ERROR** (not WARNING): this is SOX evidence for an
  authentication event and nothing downstream retries it. The log carries the
  action, the org UUID and the exception **class name** only — never `details`,
  which carries the submitted address. The guarantee is "written shortly after
  the response, or reported at ERROR", not "written before the response
  returns".
- **A clean shutdown flushes the queue.** The lifespan's `finally`
  (`app/main.py` — the production entrypoint `app.main:app`, which the local-dev
  `main.py` also runs) awaits `drain_auth_audits(...)` before
  `dispose_all_engines`, since the write commits through the tenant engine that
  call disposes. **Bounded at `AUTH_AUDIT_DRAIN_TIMEOUT_SECONDS` (5s):** a hung
  write costs the shutdown a known five seconds and is then cancelled, with the
  abandoned **count** logged at WARNING. Unbounded, one stuck write would block
  the shutdown until an orchestrator SIGKILLs the process — losing every *other*
  queued row too, not just the stuck one. A hard kill still loses in-flight
  rows, the same exposure every other fire-and-forget leg carries
  (`services/post_commit`, `services/webhooks/dispatch`).
- **Only the login *failure* rows moved.** Success, MFA-challenge-issued and
  the SSO-only refusal still `await dispatch_auth_audit` — they all sit *after*
  a correct password, so none of them is an existence oracle.
- **The supplier portal had the identical defect, and the identical fix.**
  `POST /api/portal/auth/login` awaited `dispatch_auth_audit` only when the
  address resolved to a vendor user, so a known supplier address paid for a
  control-plane lookup plus a *second* tenant session (the dispatcher opens its
  own, separate from `get_tenant_db`'s) that an unknown one skipped — despite
  the portal's differently-shaped, tenant-scoped failure budget. Both rejections
  now funnel through `api/portal_auth.py::_reject_portal_login` with the same
  queued write. A legacy vendor-user row with no `organization_id` used to
  `return` from inside the audit helper *after* entering the await; it now lands
  on the same `organization_id=None` branch as an unknown address, so it is not
  a third timing class. The row's contents are unchanged — still identified by
  `entity_id`, never by address.

### Database separation

- **Auth routes** (`/api/auth/*`) read from the control-plane DB (`feohledger`) — this is where `users`, `organizations`, and `roles` tables live
- **Business routes** (`/api/invoices`, `/api/vendors`, `/api/dashboard`) read from the tenant DB (`feoh_<slug>`) — resolved via the `X-Tenant-Slug` header

### Cross-tenant guard

`app/tenant.py::get_tenant` is the single chokepoint every business
route flows through. It refuses to resolve a tenant if the caller's
JWT identifies a different organization than the slug — the header
alone is **not** trusted to decide which tenant's data is read.

For employee tokens (`typ` is anything other than `"vendor"`), the
guard requires `payload["org"] == organization.id`. Mismatch → 403.
Vendor-portal tokens (`typ="vendor"`) are exempt because `VendorUser`
rows live in the per-tenant DB and a cross-tenant attempt fails
naturally on the user-lookup query in
`app/api/portal_deps.py::get_current_vendor_user`. Unauthenticated
requests pass through to the downstream auth dep (which 401s).

Without this guard, an authenticated user from tenant A could read
or mutate tenant B's data simply by sending
`X-Tenant-Slug: <other-tenant>` on the request. The guard is pinned
by `backend/tests/test_tenant_isolation.py` (unit) and
`frontend/tests-e2e/auth/tenant-isolation.spec.ts` (e2e).

## SAML SSO

SAML 2.0 is an **additive, separate** SSO code path (`app/api/auth_saml.py`)
alongside OIDC. Both protocols share the same per-tenant `Organization.settings.sso`
block, discriminated by a `protocol` key (absent / `"oidc"` → OIDC; `"saml"` →
SAML), and the same identity tail: JIT provisioning + JWT mint + session
registration (`app/services/identity_provisioning.py`). Only *verification*
differs. Local testing: [`local-sso-saml.md`](local-sso-saml.md).

**SP-initiated flow.** `GET /auth/saml/login[?slug=]` builds the AuthnRequest and
302s to the IdP, binding a single-use Redis **RelayState** to `{tenant,
AuthnRequest-ID}`. `slug` is optional: on a white-label vanity host the tenant is
resolved from the request `Host` instead (see *SSO on a white-label vanity host*
under [SSO (OIDC…)](#sso-oidc-via-okta--microsoft-entra) — the SP EntityID and
ACS URL are unaffected, both stay derived from `FEOH_API_PUBLIC_URL`). The IdP HTTP-POSTs the signed `SAMLResponse` to
`POST /auth/saml/acs`; the handler recovers the tenant from the RelayState
(**never** the assertion or a header), verifies the response, JIT-provisions, and
303-redirects to the SPA bridge `/login/saml-callback?code=<once>`. The bridge
`POST`s the one-time code to `/auth/saml/exchange`, which returns the JWT **in the
body** — the token never transits a URL.

**Verification (the load-bearing control).** Delegated to `python3-saml` but
pinned to an explicit hardened posture (the library defaults are unsafe):

- Trust anchor = the tenant's pre-registered `idp_x509_cert` (`x509certMulti` for
  rotation) — never a fingerprint, never a cert embedded in the document. An
  empty/blank/malformed cert fails closed at config resolution.
- `wantAssertionsSigned` + `rejectDeprecatedAlgorithm` (rejects SHA-1/none),
  SHA-256 sig/digest — the SAML analog of the OIDC `ID_TOKEN_ALGORITHMS` pin.
- Issuer pinned to the configured IdP; Audience (= per-tenant `sp_entity_id`) and
  Destination validated from trusted server config, not spoofable `Host` /
  `X-Forwarded-*`.
- InResponseTo **presence** is enforced in the handler (the library only checks it
  when present, so an unsolicited response would otherwise pass).
- Replay dedup of both Assertion + Response IDs, scoped per-tenant
  (`saml:<slug>`), via the shared webhook `SET NX` ledger.
- All IdP XML parsed with python3-saml's DTD/entity-hardened parser (no raw lxml
  → no XXE).

Every rejection fails closed to one generic error + a PII-safe
`auth.saml.login.failure` audit row (reason code only — the email is **omitted**,
tighter than the OIDC `domain_blocked` precedent). The IdP owns MFA, so the app's
MFA challenge is skipped (same as OIDC).

**Provider-discriminated matching.** JIT match is `(sso_provider,
sso_provider_id)` then `(org, email)`. SAML uses `provider="saml"`,
`sso_provider_id=NameID`. A tenant migrating a user from OIDC to SAML won't match
on the durable key the first time — it links by email and rebinds the provider
deterministically. Intentional; no migration needed (config is additive JSONB).

**Config (`settings.sso`, `protocol="saml"`):** `idp_entity_id`, `idp_sso_url`,
`idp_x509_cert` (required), `sp_entity_id` (defaults per-tenant), optional
`idp_x509_cert_multi`, `allowed_email_domains`. The optional SP signing keypair
(only when the IdP requires signed AuthnRequests) is a real secret → `FEOH_SAML_SP_*`
via sops; empty by default so local Keycloak runs with no SP keypair.

### Every admin-supplied SSO URL is SSRF-guarded

`settings.sso.discovery_url` is admin-supplied config that the backend `GET`s
directly, and the discovery document's `token_endpoint` (a `POST` target
carrying the client secret and auth code) and `jwks_uri` (which supplies the
key an ID token is verified against) were then used verbatim. None passed
through `utils/url_safety.assert_public_url` — the guard every *other*
admin-supplied URL this app fetches goes through (branding logo, chat
webhooks, ERP and enrichment base URLs).

`url_safety` exposes the guard in two forms over **one** policy body:
`assert_public_url` / `is_public_url` (sync, blocking `socket.getaddrinfo`) for
sync call sites, and `assert_public_url_async` / `is_public_url_async` (name
resolution via `loop.getaddrinfo`) for every `async def` one. Every coroutine
call site — SSO, the chat-webhook admin save, the Slack/Teams `send()`, the
D365 and enrichment adapters — uses the awaitable pair, because a blocking DNS
lookup on the event loop stalls *every* concurrent request for the length of
the lookup, and a DNS timeout is measured in seconds.

That is reachable **unauthenticated**: anyone can self-signup a tenant, `PATCH`
an `sso.discovery_url` of `http://169.254.169.254/...`, and hit the public
`GET /api/auth/sso/authorize?slug=<tenant>` to make the backend fetch cloud
instance credentials on demand — or port-scan the VPC by telling reachable
hosts apart from dead ones by the 302-vs-400 response.

Now: `assert_public_url_async` runs at `resolve_sso_config` time (so a bad value
is refused where it is read, not only where it is fetched) and again in
`fetch_discovery` / `fetch_jwks` / before the token `POST`. `_pinned_endpoint`
additionally pins `token_endpoint` and `jwks_uri` to the discovery document's
own issuer netloc — OIDC Discovery requires the document to be served under its
own `issuer`, and this mirrors the check `api/auth_sso.py` already applied to
`authorization_endpoint`, so a compromised or mis-served document cannot
redirect either fetch off-host.

**Local-first carve-out** (guard rail 7): the documented local IdP is Keycloak
on `http://localhost:8088`, and a loopback address is exactly what the guard
rejects. Outside a deployed environment (`settings.is_deployed`, the same
discriminator the extraction-provider fallback uses) an unroutable address logs
a warning instead of refusing. The URL *shape* check is unconditional either
way, so a `file://` URL is refused in dev too. Refusals are PII-free: they name
the field, never the URL (it can carry a tenant identifier) and never the
resolved address.

## SSO-only mode

`settings.sso.sso_only` (a per-tenant flag, shared by OIDC + SAML) closes
password login for the whole org. The `/api/auth/login` handler refuses with
`403` + an `auth.login.failure` / `reason=sso_only` audit row **after** verifying
the password (so it reuses the org load and doesn't perturb the unknown-vs-
wrong-password enumeration parity). `services/sso.py::is_sso_only` gates on
`sso.enabled` too, so setting the flag without a working IdP can't lock everyone
out. The public `/auth/{sso,saml}/config` endpoints echo `sso_only` **only when
the IdP config resolves** — so the login page hides the password form for an
SSO-only tenant, but a broken config (enabled=False) leaves password login
visible as the escape hatch. Backend enforcement is the security boundary; the
hidden form is UX.

## Frontend Implementation

- Auth state is managed in `src/lib/stores/auth.svelte.ts` (Svelte 5 runes)
- Tenant slug is extracted by `src/lib/tenant.ts` from the subdomain
- API client (`src/lib/api.ts`) automatically attaches both the Bearer token and the `X-Tenant-Slug` header to every request
- The root `+layout.svelte` guards all routes — unauthenticated users are redirected to `/login`
- If no tenant subdomain is detected, a "no tenant" page is shown instead
- The login page has its own layout without the sidebar

## Configuration

| Variable        | Default                    | Description               |
|-----------------|----------------------------|---------------------------|
| `FEOH_SECRET_KEY` | `change-me-in-production`  | JWT signing key           |
| `FEOH_ACCESS_TOKEN_EXPIRE_MINUTES` | `30`      | Token lifetime in minutes |
| `FEOH_REDIS_URL`  | `redis://localhost:6379`   | Redis URL for blocklist + active-session tracking |
| `FEOH_MAX_CONCURRENT_SESSIONS` | `5`        | Concurrent sessions per user. `0` disables the cap. |

Set `FEOH_SECRET_KEY` to a strong, random value in production.

## RBAC (Role-Based Access Control)

The database supports four roles:
- **admin** — full access to all features, user management, workflow configuration
- **ap_manager** — review and approve invoices, manage vendors and payments
- **ap_clerk** — upload and edit invoices, submit for review (cannot approve, delete, or change status)
- **cfo** — approve high-value invoices, view reports, manage vendors and payments

Roles are returned by `GET /api/auth/me` in the `roles` array, and the user's
effective **granular permissions** in the `permissions` array. The frontend uses
roles to control sidebar navigation visibility and most button/action
availability, and `auth.can(perm)` for the *split* sensitive controls (see
[user-management.md](user-management.md) for the full matrix and § Granular
permissions / segregation of duties below).

Backend API-level enforcement is via `Depends(require_roles(...))` on most
protected endpoints and `Depends(require_permission(...))` on the
fraud-sensitive, SoD-splittable subset (see below). The full permission matrix
is in the **RBAC** section below. Frontend gates exist for UX (hiding buttons,
sidebar items) but are not the security boundary — the backend is.

### Granular permissions / segregation of duties

The four system roles bundle whole sets of duties, which conflates
fraud-sensitive ones — e.g. one `ap_manager` could both **approve a vendor
bank-detail change** and **execute a payment run** (the person who can redirect
where money goes can also send it). A granular **permission layer**
(`backend/app/api/permissions.py`) lets an org **split** these duties via custom
roles. It is additive and backward-compatible: the four system roles behave
exactly as before.

- **Catalog** — a small, deliberately-scoped set of *splittable* permission
  constants (dotted strings): `invoice.approve`, `payment_run.approve`,
  `payment.execute`, `payment.void`, `vendor.bank_change.approve`,
  `vendor.block`, `vendor.manage`, `user.manage`. `GET /api/admin/permissions`
  returns the catalog (key + label) for the role editor — gated `user.manage`
  (see below), same as its sibling reads. Everything not in the catalog stays
  on `require_roles`.
- **System-role defaults** — a static map (`ROLE_DEFAULT_PERMISSIONS`)
  reproduces today's matrix exactly: `admin` holds all; `ap_manager` holds
  invoice approve + run approve/execute + vendor bank-change/block/manage (NOT
  payment void); `cfo` holds invoice approve + run approve/execute + payment
  void; `ap_clerk` holds none.
- **Custom-role permissions** — a control-plane JSONB column `roles.permissions`
  (migration `0062_role_permissions`, control-plane-only — `roles` is
  control-plane). System roles leave it NULL (they resolve via the default map);
  custom roles store an explicit, sanitized list. A custom role with an empty
  list grants nothing (the inert pre-feature default).
- **Effective permissions** — the union over all the user's roles (system via
  the default map, custom via their stored list). Computed once in
  `get_current_user` (cached on `User.effective_permissions`), exposed on
  `GET /api/auth/me`'s `permissions` array, and enforced by
  `require_permission(*perms)` (any-of semantics, 403 on miss, WARN log; typos
  rejected at import time).
- **Migrated endpoints** — only the splittable sensitive set moved to
  `require_permission`: payment-run create (`payment_run.approve`) — its
  sibling `POST /api/payments/runs/{id}/approve` (the CFO sign-off above the
  org's dollar threshold) deliberately stays on `require_roles(ROLE_CFO)`
  rather than sharing that permission: the two "approve" names look like
  siblings but aren't — granting the create-draft permission's default
  holders (admin/ap_manager) the sign-off too defeats the control a genuine
  CFO signature exists to provide — payment-run
  execute (`payment.execute`), payment void (`payment.void`), vendor
  create/update/verify/reject/delete + the bulk vendor-create paths (ERP sync,
  CSV import) + the manual re-screen trigger + the consolidation-merge execute
  (`vendor.manage` — every one of these had a role set of exactly
  admin/ap_manager already, so migrating them changes nothing for the four
  system roles), vendor block/unblock (`vendor.block`), vendor bank-change
  approve (`vendor.bank_change.approve` — its sibling
  `POST /vendors/change-requests/{id}/reject` deliberately stays on
  `require_roles(ADMIN, AP_MANAGER)`: reject never touches the vendor row, so
  it isn't the fraud-sensitive action the permission exists to gate — see
  "Approve and reject are not always the same role set" below), and user
  create/update/delete/bulk-delete (`user.manage`). The vendor list/detail/
  counts reads and the change-request queue's own `GET /vendors/change-requests`
  stay on `require_roles` too — broader than any one splittable permission
  (cfo can read vendor detail without holding `vendor.manage`), matching the
  same "reads stay coarse, only the specific action splits" pattern payments.py
  uses for `GET /payments/queue` etc. Role/permission CRUD itself stays
  admin-only on `require_roles` (managing the catalog must not be a grantable
  permission — that would be a privilege-escalation path).
  - **One migration deliberately does NOT reproduce the prior matrix.**
    `POST /api/cards/{id}/cancel` was `require_roles(ADMIN, AP_MANAGER, CFO)`
    and is now `require_permission(payment.void)`, so the system `ap_manager`
    **loses** it. Everywhere else "migrating changes nothing for the four
    system roles" holds; here the old role set was the bug. Three routes close
    a virtual card — this one, `POST /api/payments/{id}/void`, and
    `POST /api/payments/{id}/void/retry-card-cancel` — and the latter two gate
    on `payment.void`, which `ap_manager` does not hold by default (it holds
    `payment.execute`: initiating money, not reversing it). So an `ap_manager`
    who could not reverse a card payment could still kill the card behind a
    live one, through the widest of three doors onto one effect.
    `docs/decisions.md` §132 stated the rule for the retry route; this applies
    it to the door left open, and §96 is why nothing in the UI regresses — the
    route is deliberately unwired. Pinned in
    `tests/test_sod_endpoint_wiring.py` by
    `test_card_cancel_narrows_ap_manager_by_design` (the four-role table) and
    `test_every_card_closing_route_gates_on_payment_void` (the three doors
    agree). See `backend/docs/virtual-cards.md` § Cancel → The gate is
    `payment.void`.
  - **The reads a `user.manage` holder needs to actually use the grant are
    migrated too.** `GET /api/admin/users` (the roster), `GET /api/admin/roles`
    (the picker `role_names` is chosen from) and `GET /api/admin/permissions`
    (the key→label lookup for the permission keys `GET /api/admin/roles`
    already echoes) are all `require_permission(PERM_USER_MANAGE)` — read-only,
    and the default holder set (`{admin}`) is byte-for-byte the same set
    `require_roles(ROLE_ADMIN)` produced, so this is a no-op for the four
    system roles and additive only for a custom role. `POST`/`PATCH`/`DELETE
    /api/admin/roles` (defining what a role CAN grant) stay
    `require_roles(ROLE_ADMIN)` — deliberately NOT migrated, because minting or
    editing a role definition can bundle any catalog permission at all,
    including ones the definer doesn't hold; that's a materially stronger
    capability than assigning an *existing* role to a user (which
    `_authorize_role_grant` already bounds to the grantor's own permissions),
    and granting it via `user.manage` would let a "user admin" custom role
    mint itself a role that grants everything, sidestepping the grant guard by
    never touching a `role_names` field.
  - **Role-grant is bounded by the grantor.** `user.manage` lets you assign
    roles, but not roles more powerful than you hold: `_authorize_role_grant`
    refuses to grant the system `admin` role unless the caller is themselves an
    admin, and refuses to grant any role whose catalog permissions aren't a
    subset of the caller's own effective permissions. Otherwise a custom role
    scoped to only `user.manage` could assign itself `admin` and take over the
    org.
  - **Admin-set passwords obey the complexity policy.** An admin resetting a
    user's password (`PATCH /api/admin/users/{id}` `password`) runs it through
    the same `validate_password_complexity` as self-service change-password
    (min 12, upper/lower/digit).
  - **You cannot act on an account more privileged than your own.**
    Complexity alone never stopped the takeover it was once described as
    stopping — the actor *chooses* the password, so how strong it is is
    irrelevant to whether they can then use it. `_authorize_role_grant` guarded
    only the `role_names` branch, so a holder of the documented `user.manage`
    custom role could `PATCH` an org admin's **password**, watch
    `revoke_user_sessions` end the real admin's sessions, and sign in as them —
    or simply `DELETE` the admins, or revoke their sessions on a loop.
    `_authorize_target_mutation` now runs on `password`, `is_active`, `email`,
    role removal, `DELETE /api/admin/users/{id}` and
    `POST /api/admin/users/{id}/revoke-sessions`: it refuses (403) when the
    TARGET holds the system `admin` role and the caller does not, or when the
    target's effective permissions are not a subset of the caller's. It shares
    `permissions_for_role` / `effective_permissions` with the grant guard, so
    the two rules cannot drift.
  - **You cannot lock the org out of its own admin account.**
    `_authorize_target_mutation` only stops a caller from touching someone
    ELSE's account with more authority than they hold — self-mutation always
    passes it trivially. That left the org's sole admin free to strip their
    own `admin` role or deactivate their own account via `PATCH
    /api/admin/users/{id}`, a one-way door: recovering needs `user.manage`,
    which by default only an admin holds. `_guard_against_last_admin_lockout`
    closes it: before any field is mutated, if the target is currently an
    active admin and the update would make them not one (role removal,
    deactivation, or both), the request is refused (409) unless another
    active admin already exists in the org to undo it. Applies to self and
    third-party targets alike, though for a third party it's a no-op — acting
    on an admin already required the caller to be one themselves, so the org
    never actually reaches zero admins in that case.
- **Frontend** — `auth.can(perm)` mirrors `require_permission`; the gated
  controls converted so far are payment Execute, payment Void, vendor
  Block/Unblock (`VendorModal.svelte`), vendor create/edit + the consolidation
  merge action (`vendor.manage` — `/vendors/+page.svelte`,
  `VendorConsolidationModal.svelte`), the bank-change-approval queue's
  Approve button (`vendor.bank_change.approve` — `/vendors/change-requests/
  +page.svelte`; its Reject button stays on the page's role-based
  `auth.isManager` gate, matching the backend), and the Users page/nav entry
  (`user.manage`). The `/admin/roles` editor renders permission checkboxes
  from the catalog and shows each custom role's grants. This composes with
  the instance-level SoD check (`check_segregation`, approver ≠ creator),
  which is unchanged.
  - **Page-level route access (nav visibility, the `auth.isManager`/role
    redirect guards) mirrors whatever `require_roles` the page's READ
    endpoint uses by default** — only the specific sensitive ACTION checks
    move to `auth.can(perm)`. The vendor pages above follow that default.
  - **A nav `roles` gate is per ENTRY, read off that route's own backend
    gate — never one list copied across a group.** A group's children
    routinely disagree: in Procurement, `GET /api/purchase-orders` and
    `GET /api/goods-receipts` (and `GET /api/inspections`, behind the same
    page's second tab) are `get_current_user` — auth-gated, role-open —
    while every `/api/budgets` read is
    `require_roles(ADMIN, AP_MANAGER, CFO)`. Those five rows shared one
    admin/ap_manager/cfo list, so an `ap_clerk` was hidden from two pages
    whose own code says a clerk reads them (`goods-receipts/+page.svelte`:
    "a clerk sees every inspection and no button"). `src/lib/nav.test.ts`
    now pins the exact link set each system role sees in that group.
  - **Exception: a permission-gated control is unreachable if the nav row
    that leads to it is still role-only.** `NavLink`/`NavChild` take an
    optional `permissions?: string[]`, OR'd with `roles` in
    `canSee`/`isEntryVisible`/`visibleChildren`/`groupHref` (all take an
    optional `can: PermissionCheck` alongside the existing `has: RoleCheck`);
    `Sidebar.svelte` and `SectionTabs.svelte` pass `auth.can`. Two nav
    entries need this: **Payments** — a custom role holding ONLY
    `payment.execute` (no `admin`/`ap_manager`/`cfo`) can call every backend
    endpoint the `/payments` page needs, so the sidebar row carries
    `permissions: [PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID]`.
    **That claim used to be false, and the nav row was the worse half of the
    bug**: widening a nav entry means checking what the PAGE loads on mount,
    not just the endpoint the row is named after. `/payments`' mount effect
    fires `GET /api/payments/summary`, `/api/payments/queue` and — on the
    default tab — `/api/payments/queue/ids` unconditionally, and all three
    were still `require_roles(ADMIN, AP_MANAGER, CFO)` while their siblings
    migrated, so the row led a permission-only holder to three 403s on first
    paint. All three now gate on
    `require_permission(PERM_PAYMENT_EXECUTE, PERM_PAYMENT_VOID)` like the
    `GET /api/payments` reads beside them, which reproduces the prior role
    footprint exactly (`ap_clerk` holds neither permission and is still
    refused). `backend/tests/test_sod_endpoint_wiring.py` pins all three, and
    its `test_every_permission_gated_route_is_pinned` makes forgetting one a
    failure rather than a silent gap. **Users** — 
    `GET /api/admin/users` migrated to `require_permission(user.manage)`,
    so a custom role holding only that permission needs the tab too —
    carries `permissions: [PERM_USER_MANAGE]`; the sibling Roles tab
    deliberately does NOT (role CRUD stays `require_roles(ROLE_ADMIN)` — see
    above). `src/lib/nav.test.ts` pins that a permission-only holder (no
    role at all) sees each row and a holder of neither does not. The vendor
    pages above didn't need this treatment — their read endpoints stayed on
    the same role set their actions default to, so no custom-role holder
    loses reachability.

### Segregation of duties on a vendor bank change (the BEC gate)

A staged `VendorChangeRequest` is applied by
`POST /api/vendors/change-requests/{id}/approve`, which refuses a proposer who
is also the approver. That check compared **one** column —
`requested_by_user_id`, the AP-side requester — and that column is NULL for
every request submitted through the supplier portal. The comment beside it
stated the assumption it rested on: *"portal-submitted requests have no AP
requester, so this only bites AP-initiated ones."* True only if AP cannot create
portal identities.

AP can. `POST /api/vendors/{id}/portal-users` is
`require_roles(ADMIN, AP_MANAGER)`, and `ROLE_AP_MANAGER` holds `vendor.manage`,
`vendor.bank_change.approve` **and** `payment.execute` by default — so one
person could invite a portal user at an address they controlled, sign in as it,
stage a bank redirect, approve their own request through the NULL
short-circuit, and pay it. The compensating `fraud_flag` raised on the
vendor's in-queue invoices used not to be a second control against the same
actor, because exception resolution had no segregation check either. It is one
now — the approver is recorded as the flag's raiser and refused its clearing
verbs (see [Segregation of duties on the exception
queue](#segregation-of-duties-on-the-exception-queue) below).

The approve path now refuses on **both** axes an AP actor can be the proposer:

| Column | Set when | Refuses |
|---|---|---|
| `vendor_change_requests.requested_by_user_id` | AP staged the change from the app | approver == that user |
| `vendor_change_requests.requester_provisioned_by_user_id` | frozen at staging from `vendor_users.provisioned_by_user_id` | approver == the AP actor who minted that portal identity's password |

`vendor_users.provisioned_by_user_id` is stamped by invite AND by
`POST .../reset-password` — the only two routes that hand an AP actor a working
supplier credential — and is deliberately NOT cleared when the supplier changes
their own password (that route needs the *current* password, which the
provisioner has, so clearing there would be a one-request bypass). NULL means
no AP actor has ever held this credential, which is correctly permissive: a
self-managed supplier's request approves normally. Migration `0095`, tenant DBs.
Full reasoning, including why the value is frozen rather than joined:
[`backend/docs/supplier-portal.md`](../backend/docs/supplier-portal.md) §
Credential provenance and the BEC dual control.

The credential itself no longer travels in the invite/reset response — it is
emailed to the portal user's own address, and a delivery failure rolls the
request back rather than leaving an undeliverable account. That is
defence-in-depth, not the control: an AP actor who supplies their own address
still receives the email. The approval refusal is what closes the chain.

### Segregation of duties on the exception queue

Clearing a payment-**blocking** exception is a money-path authorisation, not
queue hygiene. `api/payments.PAYMENT_BLOCKING_EXCEPTION_TYPES` — `duplicate`,
`fraud_flag`, `line_total_mismatch`, `payment_reconciliation` — stops a payment
run while the row is `open` **or** `escalated`, and invoice approval gates on
none of them. So resolving or dismissing one is the human sign-off that lets a
flagged payable be paid, and `POST /api/exceptions/{id}/resolve` and
`/bulk/resolve` used to gate on `require_roles(ADMIN, AP_MANAGER)` and nothing
else.

The check lives in `services/exception_lifecycle.segregation_refusal`, enforced
by `record_decision` — the one chokepoint the single-row route, `/bulk/resolve`
**and** the agent coordinator all pass through, so the three doors cannot drift.
It refuses on **two** axes, either of which is enough:

| Axis | Read from | Refuses |
|---|---|---|
| the payable's implicated actors | `Invoice.uploaded_by_id` ∪ `Invoice.segregation_actor_ids`, through `approval_chain.violates_segregation` | anyone the same invoice's **approval** already refuses |
| the raiser | `exceptions.raised_by_user_id` (migration `0098`, tenant DBs) | the actor whose own act the flag exists to have a second person review |

The first axis is the one that binds in volume, and it is the one the
[followups](followups.md) entry did not anticipate. A flag is a *detector's*
finding about an invoice's contents, so the person with a motive to clear it is
whoever created or shaped that invoice — and that person was already barred from
approving it. Leaving them the flag standing between it and a payment run was
the asymmetry.

The second axis exists because the first cannot reach the BEC chain above: the
approver of a vendor bank-detail change is not the uploader of the invoices that
change re-points. `api/vendors._flag_payable_invoices_for_bank_change` is
therefore the **one** raise site that records an actor, and it records the
approver.

Everything else records NULL, deliberately and with a stated reason. Nine of
the eleven `create_exception` call sites are detectors or sweeps — the warnings
recompute, semantic duplicate detection, extraction failure, the payment
reconciler, the settlement-mismatch check, the ERP webhook, a Positive Pay bank
return — and several run from a door that *does* have a user in scope who did
not cause the finding at all (`refresh_warnings` is re-run by whoever next
PATCHes the invoice; a bank-return file is imported by an operator who did not
alter the cheque). Recording those actors would bar a bystander and absolve
whoever really caused the flag.
`backend/tests/test_exception_raiser_stamping.py` walks the syntax tree of every
call site, requires the kwarg, and requires a literal `None` to be declared with
its reason — the same enforcement, for the same reason, as the uploader stamp.

#### What is deliberately *not* refused

- **`escalate`.** An escalated row still blocks a payment run, and escalation is
  the refused actor's designated exit — handing the decision on is the behaviour
  the control wants. Refusing it would trap the row with no way out but a role
  change. Only `resolve` and `dismiss` are in scope
  (`exception_lifecycle.CLEARING_ACTIONS`).
- **Non-blocking types.** Clearing a `po_mismatch`, `missing_data`,
  `quality_hold`, `price_variance`, `contract_noncompliant`,
  `payment_compliance_hold`, `erp_reconciliation`, `review_rejected`,
  `unverified_vendor` or `extraction_failed` releases nothing, so a refusal
  there would be friction with no control behind it. Scope is
  `is_payment_blocking`, which reads the payment-run gate's own tuple — so
  adding a type to that tuple extends this control automatically and there is no
  second classification to keep in step.
- **A NULL actor on both axes.** This reads as "no employee is implicated", and
  it is fail-open for the same reason [the NULL uploader](#the-other-way-sod-can-be-silently-off-a-null-uploader)
  is: failing closed would make every sweep-raised and system-ingested exception
  permanently unclearable — an outage on the queue, not a control. Nothing
  before migration `0098` is backfilled; there is no honest raiser to recover
  and every proxy manufactures a refusal or an absolution.

#### The opt-out, and what an org that has never set it gets

`Organization.settings.exceptions.require_segregation`. **Default ON** — only an
explicit `false` turns it off (`exception_segregation_enabled` reads
`.get("require_segregation", True) is not False`, so a missing key, a missing
`exceptions` block, `None`, `0`, or the *string* `"false"` all keep the
control). An org that has never touched the setting is protected.

It sits beside `auto_assign_by_type` / `sla_hours_by_type` /
`default_sla_hours` — the block the queue already reads — and mirrors
`settings.payments.require_run_segregation`
([`backend/docs/payments.md`](../backend/docs/payments.md)), so the three
identity-level controls on the money path are configured the same way. It is
deliberately **not** the workflow approval step's own `require_segregation`: an
exception need not have an invoice at all (an invoice-less Positive Pay
`fraud_flag`), the queue is not part of any invoice's frozen definition, and
turning off self-approval is a separate decision from turning off
self-clearing.

The escape hatch is why this was recorded as a control-*design* call rather than
patched as a bug: a two-person AP team can have nobody else to work the queue,
and a control that strands a payable is an outage.

#### Autonomous agents inherit the refusal

`run_agent` holds no authority of its own — `actor_id` is the human who
triggered it, it refuses to act at all without that human's real roles, and
`resolver.apply` approves through `review.approve_invoice` on those roles.
Exempting `via="agent"` would hand a barred actor a laundering route to the
outcome the HTTP door refuses them. The coordinator therefore pre-checks and
**escalates with a recorded `AgentDecision`**, the same degradation every other
apply-time refusal produces, rather than surfacing a bare 403. Nothing in the
shipped registry can reach that gate today — `duplicate` and `fraud_flag` are
escalate-only stubs and the other two blocking types have no resolver — which is
exactly why it is tested with one registered
(`backend/tests/test_exception_agent_queue_segregation.py`).

#### What the refusal says, and what is audited

Both messages are about the **caller**, so neither leaks a third party: an
implicated actor already learns they are in the set from the identical refusal
on the approval path, and a raiser is being told about their own act. Neither
names the other actors, the uploader, the vendor, or the amount.

| Axis | Single-row 403 `detail` | `/bulk/resolve` per-row `reason` |
|---|---|---|
| raiser | "Segregation of duties: the user whose action raised this exception cannot also clear it. Escalate it, or ask a different user to decide." | `segregation_raiser` |
| implicated | "Segregation of duties: a user involved in creating this invoice cannot also clear an exception that blocks its payment. Escalate it, or ask a different user to decide." | `segregation_implicated` |

A refusal in bulk is a per-**row** outcome, never a 409 for the batch — the
queue is worked by selecting a filtered page, so one refused row must not take
down an operator's whole sweep.

A **refused** resolution writes no `audit_log` row: it changes no state, the
sibling control on the approval path audits none either, and `audit_log` is
DB-level append-only and WORM-shipped, so a caller-paced refusal row is an
unprunable write. It is logged (ids only, PII-free) at `WARNING`. The evidence
an auditor needs is stronger than a refusal row anyway: every **successful**
`exception.resolved` / `exception.dismissed` row names its `actor_id` and its
`invoice_id`, and the invoice carries the implicated set, so the control's
*outcome* can be re-derived for every historical decision rather than only for
the ones that tripped. See [decisions.md](decisions.md) §165.

### Segregation of duties on a workflow's approval step

`check_segregation` is driven by the approval step's own
`require_segregation` flag, and **the default is ON everywhere**:
`app/schemas/workflow.py` declares `require_segregation: bool = True`, and
`services/approval_chain.py` reads `.get("require_segregation", True)` — so a
definition that carries no key at all still blocks the uploader from approving
their own invoice.

The no-code builder used to undercut that: `DEFAULT_APPROVAL_CONFIG`
(`frontend/src/lib/types/workflow.ts`) hardcoded `require_segregation: false`,
and an explicit `false` is a real value, not an absent key. Every workflow
created through `/workflows` or the builder's Add-step therefore shipped with
self-approval permitted — and with no toggle anywhere in the UI it could
neither be seen nor switched back on. Template-created workflows
(`POST /api/workflows/from-template`) were never affected, which is why it
stayed hidden.

The default is now `true`, and the builder's approval-step editor renders a
visible **Require segregation of duties** switch (with a warning line when it
is off) so disabling the control is a deliberate, auditable choice rather than
a silent default. `frontend/src/lib/types/workflow.test.ts` is the drift guard
on the default; `frontend/tests-e2e/workflows/segregation-default.spec.ts`
covers the persisted value and the toggle round-trip.

#### The rule is a set, not a column

`violates_segregation` refuses an approval by the invoice's `uploaded_by_id`
**or** by anyone in `Invoice.segregation_actor_ids` — every *other* control-plane
user whose act shaped the payable's terms. One column can only name one person,
and a recurring template has two roles that shape what it raises: the employee
who authored the standing instruction, and anyone who later repointed its vendor
or amount. Migration 0097 added the set; see § *A recurring template's editor is
implicated too* below.

Nothing else writes the set. Every other creation path has a single actor, so the
column stays NULL and the reading below is unchanged.

#### The other way SoD can be silently off: a NULL uploader

The flag is one of two ways `violates_segregation` returns False. The other is
`Invoice.uploaded_by_id IS NULL` **with an empty `segregation_actor_ids`**, which
the check reads as **"no employee created this row"** and therefore nobody who
could self-approve.

That is fail-open, and it holds only because of an invariant enforced outside
the function: every path under `app/` that creates an invoice on behalf of a
signed-in employee stamps the column —

| Path | Uploader recorded |
|---|---|
| `POST /api/invoices` (manual entry) | the caller |
| `POST /api/workflow/upload` (file upload) | the caller |
| `POST /api/invoices/import-csv` (CSV import) | the caller |
| `POST /api/recurring/{id}/generate-now` | the caller |
| `POST /api/invoices/{id}/route-intercompany` (the mirror payable) | the routing actor |
| the recurring-invoice background sweep | `RecurringInvoiceTemplate.created_by_user_id` — the employee who authored the template (NULL only for a template predating migration 0096) |
| email intake, inbound PEPPOL | NULL — system ingestion, no human |
| supplier-portal submit, portal PO flip | NULL — the actor is a tenant-scoped `VendorUser`, who holds no employee JWT and can never reach an approval endpoint |

The CSV importer was the hole: the route had the user and the audit row used
it, but the `Invoice(...)` constructor never passed it, so the importer could
approve what they had just imported while the same person doing the same thing
through `POST /api/invoices` got a 403.

Making the NULL case fail CLOSED was considered and rejected — it would render
every email-intake, PEPPOL and portal-submitted invoice permanently
unapprovable, an outage across three ingestion channels rather than a control.
`backend/tests/test_invoice_uploader_stamping.py` enforces the invariant
instead: a new construction site must pass `uploaded_by_id` explicitly, and
passing a literal `None` must be declared with the reason there is no employee
actor. See `docs/decisions.md`.

#### The recurring sweep was on that list and did not belong

It was the one row where NULL was *wrong* rather than merely permissive. Nobody
**runs** the sweep, but an employee **authored** the template, and that is the
person segregation has to exclude — so the template's author could approve the
invoice their own standing instruction raised, exempt exactly as a legacy
pre-`uploaded_by_id` row is. The other three channels have no control-plane user
in the picture at all; this one did, and we simply had nowhere to put them.

Migration 0096 added `recurring_invoice_templates.created_by_user_id`, stamped
by `POST /api/recurring`, and `recurring_invoices.generate_one` now stamps
`actor_id or template.created_by_user_id` — the live actor when there is one
(generate-now), the author when there is not (the sweep). The **audit** row
keeps `actor_id` honest (`None` for the sweep): *who acted* is nobody, *whose
instruction created this payable* is the author, and only the second is a
segregation input.

Templates created before that migration are deliberately **not** backfilled.
There is no honest author to recover, and guessing one manufactures either a
refusal or an absolution. Those keep the legacy NULL reading — a shrinking set,
not a standing hole.

#### A recurring template's editor is implicated too

Recording the author closed half of it. An `ap_manager` who PATCHes *someone
else's* template — repointing the vendor and the amount — shapes every payable it
goes on to raise as completely as whoever wrote it, was recorded nowhere on the
invoice, and could still approve it. Stamping the editor into
`created_by_user_id` instead would only have moved the exemption to the author:
**no single column holds both people**, so the predicate and every path feeding
it had to change together.

Migration 0097 adds two nullable JSONB sets of control-plane user ids, both
tenant-scoped:

| Column | Written by | Holds |
|---|---|---|
| `recurring_invoice_templates.material_editor_ids` | `PATCH /api/recurring/{id}` | everyone who changed a **material** field — a term of the generated payable |
| `invoices.segregation_actor_ids` | `recurring_invoices.generate_one` | the template's author ∪ its material editors, minus whoever already landed in `uploaded_by_id` |

"Material" is defined once, in
`backend/app/models/recurring_invoice.MATERIAL_EDIT_FIELDS`: vendor, amount,
currency, the four GL-coding dimensions, PO number, payment terms, the cadence /
day / start / end schedule, and the owning entity. `COSMETIC_EDIT_FIELDS` holds
the rest — `name`, `description`, `notes`, and `variance_tolerance_pct` (a
detection band for *arrived* vendor invoices, which never touches the invoice
this template generates). The two sets must between them cover every field on
`RecurringTemplateUpdate`, and
`backend/tests/test_recurring_invoices.py::test_every_patchable_template_field_is_classified`
fails until a newly added field is classified — an unclassified field would
default to "cosmetic" and silently widen the exemption.

The append is idempotent (a set, not a log), never overwrites the author, and is
keyed off what *actually changed*, so re-saving a form with its existing values
implicates nobody. The pause / resume / end endpoints implicate nobody either:
they change whether the instruction is live, not what it instructs.

Edits made **before** that migration implicate nobody, for the same reason
authors are not backfilled — `updated_at` records *that* someone edited, never
who, and inferring an editor manufactures either a refusal or an absolution.

`check_segregation` is reused for expense reports, requisitions and expense
pre-approvals through a `SimpleNamespace` attribute shim, so one rule and one
403 string serve every "decider ≠ requester" surface. Those three pass
`segregation_actor_ids=None` explicitly — none of those tables records who
*edited* a row, so there is no second actor to name — rather than relying on the
predicate's `getattr` default, which exists only so a future subject that forgets
cannot raise `AttributeError` on the approval path.

The inter-company mirror (`POST /api/invoices/{id}/route-intercompany`)
deliberately does **not** inherit the source invoice's set. Its segregation
subject has always been its own creator, the routing actor; propagating the set
while still not propagating `uploaded_by_id` would block a source *editor* there
while leaving the source *uploader* free. Whether shaping a payable under one
entity should bar you from signing its mirror under another is an entity-scope
question — tracked in [followups.md](followups.md).

### Approve and reject are not always the same role set

Where a decision splits into an "approve" and a "reject" half, the two may
carry **different** gates, and a frontend predicate that serves both will be
wrong for one of them. The live example is expense reports
(`app/api/expenses.py`):

| Action | Roles | Extra gate |
|--------|-------|-----------|
| `POST /api/expense-reports/{id}/approve` | `admin` \| `ap_manager` \| `cfo` | above `settings.expense_approval.cfo_threshold` (default 5000) only `cfo` / `admin` may approve |
| `POST /api/expense-reports/{id}/reject` | `admin` \| `ap_manager` | — |

The `/expenses` Reports tab gated BOTH buttons on one manager-only predicate,
so an over-threshold report 403'd for the `ap_manager` **and** showed the CFO no
Approve button at all — the exact person the threshold escalates to could not
act from the UI. The page now derives `canApproveReport` (the wider set) and
`canDecideReport` (reject) separately; both keep the same SoD self-check, since
the decider must never be the submitting employee. When adding a decision pair,
check the two backends' `require_roles` independently rather than assuming they
match. Guarded by `frontend/tests-e2e/expenses/cfo-report-approval.spec.ts`.

The same asymmetry bit dynamic discounting in the other direction: accepting an
early-payment offer allowed the CFO while declining it did not, so a CFO could
commit cash early but not refuse. Accept and decline are two halves of one
decision the CFO owns, so `POST /api/discounts/offers/{id}/decline` now carries
the same gate as `/accept` (`admin` \| `ap_manager` \| `cfo`). Declining moves
no money — it only flips status.

## Testing Auth via curl

```bash
# Login and capture token. NOTE: when FEOH_MFA_ENABLED=true and the user has MFA
# enrolled (or the org enforces it), the response is an MFAChallengeResponse
# (no `access_token` field) and you'll need to complete /api/auth/mfa/verify
# next. The snippet below assumes MFA is off (the local-dev default).
TOKEN=$(curl -s -X POST http://localhost:8000/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@acme.com","password":"demo"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])")

# Use the token (include tenant slug header)
curl http://localhost:8000/api/invoices \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Tenant-Slug: acme"

# Get current user
curl http://localhost:8000/api/auth/me \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Tenant-Slug: acme"

# Logout (revoke the token)
curl -s -X POST http://localhost:8000/api/auth/logout \
  -H "Authorization: Bearer $TOKEN"

# Verify the token is revoked (should return 401)
curl http://localhost:8000/api/auth/me \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Tenant-Slug: acme"
```

---

## SSO (OIDC via Okta + Microsoft Entra)

A single OIDC flow supports both Okta and Entra because they're both OIDC-compliant — the only per-tenant variation is the discovery URL. Config lives on `Organization.settings.sso`:

```json
{
  "sso": {
    "enabled": true,
    "provider": "okta",
    "discovery_url": "https://acme.okta.com/.well-known/openid-configuration",
    "client_id": "...",
    "client_secret": "...",
    "scim_bearer_hash": "<sha256 hex>",
    "allowed_email_domains": ["acme.com"]
  }
}
```

### Flow

1. User clicks **Sign in with Okta/Microsoft** on `/login` (button renders only when `GET /api/auth/sso/config?slug=<tenant>` returns `enabled: true`). `slug` is optional — on a vanity host it is omitted and the tenant comes from the request `Host` (see *SSO on a white-label vanity host* below).
2. Browser navigates to `GET /api/auth/sso/authorize?slug=<tenant>` on the backend (same optional-`slug` rule).
3. Backend fetches the discovery doc (cached in Redis for 24h), mints a one-shot `state` + `nonce` into Redis (keyed to the tenant slug), and **302-redirects** to the IdP's `authorization_endpoint` with `redirect_uri` pointing to the *tenant's own subdomain* (built from `FEOH_TENANT_URL_TEMPLATE`).
4. User authenticates on the IdP.
5. IdP redirects back to `<tenant>.app.com/login/sso-callback?code=...&state=...`.
6. Frontend callback page POSTs `{code, state}` to `/api/auth/sso/callback`.
7. Backend:
   - Consumes the state (single-use) and extracts the bound tenant + nonce.
   - Exchanges the code for tokens via the IdP's `token_endpoint`.
   - Validates the ID token against the IdP's JWKS (signature + iss, aud, exp, nonce). Signature verification is pinned to asymmetric algorithms (`RS*`/`ES*`/`PS*`/`EdDSA`) via `services/sso.ID_TOKEN_ALGORITHMS`, so a forged token can't downgrade to HMAC (alg-confusion) or `alg:none`.
   - Optionally enforces `allowed_email_domains`.
   - JIT-provisions the user (match order: `(sso_provider, sso_provider_id)` → `(organization_id, email)` → new user with least-privilege `ap_clerk` role, or `admin` if it's the first user in the org).
   - Mints our own JWT and returns `{access_token, must_change_password, tenant_slug}`.
8. Frontend stores the JWT, fetches `/api/auth/me`, and routes to `/change-password` if flagged or `/` otherwise.

### Why callback URLs are per-tenant

Each customer registers their own Okta/Entra app with `redirect_uri = https://<theirtenant>.app.com/login/sso-callback`. That way the IdP redirects directly to the tenant origin and our localStorage JWT works without cross-origin hops. In dev, `FEOH_TENANT_URL_TEMPLATE=http://{slug}.localhost:7777` gives each tenant their own callback URL for free.

### SSO on a white-label vanity host

A tenant served on its own hostname (`sso.acmecorp.com`, registered in
`settings.brand.custom_domains` — see [white-label.md](white-label.md) § Custom
domains) has **no slug anywhere in its URL**. Two separate things follow, and
they are deliberately decoupled.

**1. The entry points accept a `Host`-resolved tenant.** `slug` is *optional* on
`GET /api/auth/sso/config`, `GET /api/auth/sso/authorize`,
`GET /api/auth/saml/config` and `GET /api/auth/saml/login`. When it is absent
the tenant is resolved from the request `Host` against the tenant's registered
custom domains, through the **same** `app/tenant.py` resolver
(`normalize_custom_domain` / `resolve_tenant_slug_by_custom_domain`) every other
host lookup uses — wrapped once as `services/sso.resolve_sso_tenant_slug`, so a
hostname can never resolve one way for a page load and another way for a login.
The SPA omits `?slug=` on a vanity host and calls the API same-origin
(`getApiBase()`), which is what puts the vanity hostname in the `Host` header at
all. This is why `routes/login/+page.svelte` no longer appears on the
`BUILD_TIME_API_URL_BASELINE` ratchet in `frontend/src/lib/tenantSlugUsage.test.ts`.

*The failure posture is unchanged.* A `Host` that resolves to nothing produces
the **identical** `404 {"detail": "Unknown tenant."}` an unknown `?slug=` has
always produced — same status, same body — so probing hostnames tells an
attacker exactly what probing slugs already told them, and nothing new
distinguishes "no such tenant" from "SSO is not configured" (that pre-existing
404-vs-400 split on the slug path is untouched). These endpoints are
pre-authentication by definition, so there is no JWT to cross-check; that is
safe because a forged `Host` can only select a tenant that *registered that
exact hostname* — the same choice `?slug=` already gives anyone — and the
handshake's state/nonce (OIDC) or RelayState (SAML) is minted against that
tenant and consumed against it.

**2. The callback URL is a separate, explicit opt-in.**
`settings.brand.sso_callback_base_url` (managed on `PUT /api/organization/branding`,
admin-only, validated as an http(s) URL, audited PII-free) sets the origin the
OIDC `redirect_uri` and the SAML bridge URL are built from. Empty — the default
— means the global `FEOH_TENANT_URL_TEMPLATE`, byte-for-byte the behaviour that
shipped before the field existed. `services/sso.sso_callback_base` is the one
resolver both protocols read; `{slug}` in the value is optional (substituted
when present, used verbatim when not).

It is **not** the per-org `settings.brand.tenant_url_template` that outbound
links use, and that separation is the whole point: both values are *registered
at the customer's IdP*, so inferring them from a vanity host — or folding them
into the setting an admin flips to fix invite and password-reset links — would
break every SSO login for that tenant until the operator re-registered the app.
Setting it is therefore the **last** step of an operator-sequenced migration,
not a config read. See [decisions.md](decisions.md) §91 and the step-by-step
procedure in
[founder-runbooks/custom-domain-provisioning.md](founder-runbooks/custom-domain-provisioning.md)
§ Re-registering SSO at the customer's IdP.

A persisted value that is malformed (edited straight into the database, so never
validated by the endpoint) is re-checked on read and degrades to the global
template — a broken row must not take a tenant's SSO offline. And because
`GET /portal/branding` is public-by-design and returns a whole `BrandConfig`,
the field is **blanked there** alongside `tenant_url_template`: it is not a
theming value, nothing on the supplier-portal login page consumes it, and an
admin may have staged it before the IdP re-registration.

Regression coverage: `backend/tests/test_sso_custom_domain.py`.

### Local testing with Keycloak (no cloud account)

You don't need an Okta/Entra tenant to exercise this flow locally. A Keycloak
container under the compose `idp` profile is the dev-laptop equivalent of the
IdP:

```bash
pnpm idp:up        # Keycloak on :8088 (Docker, opt-in)
pnpm idp:seed      # write the acme tenant's settings.sso → local Keycloak
pnpm dev           # then sign in via SSO at http://acme.localhost:7777
```

`demo@acme.com` / `demo` links to the seeded admin; `newhire@acme.com` / `demo`
JIT-provisions a fresh `ap_clerk`. Because the same generic OIDC code path runs,
a flow that works against Keycloak works against Okta/Entra. Full walkthrough:
[`local-sso-keycloak.md`](local-sso-keycloak.md).

### JIT user provisioning

First SSO login provisions the user. Three match paths:

| Match on | When it fires |
|---|---|
| `(sso_provider, sso_provider_id)` | Durable — survives email changes |
| `(organization_id, email)` | First SSO login for an existing password user — links their row and sets `sso_provider_id` |
| Create new | No match → new row with `hashed_password=NULL` (SSO-only), `must_change_password=false` |

New users get `ap_clerk` role by default (least privilege). The first user ever in an org gets `admin`.

A match that lands on a **deactivated** account (`users.is_active = false`) raises
`DeactivatedAccount` instead of returning the row, so neither SSO callback can
mint a session for someone who has been offboarded — see
[A deactivated account cannot sign in, on any path](#a-deactivated-account-cannot-sign-in-on-any-path).
The raise happens *before* the email-link branch rebinds `sso_provider_id`, so a
login attempt against a disabled account can't silently repoint its SSO identity
either.

The asserted address is normalised (lower-cased, stripped) and then checked for
**control characters** before anything is stored — `utils/emails.is_header_safe`,
raising `UnsafeEmailAddress`, which both callbacks turn into their existing
generic refusal (SAML `unsafe_email`, OIDC a 403 + a PII-free audit reason).
`strip()` already removed a trailing newline, but an interior one survived it,
and `User.email` is both a login and the destination of every notification the
app sends that person — a newline reaching an SMTP header is the
header-injection primitive. The check is deliberately NARROWER than the shape
rule the signup / partner / scheduled-report surfaces apply: a corporate IdP can
legitimately assert an internal-only `user@intranet`, and refusing that would
lock a tenant out of its workspace over a cosmetic rule. See
[decisions §60](decisions.md).

### A deactivated account cannot sign in, on any path

`users.is_active = false` is how offboarding is expressed — an admin flips it via
`PATCH /api/admin/users/{id}`, or the IdP deprovisions through SCIM
(`active: false`, `DELETE /scim/v2/Users/{id}`). Every sign-in entry point
refuses such an account:

| Path | Behaviour |
|---|---|
| `POST /api/auth/login` | Opaque `401 Invalid credentials`, checked alongside "no such account" so a deactivated account is indistinguishable from one that never existed. Burns the per-account failure budget; audits `auth.login.failure` with `reason: "inactive"`. |
| `POST /api/auth/mfa/verify`, `POST /api/auth/mfa/passkey/authenticate/verify` | `401 Invalid challenge`. |
| `POST /api/auth/sso/callback` | `403`; audits `auth.sso.login.failure` with `reason: "inactive"`. |
| `POST /api/auth/saml/acs` | The router's single generic error; audits `auth.saml.login.failure` with `reason: "inactive"`. |
| `POST /api/portal/auth/login` | Opaque `401`; audits `portal.login.failure` with `reason: "inactive"`. |

`get_current_user` refuses an inactive user regardless, so a token minted for one
was always inert. The reason these entry points check anyway: without it the
caller was told the sign-in SUCCEEDED — the SPA stored a token and then 401'd on
every call, the session counted against `FEOH_MAX_CONCURRENT_SESSIONS`, and the
SOX trail recorded an `auth.login.success` for a terminated employee.

### OIDC endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/auth/sso/config?slug=<s>` | Unauthenticated. Returns `{enabled, provider}`. Never leaks secrets. |
| `GET` | `/api/auth/sso/authorize?slug=<s>` | 302 to IdP. |
| `POST` | `/api/auth/sso/callback` | `{code, state}` → `{access_token, must_change_password, tenant_slug}`. |

### Not in this pass

- **SAML 2.0** — tracked in Priority 7 roadmap. OIDC covers ~90% of "SSO" asks; SAML remains a separate code path for regulated industries that require it.

---

## Multi-factor authentication (MFA)

TOTP-based two-factor with optional email backup. Per-user opt-in by default; admins can flip a tenant-wide enforcement switch under **Organization → Security**.

### Master switch

`FEOH_MFA_ENABLED` (default `false`) is the platform-level gate. When false, all MFA endpoints, login challenges, and enforcement are skipped — useful for local dev where you don't want to scan a QR code every time. Flip to `true` in any deployed environment.

### Factors

| Factor | Identifier | Notes |
|---|---|---|
| **TOTP** | `totp` | RFC 6238, 30-second window, ±1 step skew tolerance. `pyotp` under the hood. Compatible with Google Authenticator, 1Password, Authy, Microsoft Authenticator. Single-use: a Redis claim (`SET NX`, ~90s TTL bounding the ±1-window validity) rejects a replay of the same code, closing the window the email-OTP path already had. |
| **Passkey / WebAuthn** | `passkey` | FIDO2 / WebAuthn — a platform authenticator (Touch ID, Face ID, Windows Hello) or a roaming security key. `py_webauthn` under the hood. A **separate code path** from TOTP (`services/webauthn.py`), opt-in and additive: a user can register one or many passkeys alongside or instead of TOTP. |
| **Email OTP** | `email` | 6-digit code emailed via the configured `FEOH_EMAIL_PROVIDER`. Lives in Redis with a `FEOH_MFA_EMAIL_OTP_TTL_SECONDS` TTL (default 6 minutes). Only a server-secret-keyed PBKDF2 digest of the code is stored — Redis dumps don't reveal codes, and keying it (via a secret salt) stops the low-entropy code being brute-forced from the digest. Single-use. |

Email is offered as a backup so a lost phone doesn't lock the account out. It's also the only available factor for users under org-enforcement who haven't enrolled TOTP yet (verifying email proves inbox ownership before they're allowed to enroll).

### Passkeys (WebAuthn)

Passkeys are an additional second factor, gated by the same `FEOH_MFA_ENABLED` master switch. They use the standard two-ceremony WebAuthn protocol:

**Registration (enroll a passkey — authenticated, on `/profile`):**

```
POST /api/auth/mfa/passkey/register {password?|code?|assertion?}   # mints PublicKeyCredentialCreationOptions
  (browser runs navigator.credentials.create())
POST /api/auth/mfa/passkey/register/verify   # verifies + persists a WebAuthnCredential row
GET  /api/auth/mfa/passkey                    # list this account's passkeys (metadata only)
DELETE /api/auth/mfa/passkey/{id} {password?|code?|assertion?}   # remove one — step-up ALWAYS required
```

**Step-up (re-prove account control before a factor change — authenticated):**

```
POST /api/auth/mfa/step-up/passkey {operation}   # → PublicKeyCredentialRequestOptions
  (browser runs navigator.credentials.get())
  → send the signed response as `assertion` in the step-up body of the
    matching call (/mfa/enroll, /mfa/passkey/register, DELETE /mfa/passkey/{id},
    /mfa/disable)
```

**Authentication (passkey LOGIN — the passkey method of the MFA step):**

```
POST /api/auth/mfa/passkey/authenticate         {challenge_token}  # → PublicKeyCredentialRequestOptions
  (browser runs navigator.credentials.get())
POST /api/auth/mfa/passkey/authenticate/verify  {challenge_token, credential}  # → {access_token, ...}
```

Both authenticate endpoints are **pre-access-token, public-by-design**: the login-issued MFA challenge token (`typ: mfa_challenge`, the same credential the `/mfa/verify` path uses) IS the gate; there is no JWT yet. The register / list / delete endpoints require the normal JWT.

The per-ceremony challenge is server-minted and stashed in Redis (`webauthn:reg_challenge:<user_id>` for registration, `webauthn:auth_challenge:<user_id>` for login, `webauthn:stepup_challenge:<operation>:<user_id>` for a step-up — `FEOH_WEBAUTHN_CHALLENGE_TTL_SECONDS` TTL, single-use) so the verify call can't be fed an attacker-chosen challenge. **Those namespaces are a security boundary, not bookkeeping** — see "Purpose binding" below. On every successful assertion the credential's monotonic signature counter is verified and bumped — a regression (a cloned authenticator) is rejected per the WebAuthn spec. RP ID and allowed origins come from `FEOH_WEBAUTHN_RP_ID` / `FEOH_WEBAUTHN_ORIGINS`, but they are **resolved per request** rather than read at each ceremony site — see "Passkeys on a custom domain" below. The dev defaults (`localhost` / `http://localhost:7777`) work across every tenant subdomain with no cloud account. In deployed envs `FEOH_WEBAUTHN_ORIGINS` also accepts wildcard-subdomain entries (`https://*.app.example.com`) so every tenant origin is admitted without a per-tenant env change — the match is same-scheme suffix-at-a-label-boundary only (suffix look-alikes like `https://evilapp.example.com`, scheme downgrades, ports, and userinfo tricks don't match, and the bare base needs its own exact entry).

Stored material — the credential id, COSE public key, counter, and the `rp_id` the credential is bound to — is not secret in the password sense (the private key never leaves the authenticator) and is never logged. The login challenge offers `passkey` as a method whenever the user has at least one registered credential **that this host's Relying Party can actually challenge**.

Registering a passkey on an account that **already** has a factor (TOTP enabled, or at least one passkey) is a step-up operation with the same optional `{password?, code?, assertion?}` body as `/mfa/enroll` — otherwise a stolen session could quietly bind an attacker-controlled authenticator to the account. The *first* factor on a bare account needs no step-up.

**Deleting** a passkey is a step-up operation too, and unconditionally: the passkey being deleted is itself a live factor, so `DELETE /api/auth/mfa/passkey/{id}` always requires the password, a current authenticator code, or a passkey assertion. Removing a factor with a stolen token is the same attack as replacing one. The credentials travel in the request **body**, never a query string — a password must not land in access logs or a `Referer` header. An id that isn't the caller's own is still an opaque `404`, checked *before* the step-up so an unknown id can't be used to burn the account's throttle or probe for existence. On top of that, under org-enforced MFA the last surviving factor can't be removed at all.

The `/profile` passkey panel renders one "Confirm your password" field that serves both add and remove, shown only when a step-up actually applies. Leaving it blank on an account that holds a passkey runs the passkey step-up ceremony instead — the only route open to an SSO-only account.

#### Passkeys on a custom domain (vanity host)

A WebAuthn credential is bound to exactly **one** registrable domain — its Relying Party ID. `FEOH_WEBAUTHN_RP_ID` is a single global, so a tenant served on a white-label vanity host (`ap.acmecorp.com`, registered under `settings.brand.custom_domains` — see `docs/white-label.md`) could neither register nor use a passkey against it: the browser refuses to sign an RP ID that is not the page's effective domain or a registrable parent of it. TOTP and email OTP were unaffected, so it presented as a silently *reduced* second-factor menu rather than an error.

The RP ID and allowed origins are now resolved **per request**, from the host the ceremony is running on, by `backend/app/services/webauthn_rp.py`. That module is the single owner of the `(rp_id, origins)` pair; every ceremony site goes through it, and `begin_registration` / `finish_registration` / `begin_authentication` / `finish_authentication` all take the resolved `RelyingParty` as a **required** argument — there is no config read left inside the ceremony module for a call site to disagree with.

**The `Host` header is client-supplied, so it can never become an RP ID on its own.** Resolution is:

| Request host | Resolved RP ID | Allowed origins |
|---|---|---|
| absent, or under the configured `FEOH_WEBAUTHN_RP_ID` (`acme.localhost`) | the global `FEOH_WEBAUTHN_RP_ID` | `FEOH_WEBAUTHN_ORIGINS` |
| a host on **this tenant's own** `settings.brand.custom_domains` | that host | `https://<host>` + `FEOH_WEBAUTHN_ORIGINS` |
| anything else — unknown, forged, or **another tenant's** vanity domain | the global `FEOH_WEBAUTHN_RP_ID` (fail closed) | `FEOH_WEBAUTHN_ORIGINS` |

Three things make that safe:

- The custom-domain list consulted is the one belonging to the org that owns the **account** the ceremony is for, not one looked up from the header. A vanity domain registered by a different tenant is therefore just an unknown host here.
- Host normalization reuses `app/tenant.py::normalize_custom_domain` — the same function the tenant resolver maps an inbound `Host` to a slug with, so there is one notion of "is this that domain", not two.
- A host that already sits *under* the platform RP ID keeps the platform RP ID even when it is also registered as a custom domain. Existing passkeys keep working; nothing needs re-registering.

`https://<host>` is auto-allowed for a registered custom domain so a new vanity host needs no env change. The RP ID is the real binding (the authenticator signs `sha256(rp_id)` into `authenticatorData`, and a browser only produces it for a page under that domain); the origin list is a pre-screen on top of it. The configured platform origins stay in the list so an operator can add an explicit local-dev origin for a vanity host without a code change.

**Registration and authentication cannot drift.** Beyond the single resolver, the RP ID a ceremony was *minted* under is stored alongside its Redis challenge and re-checked on the verify call — so a ceremony begun on the platform host and finished on a vanity one (or the reverse) is refused, instead of persisting a credential bound to a domain the authenticator never signed.

##### The migration story: a passkey does not follow you across hosts

A passkey registered against the platform RP ID genuinely cannot be presented on a vanity host. That is the protocol, not a defect to code around — so the goal is to make it **legible** rather than silent. `webauthn_credentials.rp_id` (migration `0091`, control-plane, nullable, backfilled to the configured global RP ID, which is provably what every pre-existing row was registered under) records where each credential lives, and:

- `GET /api/auth/mfa/passkey` lists **every** passkey with its `rp_id` and a derived `usable_here` flag, so the security page can show "registered for `ap.acmecorp.com`" instead of offering a credential whose ceremony is guaranteed to fail.
- `POST /api/auth/mfa/passkey/authenticate` and `POST /api/auth/mfa/step-up/passkey` narrow `allowCredentials` to the credentials this host can challenge, and when that leaves none they say *which* host the account's passkeys belong to. The caller has already proved control of the account (an access token, or the post-password MFA challenge token) and the hosts named are the tenant's own, so this leaks nothing. An account with **no** passkey at all keeps the old opaque `No passkey registered`, so the message can't be used to probe factor enrollment.
- The login `methods` list omits `passkey` on a host where none is usable — but the **MFA gate itself still counts every passkey**, so a vanity-host passkey remains a second factor on the platform host. `email` is always offered, so narrowing the menu can never strand an account.
- **Deleting** a passkey is deliberately *not* host-scoped: a user must be able to remove a credential from wherever they happen to be signed in.

The residual rough edge, by design: an SSO-only account whose only factor is a passkey registered on another host cannot run the *assertion* step-up on this one (there is nothing here to sign with). It falls back to the password / TOTP proofs, or to registering a passkey on this host first. Regression tests: `backend/tests/test_webauthn_custom_domain.py`.

#### Purpose binding — why a step-up assertion is not a login

`clientDataJSON.type` is `"webauthn.get"` for a login assertion and a step-up assertion alike; the authenticator has no idea what the relying party intends to do with the signature. The **challenge is therefore the only thing that can tell the two apart**, which makes the Redis namespace the binding mechanism:

| Ceremony | Redis slot |
|---|---|
| Passkey login | `webauthn:auth_challenge:<user_id>` |
| Step-up | `webauthn:stepup_challenge:<operation>:<user_id>` |

Each verify path reads only its own slot and consumes it single-use, so:

- a **step-up assertion cannot be replayed as a login** — `/mfa/passkey/authenticate/verify` looks in the login slot and finds a different (or no) challenge, and returns the same opaque `401` as any bad signature;
- a **login assertion cannot satisfy a step-up** — an attacker who observed a legitimate sign-in still can't change the victim's factors with it;
- an assertion collected to authorize `passkey_register` **cannot authorize `passkey_delete`** — one consented biometric prompt grants exactly the action it was requested for, not the whole factor-management surface.

`operation` is a closed set (`totp_enroll`, `totp_disable`, `passkey_register`, `passkey_delete`), rejected at the schema, so the challenge namespace can't be widened by the client. `services/webauthn._assertion_challenge_key` owns the mapping and `purpose` is a required argument on `begin_authentication` / `finish_authentication` — a caller cannot forget to say which ceremony it means. Regression tests: `backend/tests/test_webauthn_step_up.py`.

### Login flow

```
POST /api/auth/login {email, password}
  └─ MFA off OR user not enrolled AND org doesn't require it
        → 200 {access_token, token_type, must_change_password}
  └─ MFA required
        → 200 {mfa_required: true, mfa_challenge_token, methods, must_enroll}

  (browser stashes mfa_challenge_token in sessionStorage, navigates to /login/mfa)

POST /api/auth/mfa/challenge/email {challenge_token}    # only if user picks email
  └─ 204 No Content (issues + emails a 6-digit code)

POST /api/auth/mfa/verify {challenge_token, code, method}    # totp / email factors
  └─ 200 {access_token, token_type, must_change_password}

# OR — the passkey factor (when methods includes "passkey"):
POST /api/auth/mfa/passkey/authenticate {challenge_token}    # → request options
POST /api/auth/mfa/passkey/authenticate/verify {challenge_token, credential}
  └─ 200 {access_token, token_type, must_change_password}
```

`methods` in the challenge response lists which factors the user can submit (`totp` / `passkey` / `email`), so a user who has registered only a passkey (no TOTP) still trips the MFA gate and is offered `passkey`.

The challenge token is itself a short-lived JWT (`FEOH_MFA_CHALLENGE_TTL_SECONDS`, default 5 minutes) with `typ: mfa_challenge`. That keeps the flow stateless — no DB row to garbage-collect. A regular access token won't satisfy the challenge endpoint and vice versa.

**Single-use.** The challenge token's `jti` is blocklisted (the same Redis blocklist `/logout` uses) the moment a factor verify actually mints a real access token — `/mfa/verify` (totp/email) and `/mfa/passkey/authenticate/verify` both consume it on success. A decode also checks the blocklist, so replaying an already-used token — even with a fresh, still-valid code — gets a clean 401 instead of a second session. Requesting an email OTP or starting a passkey ceremony does *not* consume the token (the user hasn't completed a factor yet), so those can be called more than once before the exchange finishes. The vendor portal's `vendor_mfa_challenge` token gets the identical treatment.

**Token-type isolation is enforced at the dependency, not just the route.** `get_current_user` (in `app/api/deps.py`) rejects every JWT whose `typ` is a non-access type — `vendor`, `mfa_challenge`, or `vendor_mfa_challenge` (`_NON_ACCESS_TOKEN_TYPES`) — so a password-verified-but-MFA-pending caller cannot wield their `mfa_challenge` token as a fully-authenticated Bearer token and skip the second factor. A real access token is `typ="user"` (a missing `typ` is still accepted for legacy tokens predating the claim). Pinned by `backend/tests/test_auth_token_security.py::test_mfa_challenge_token_cannot_act_as_access_token` (and the `vendor_mfa_challenge` sibling), which wire the user lookup to a valid active user so only the type-rejection can produce the 401.

### Per-user enrollment

```
POST /api/auth/mfa/enroll     {password?|code?|assertion?}  # mints a CANDIDATE secret + URI + QR
POST /api/auth/mfa/enroll/verify {code}                     # promotes it, flips mfa_enabled true
POST /api/auth/mfa/disable    {password?|code?|assertion?}  # turns it off (step-up first)
POST /api/auth/mfa/step-up/passkey {operation}              # mint a step-up assertion challenge
```

The QR code is returned inline as a `data:image/png;base64,...` URL so the frontend doesn't need a separate authed image endpoint. The plaintext base32 secret is also returned so users without a camera-equipped scanner can paste it manually.

**Enrollment never disturbs the factor already in force.** `/mfa/enroll` mints a *candidate* secret and parks it in Redis (`mfa:pending_enroll:<user_id>`, `FEOH_MFA_ENROLL_PENDING_TTL_SECONDS`, default 15 min). `User.mfa_secret` / `mfa_enabled` / `mfa_enrolled_at` are written by `/mfa/enroll/verify` and nowhere else, so an abandoned or half-finished enrollment leaves the account exactly as it was. Previously enroll-start wrote the new secret straight onto the row and cleared `mfa_enabled`, which made *starting* an enrollment a silent second-factor strip.

**Changing an existing factor is a step-up operation.** When the account already has a live factor, `/mfa/enroll` (and `/mfa/passkey/register` — see below) requires one of:

- `password` — the account password, verified through the shared `verify_password` (the `pwd_context` wrapper), exactly like `/mfa/disable`; or
- `code` — a code from the **currently enrolled** authenticator (for the user who has their phone but not their password manager); or
- `assertion` — a **WebAuthn assertion from an already-registered passkey**, obtained from `POST /api/auth/mfa/step-up/passkey` for that same operation.

A "live factor" here means an enabled TOTP secret **or** at least one registered passkey — adding TOTP to a passkey-protected account is as much a factor change as the reverse, so both doors are gated the same way.

Neither field is required for a **first** enrollment: an account with no factor has nothing to protect, so onboarding stays frictionless. A missing or wrong step-up is a `400` with a generic message that reveals nothing about the account.

An SSO-only account — no password, no TOTP secret — has no *stateless* credential to be challenged on, and is still never **exempted**: exempting it would let a stolen JWT plant an attacker-controlled passkey on an account the attacker never proved control of. Instead, if it holds a registered passkey, that passkey **is** the challenge: `POST /api/auth/mfa/step-up/passkey` mints an assertion challenge bound to the operation, and the signed response goes back as `assertion`. That is what makes a passwordless SSO deployment able to enroll, rotate and remove its own factors at all; before it, such an account was locked out of factor management and recovered only via an admin password-set — `PATCH /api/admin/users/{user_id}` with a `password` field (`app/api/admin.py`, `app/schemas/admin.py::AdminUserUpdate`); there is no `POST .../password` route (still the fallback for an account with *no* factor of any kind, which genuinely has nothing to prove). The password / TOTP checks stay pure in `services/mfa.step_up_verified`; the assertion path is `api/auth._step_up_satisfied` because it needs the DB and Redis.

Every step-up check is **throttled and audited**: 5 attempts per minute keyed on the *account* (not the client IP — the attacker already holds the victim's token and can rotate IPs), and a failure writes a PII-free `auth.mfa.step_up.failure` / `portal.mfa.step_up.failure` audit row carrying only the operation name. Without that, a credential-management endpoint that checks a password is an unlimited, silent password oracle. The same throttle + audit covers `/mfa/disable` on both surfaces.

**A factor change that succeeds is audited too — on both surfaces.** For a long time only the *failed* step-ups were on the trail, which is the wrong way round: a wrong password is a signal, but a second factor actually being added, replaced or removed on an account that can approve invoices or stage a bank-detail change is the event an incident responder has to be able to find at all. Every path that moves a factor now writes one PII-free row after the change commits — `auth.mfa.enrolled` / `auth.mfa.disabled` / `auth.mfa.passkey.registered` / `auth.mfa.passkey.removed` on the employee surface, `portal.mfa.enrolled` / `portal.mfa.disabled` on the supplier portal (see the [action table](#auth-event-audit-logging) for the exact `details`). Three rules hold across all of them:

- **The row never carries the credential it is about.** No TOTP secret, no submitted password or code, no passkey public key or authenticator credential handle, no recovery code. A passkey row names the API-visible `webauthn_credentials.id` instead, which is the handle `GET /mfa/passkey` lists and `DELETE /mfa/passkey/{id}` takes — so registration and removal are joinable without publishing a login identifier onto an append-only, WORM-shipped trail.
- **The row is written after the commit**, so the trail records factors that took effect. A rejected enrollment code writes no `*.enrolled` row.
- **`replaced` is read before the write.** It distinguishes a first enrollment from one that displaced a live authenticator; afterwards every account looks freshly enrolled, so the distinction has to be captured up front.

Both surfaces funnel through one helper each (`api/auth._audit_mfa_event`, `api/portal_auth._audit_portal_mfa_event`) so the shape can't drift: `actor_id` and `entity_id` are both the account, `entity_type` is `auth`, and `details` is a small dict of fixed literals. A vendor user with no `organization_id` (a legacy portal row) has no tenant DB to route to, so its row is skipped rather than failing the request. Regression coverage: `backend/tests/test_mfa_enrollment_audit.py`, which asserts the details of every one of these rows against the credential that produced it and reads one back out of a real tenant `audit_log` end to end.

`/mfa/disable` rides the same three-proof gate: password, a code from the authenticator being turned off, or a passkey assertion (`operation=totp_disable`). It used to be password-only, which left an SSO-only account unable to disable its own TOTP. A stolen session still can't silently strip MFA off, and if the org enforces MFA, disable is blocked outright.

**The supplier portal has no passkey step-up.** `WebAuthnCredential` hangs off a control-plane `users.id` and `VendorUser` is tenant-scoped, so a portal user has no passkey to assert with; `/portal/auth/mfa/*` remains password-or-TOTP-code only. Adding portal passkeys would need a tenant-scoped credential table and is not part of this change.

### Org enforcement

`Organization.settings.mfa.required: bool` — toggled from **Organization → Security** (admin only). When true:

- Every user without `mfa_enabled` is gated. They can complete one email-OTP login but are routed straight to `/profile` to enroll TOTP.
- Per-user disable is blocked — if you don't want enforcement, turn off the org setting first.
- New users (including SCIM-provisioned ones) follow the same gate.

### What stays where

| Data | Lives in | Why |
|---|---|---|
| `User.mfa_secret` (base32) | control-plane DB | Per-user, durable, written ONLY by a successful `/mfa/enroll/verify`. |
| Pending (unverified) enrollment secret | Redis (`mfa:pending_enroll:<user_id>`, `mfa:vendor_pending_enroll:<id>`) | The candidate from an in-flight enrollment. Kept off the account row so starting an enrollment can't disturb the factor already in force; TTL `FEOH_MFA_ENROLL_PENDING_TTL_SECONDS`, consumed on verify. |
| `User.mfa_enabled`, `mfa_enrolled_at` | control-plane DB | Drives login-flow decisions. |
| `WebAuthnCredential` rows (credential id, COSE public key, sign counter, `rp_id`) | control-plane DB (`webauthn_credentials`, migrations 0063 + 0091) | One row per registered passkey, keyed by `user_id` (control-plane, never tenant-fanned). Not secret in the password sense; never logged. `rp_id` records the registrable domain the credential is bound to — see "Passkeys on a custom domain". |
| `Organization.settings.mfa.required` | control-plane DB (JSONB) | Org-wide policy; lives next to other settings. |
| Email-OTP hash | Redis (`mfa:email_otp:<user_id>`) | Short-lived, single-use, no need to persist. |
| WebAuthn ceremony challenge | Redis (`webauthn:{reg,auth}_challenge:<user_id>`) | Short-lived, single-use; the verify call rejects an attacker-chosen challenge. |
| MFA challenge token | client only (sessionStorage) | Stateless JWT — server doesn't need to remember it. |

### SSO + MFA

OIDC SSO sign-in does **not** trigger our MFA challenge — the IdP is the source of truth for "did this person prove a second factor." Configure MFA in Okta/Entra itself if you want it enforced for SSO users.

### Supplier-portal MFA (vendor users)

The supplier portal has its own TOTP MFA (with an email-OTP backup) for `VendorUser`s, mirroring this flow but on the separate vendor auth surface (`typ=vendor` JWT, tenant-scoped). It reuses the same `services/mfa.py` primitives and the same `FEOH_MFA_ENABLED` master switch.

- **Columns:** `vendor_users.mfa_secret` / `mfa_enabled` / `mfa_enrolled_at` (migration `0053_vendor_mfa`, tenant DB) — the exact shape of the `User` MFA columns. The email-OTP backup needs no column (Redis-only, like the employee one).
- **Opt-in per vendor user.** There's no org-wide enforcement for vendors (unlike employee `Organization.settings.mfa.required`). With `FEOH_MFA_ENABLED=false` (local-dev default), an enrolled vendor still logs in with just a password.
- **Login challenge** returns `PortalMFAChallengeResponse` (`methods: ["totp", "email"]`); the vendor completes `POST /api/portal/auth/mfa/challenge` (with `method` totp|email) to mint the access token. Enroll / verify / disable live at `/api/portal/auth/mfa/{enroll,verify,disable}`.
- **Same enrollment safety as the employee surface.** `/portal/auth/mfa/enroll` parks a *candidate* secret in Redis (`mfa:vendor_pending_enroll:<id>`) and writes nothing to `vendor_users`; only `/mfa/verify` promotes it. Re-enrolling over a live factor requires the same `{password?, code?}` step-up (portal password or a code from the current authenticator), so a stolen vendor session can't strip or swap the supplier's second factor. First-time enrollment needs no step-up. A completed enrollment writes `portal.mfa.enrolled` and a disable writes `portal.mfa.disabled`, both PII-free — same rule as the employee surface, because a portal account can stage a bank-detail change.
- **Email-OTP backup.** When the vendor has lost their authenticator, `POST /api/portal/auth/mfa/challenge/email` (public, gated by the `vendor_mfa_challenge` token) emails a single-use 6-digit code via the configured email adapter (`console` in dev). Its server-secret-keyed PBKDF2 digest lives in Redis under a distinct keyspace (`mfa:vendor_email_otp:<id>`, separate from the employee `mfa:email_otp:`) with the `FEOH_MFA_EMAIL_OTP_TTL_SECONDS` TTL. Gated on the vendor having enrolled TOTP — a backup to the authenticator, not a standalone enrollment path. 204-silent for unenrolled / unknown accounts (no enumeration); OTP + email never logged.
- **Token-type isolation.** The portal challenge token carries `typ=vendor_mfa_challenge` — distinct from the employee challenge (`mfa_challenge`) and the vendor access token (`vendor`) — so the three token types can never be substituted for one another across surfaces. Full reference: `backend/docs/supplier-portal.md` § MFA.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/auth/login` | Returns either a token or an MFA challenge. |
| `POST` | `/api/auth/mfa/challenge/email` | Sends a 6-digit code to the user's email. |
| `POST` | `/api/auth/mfa/verify` | Trades a challenge token + code for an access token. |
| `POST` | `/api/auth/mfa/enroll` | Starts TOTP enrollment — returns a CANDIDATE secret + QR. Step-up (`password` or current `code`) required when a factor is already live. |
| `POST` | `/api/auth/mfa/enroll/verify` | Confirms enrollment with a valid code — the only writer of `mfa_secret`/`mfa_enabled`. Audits `auth.mfa.enrolled` on success. |
| `POST` | `/api/auth/mfa/disable` | Turns MFA off (password, a current code, or a passkey assertion). Audits `auth.mfa.disabled` on success. |

### Not in this pass

- **WebAuthn / passkeys** — tracked in roadmap. TOTP covers the bulk of "we need MFA" asks; WebAuthn is a separate, more involved code path.
- **Backup codes (static)** — email-OTP fills the same recovery role and doesn't require the user to safely store anything.
- **Mobile MFA** — the Flutter app currently expects `TokenResponse` from `/login` and doesn't handle `MFAChallengeResponse`. Mobile users can sign in with `FEOH_MFA_ENABLED=false`. Mobile MFA is on the roadmap.

---

## Delegation / Out-of-Office

Any user can set a delegate who receives their approval assignments while they are away.

- **Set a delegate:** `POST /api/auth/delegation` with `{delegate_to_id, until}` — `until` is an ISO datetime after which delegation automatically expires.
- **Check status:** `GET /api/auth/delegation` — returns the current delegation (delegate user, expiry) or empty if none is active.
- **Clear:** `DELETE /api/auth/delegation` — removes the delegation immediately.

When a reviewer is OOO (has an active, non-expired delegation), approval assignments auto-route to their delegate. The audit trail records both the original assignee (`WorkflowStep.original_assigned_to`) and the delegate who actually performed the action.

**Delegation is routing, not a privilege grant** — the delegate still needs
`invoice.approve` to act on what lands in their queue, so delegating to a
lower-privileged colleague confers nothing.

What `POST` refuses, and why:

| Input | Result |
|---|---|
| `delegate_to_id` that isn't a UUID, or an unparseable `until` | `422`. Both used to escape the handler as a bare `ValueError`, i.e. a 500 on ordinary bad input. |
| `until` already in the past | `422` — it would leave the caller believing they are out of office while `resolve_assignee`'s `delegate_until > now` test never fires. |
| A **deactivated** delegate | `404` (same opaque answer as unknown / other-org). `review.assign_reviewer` reassigns the invoice unconditionally, so approvals would land on an account that can never sign in and the invoice would sit owned by nobody. |
| `until` with no timezone | Accepted, interpreted as **UTC**. A naive value handed to the `timestamptz` column is read in the DB *session's* timezone, while every consumer compares against `now(UTC)` — so the same wall-clock string meant a different expiry depending on server config. |

Setting and clearing a delegate each write a PII-free audit row
(`auth.delegation.set` / `auth.delegation.cleared` — ids and the window only):
who receives approval assignments is an access-control fact.

---

## SCIM 2.0 (user provisioning from Okta + Entra)

Automated user lifecycle — IdP pushes create/update/deactivate events to our SCIM endpoints so admins don't hand-manage users.

### Per-tenant bearer auth

Each tenant has its own SCIM bearer token. To generate one, the admin POSTs to `/api/organization/sso/scim-token` (no body). The backend:

1. Mints a 43-char URL-safe token.
2. SHA-256 hashes it.
3. Stores the **hex digest** in `Organization.settings.sso.scim_bearer_hash`.
4. Returns `{token, bearer_hash_prefix}` — the plaintext token is shown **once** and never re-served. The 8-char prefix is a UI identifier so admins can tell which token is currently active.
5. Admin pastes the plaintext token into Okta/Entra's SCIM config alongside the SCIM URL.

Re-calling the endpoint rotates the token: the old hash is overwritten, so any IdP still using the previous token starts seeing 401s.

Every mint / rotation writes an `organization.scim_token_minted` audit row into
the tenant trail, carrying the 8-char digest prefix and nothing else — never the
token or its full digest. This is a tenant-wide user-provisioning credential
(its holder can create, rename and deactivate accounts, and grant roles through
group mapping), so it is audited like the other credential mints
(`api_key.created`, `webhook_subscription.created`).

Every SCIM request Authorization-headers a bearer token; the backend SHA-256s it and looks for a matching `scim_bearer_hash` across all orgs to resolve the tenant. Linear scan, acceptable while tenant count is <<1000.

### Endpoints (all under `/api/scim/v2`)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/ServiceProviderConfig` | Discovery doc — Okta/Entra probe this first. |
| `GET` | `/Schemas/{id}` | Minimal User schema — Entra probes this before syncing. |
| `GET` | `/Users` | List + filter + paginate. Supports `userName eq`, `emails eq`, `externalId eq`, `active eq`. |
| `GET` | `/Users/{id}` | Fetch one user. |
| `POST` | `/Users` | Create. Returns 409 `uniqueness` on duplicate userName. |
| `PUT` | `/Users/{id}` | **Full-resource replace.** Authentik (and RFC 7644 §3.5.1) update users via PUT, not PATCH. Returns 409 `uniqueness` if the new userName collides with another user. |
| `PATCH` | `/Users/{id}` | Partial update. Supports the ops Okta + Entra send (active toggle, userName/externalId/name replace, root-object replace). A `userName` op returns 409 `uniqueness` on a collision. |
| `DELETE` | `/Users/{id}` | **Soft delete** — sets `is_active=false`. Preserves audit trail. |
| `GET` | `/Groups` | List + paginate; `displayName eq` filter. |
| `GET` | `/Groups/{id}` | Fetch one group. |
| `POST` | `/Groups` | Create. 409 `uniqueness` on duplicate displayName. |
| `PUT` | `/Groups/{id}` | Full replace (displayName + members). |
| `PATCH` | `/Groups/{id}` | Add/remove/replace members + rename — the op/path shapes Okta/Entra/Authentik send. |
| `DELETE` | `/Groups/{id}` | Remove the group (revokes its mapped role from former members). |

Both `PUT` and `PATCH` `db.refresh()` the row after the flush: the `UPDATE` fires
`updated_at`'s server-side `onupdate`, which SQLAlchemy expires — reloading it in
the async handler avoids a sync lazy-load (`MissingGreenlet` → 500) when the SCIM
response reads `meta.lastModified`.

**`userName` uniqueness is platform-wide, not per-tenant.** `users.email` carries
a global `UNIQUE` constraint — it is the login identifier, and `/auth/login`
resolves an account by address alone with no tenant hint. So `POST`, `PUT` and
`PATCH` all check the address across every org (`scim._email_taken`, the same
scope `admin.create_user` uses) and return 409 `uniqueness`. Scoping the check to
the calling tenant would let an address already held in a *different* tenant slip
past the guard and trip the DB constraint on flush — an unhandled 500 where the
RFC requires a 409, which providers then retry forever.

### Filter syntax

Only the filter subset Okta + Entra actually use:

```
userName eq "alice@acme.com"
emails eq "alice@acme.com"
externalId eq "okta-user-id"
active eq true
```

Anything else returns a `400 invalidFilter` with a clear message so the IdP surfaces a useful error.

### Local testing with Authentik (no cloud account)

You don't need an Okta/Entra tenant to exercise SCIM. A Keycloak container covers
inbound SSO; an **Authentik** container (Docker Compose `idp` profile) covers
outbound SCIM — it's the SCIM *client* that pushes users into `/api/scim/v2`:

```bash
pnpm idp:up        # Keycloak + Authentik (Docker)
pnpm scim:seed     # set the matching SCIM bearer token on the acme tenant
pnpm dev           # the app must be running — Authentik POSTs to :8000
# Authentik admin http://localhost:9002 (akadmin / admin) → Providers → Run sync
# Provisioned users appear at http://acme.localhost:7777/admin
```

Deterministic CI coverage of the same contract (create → filter → PUT → PATCH
deactivate → DELETE, verified in `/admin`) lives in
`frontend/tests-e2e/scim/provisioning.spec.ts` (`pnpm test:scim`) — it runs
without the Authentik container, which CI can't host. Full walkthrough:
[`local-sso-keycloak.md` § Authentik](local-sso-keycloak.md#authentik--local-scim-provisioning).

### Groups → role mapping

`/Groups` maps IdP groups onto our RBAC `Role` rows. Group state (displayName,
externalId, member ids) is stored as JSONB on `settings.sso.scim_groups` —
groups are few per tenant and the only thing we do with one is map it to a role,
so no dedicated table (a control-plane `scim_groups` table is the upgrade path if
volume grows; the service boundary stays the same). The mapping lives in
`settings.sso.scim_group_role_map` (`{displayName: role_name}`).

On any membership change (create / PUT / PATCH / delete), affected users are
**reconciled** (`services/scim_groups.py::reconcile_user_roles`): a user's
SCIM-derived roles = the mapped role of every group they belong to. Only roles
named in the map are added/removed — manual and JIT-default assignments to other
roles are never touched. **Contract:** a role named in the map becomes
IdP-managed for any user the IdP places in (or removes from) a mapped group.
Member ids are validated against real org users before reconciliation, so a
phantom id can't grant a role. Unmapped groups are stored and synced but grant
nothing until an admin adds them to the map.

---

## Role-based access control (RBAC)

Every authenticated endpoint declares the roles it accepts via a single dependency, `require_roles(*roles)` in `app/api/deps.py`. The check is **any-of**: a user passes if they hold at least one of the listed roles. This mirrors the frontend's `hasAnyRole()` gate so backend and UI permissions can't drift apart.

```python
from app.api.deps import ROLE_ADMIN, ROLE_AP_MANAGER, require_roles

@router.post("/invoices/{invoice_id}/approve")
async def approve_invoice(
    invoice_id: uuid.UUID,
    user: User = Depends(require_roles(ROLE_ADMIN, ROLE_AP_MANAGER)),
):
    ...
```

Failed checks return `403 Forbidden` with `{"detail": "Your role does not permit this action."}` and emit a `WARNING`-level log (`RBAC denied: user=… roles=… required_any=… METHOD PATH`) so monitoring can pick up brute-force probing.

### Permission matrix

The matrix below is the source of truth — it mirrors the per-route `roles` gates in `frontend/src/lib/nav.ts` (which drives sidebar + section-tab visibility) and the `!isClerkOnly` / `isManager` / `isCfo` checks in invoice + workflow components. Roles are non-exclusive: a user may hold any combination.

It describes the **endpoints**, and in two places the nav is deliberately narrower than the read it gates: `/workflows` and `/organization` are `admin`-only sidebar rows whose reads are open to any authenticated user. Both routes are therefore reachable by typed URL — `/workflows` redirects (it is an editing surface whose every control would 403, `docs/decisions.md` §144) while `/organization` renders read-only and says which panels it cannot fill (§153). A row that is narrower than its endpoint is a product call, not drift; a row that is *wider* is the defect to look for.

| Endpoint area | Read | Write |
|---|---|---|
| `/admin/*` (user CRUD, role list) | admin | admin |
| `/organization` — `GET` settings / branding / custom-domains / data-residency | any-authenticated (**payload projected by role**) | admin |
| `/organization` — chat notifications, email intake, fraud-rule defaults, connection tests, SCIM token | admin | admin |
| `/workflows` (definition CUD) | any-authenticated | admin |
| `/exceptions` (list + resolve) | admin · ap_manager | admin · ap_manager |
| `/vendors/{id}/verify`, `/reject`, `/sync-erp` | — | admin · ap_manager |
| `/vendors` create / patch / delete | — | admin · ap_manager |
| `/gl-accounts` create / sync-erp | any-authenticated (read) | admin · ap_manager |
| `/purchase-orders` sync-erp | any-authenticated (read) | admin · ap_manager |
| `/invoices/{id}/assign` (route to a reviewer) | — | admin · ap_manager |
| `/invoices` mutate (create / patch / delete / line-items / bulk) | any-authenticated (read) | admin · ap_manager · cfo |
| `/invoices/{id}/upload`, `/extract`, `/reset-extraction` | — | admin · ap_manager · cfo |
| `/invoices/{id}/approve`, `/reject`, `/resubmit`, `/complete`, `/send-to-erp`, `/retry-erp` | — | admin · ap_manager · cfo |
| `/payments/*` (incl. runs create + execute) | admin · ap_manager · cfo | admin · ap_manager · cfo |
| `/cards` (list / dashboard / generate / cancel / details / rebates) | admin · ap_manager · cfo | admin · ap_manager · cfo |
| `/dashboard` | any-authenticated | — |
| `/auth/me`, `/auth/mfa/*` (per-user MFA mgmt), `/auth/change-password` | any-authenticated | any-authenticated |

### "Read open to all authenticated" surfaces

Invoices, workflow definitions list/active-steps, GL accounts list, and POs list are readable by every authenticated user (including pure clerks). Clerks can see the work; they just can't take action on it. This matches the frontend, where the invoice list page is visible to clerks but write controls are hidden.

The org settings read is open too, but its `settings` payload is **projected by role** — `backend/app/services/org_settings_view.py::NON_ADMIN_SETTINGS` is an allow-list, so a non-admin gets `company`, `invoice_defaults`, `reporting_currency`, `payments.home_currency`, `brand` and `erp.integration_method`, and never the tenant's third-party credentials (ERP client secret, processor credentials, card API key, the SSO client secret). The `/organization` page is therefore read-only for a non-admin **and says which panels it cannot fill**, rather than rendering the platform defaults its fields fall back to; widening the projection to populate them would re-open the leak that module closed (`docs/decisions.md` §153).

### Endpoints that intentionally do **not** require a JWT

Every such route is declared in `tests/test_rbac.py`, in **one of two lists that
carry different obligations**. Which list a route belongs in is not a labelling
choice — each list asserts something specific, and a route in the wrong one
fails.

#### `PUBLIC_BY_DESIGN` — must answer an anonymous caller

Listing a route here does not exempt it: the test **drives** each entry with a
real credential-free request and asserts the response is not 401/403. A route
that in fact requires a credential fails the probe, so it cannot be quietly
parked here.

| Route | Why it is public |
|---|---|
| `GET /health` | Liveness probe. No identity, no tenant data. |
| `GET /public-config` | Non-secret config (hCaptcha sitekey, tenant URL template) the SPA needs before a session exists. |
| `GET /v1/openapi.json`, `GET /v1/docs` | The published contract + reference for the public `/api/v1` surface — public to read like any API doc; both 404 when `FEOH_PUBLIC_API_ENABLED` is off. |
| `GET /scim/v2/ServiceProviderConfig`, `GET /scim/v2/Schemas/{id}` | SCIM discovery documents. IdPs (Entra) probe them before they hold a bearer token; static capability/schema flags, no tenant data. |
| `GET /auth/sso/config`, `GET /auth/saml/config` | The login page decides whether to render the SSO button before anyone is signed in. Returns only `{enabled, provider, sso_only}`. |
| `GET /auth/sso/authorize`, `GET /auth/saml/login` | Entry legs of the OIDC / SAML dance — they 302 the browser to the IdP. There is no session yet, by definition. |
| `GET /auth/saml/metadata` | SP `EntityDescriptor` XML an IdP admin registers. No secrets — only the public SP cert when AuthnRequest signing is on. |
| `GET /signup/slug-check`, `POST /signup/start` | Self-service tenant signup: there is no account to authenticate yet. Abuse is bounded by the per-IP / per-email rate limits and the captcha, not by a credential. |
| `POST /auth/forgot-password` | **Self-service password recovery.** It has to be reachable by a user who — by definition — cannot log in; requiring a credential to ask for a password reset would be circular. It is not an unguarded surface: it answers *identically* for a known and an unknown address (the email is dispatched on a detached task, so response time can't enumerate accounts either), and it is throttled on two axes — per IP (5/hour) and per submitted address (3/hour), so an attacker rotating IPs still can't email-bomb one victim's inbox with reset links. The state-changing half of the flow is **not** public: `POST /auth/reset-password` is gated by the single-use emailed token (see below). |
| `GET /portal/branding` | White-label brand read for the **unauthenticated** supplier-portal login page. Tenant resolved by the `get_tenant` chokepoint; returns only the whitelisted `BrandConfig` fields. See `white-label.md`. |

#### `ALTERNATE_AUTH` — gated by something other than the JWT

Each entry names its **specific** gate, and the test asserts that gate is still
there: dependency gates are resolved **by identity** in `route.dependant`, and
body gates (an HMAC verify, a signed-token decode, a password check — none of
which are FastAPI dependencies) are asserted as symbols the handler's own code
references. Deleting the gate fails the suite.

Be clear on what the symbol half proves. It is a *static* check — it makes the
structural regression (the gate simply going away, or being swapped for a
different primitive) impossible to land unnoticed. It cannot prove the gate is
still reached on every path, or that its verdict is still acted on; a call left
in place but ignored would pass it. That residual is what the per-route
behavioural suites cover (`test_slack_approvals`, `test_webhook_security`,
`test_payment_webhook_security`, `test_email_intake`, `test_peppol_inbound`,
`test_auth_*`), which drive the rejection paths for real.

| Routes | Gate |
|---|---|
| `POST /auth/login`, `POST /portal/auth/login` | The submitted password, through the shared `pwd_context` (`verify_password` / `dummy_verify`). |
| `POST /auth/logout`, `POST /portal/auth/logout` | Bearer token read off the header and validated by `decode_token` rather than through `Depends`. |
| `POST /auth/mfa/verify`, `POST /auth/mfa/challenge/email`, `POST /auth/mfa/passkey/authenticate[/verify]` | The login-issued MFA challenge token (`typ=mfa_challenge`) — password already proven, access token not yet issued. The passkey verify additionally checks the assertion against the single-use login-purpose challenge slot. |
| `POST /portal/auth/mfa/challenge[/email]` | The portal's own `typ=vendor_mfa_challenge` token — distinct from the employee one so neither crosses the AP/vendor boundary. |
| `POST /auth/reset-password` | The single-use emailed reset token IS the credential; `consume_reset_token` GETDELs it atomically, so a replay 400s exactly like an expiry. |
| `POST /auth/sso/callback` | Server-minted single-use `state`, then the IdP's signed, nonce-bound ID token. |
| `POST /auth/saml/acs`, `POST /auth/saml/exchange` | Server-minted single-use RelayState + python3-saml's signed-assertion verification; the exchange leg consumes a one-time handoff code. |
| `POST /signup/complete` | The emailed `EmailVerification` token, taken `FOR UPDATE` and consumed so two calls can't double-provision a tenant. |
| `POST /cards/webhook/{provider}`, `POST /erp/webhook/{erp_type}` | Per-tenant webhook signing secret (HMAC over the raw body) + Redis dedupe on the provider event id. |
| `POST /payments/webhook/{tenant_slug}/{provider}`, `POST /billing/webhook/{provider}` | The adapter's `parse_webhook` verifies the processor HMAC, then event-id dedupe. |
| `POST /email-intake/inbound/{provider}` | Process-level HMAC over the raw body plus the opaque per-tenant token in the recipient address. |
| `POST /peppol/inbound/{tenant_slug}` | Access Point HMAC; dedupe is the DB `uq_peppol_message_id` index. |
| `POST /catalogs/punchout/return/{tenant_slug}` | Supplier shared-secret HMAC over the cart body, BuyerCookie-correlated to a pending session. |
| `GET /invoices/email-action/{token}`, `POST .../confirm` | The signed single-action token in the URL IS the credential (re-verified on the acting request, never trusted from the confirm page). |
| `POST /approvals/slack/interactivity`, `POST /approvals/teams/interactivity` | The provider request signature **and** the signed single-use action token in the button / card action. |
| `GET /portal/cards/{token}` | Single-use card-reveal token; `consume_reveal_token` claims it atomically before the PAN is fetched. |
| `GET/POST/PUT/PATCH/DELETE /scim/v2/Users[...]` and `/scim/v2/Groups[...]` (12 routes) | The per-tenant SCIM bearer token, via `Depends(get_scim_tenant)` — asserted present in the dependency tree by identity. |

### Coverage gate

`tests/test_rbac.py::test_every_endpoint_requires_auth_or_is_explicitly_public`
walks every router at import time. If a new endpoint ships without an auth
dependency and is on neither allowlist, the test fails. This is the *one* test
that has to fail noisily in CI when someone forgets RBAC.

**Auth is resolved by identity, not by name.** The gate walks `route.dependant`
— the same tree walk `test_sod_endpoint_wiring.py` uses — and compares against
the actual callables `app/api/deps.py` / `portal_deps.py` export:
`get_current_user`, `get_current_vendor_user`, `get_api_key_principal`. Every
factory (`require_roles`, `require_permission`, `require_api_scope`,
`require_entitlement`, `require_api_entitlement`) declares one of those as its
own sub-dependency, so all five are covered for free.

It did not always work that way, and the earlier version is worth remembering as
the shape of a guard that *looks* like it is checking something. It read only
the endpoint function's own signature and accepted any dependency whose closure
happened to be named `checker` — the house style for all five factories. A name
is not a security property in either direction: renaming the closure broke the
gate on every route at once, and an unrelated function called `checker` that
authenticated nobody satisfied it. Paired with a single `NO_AUTH_REQUIRED` set
that asserted *nothing* once an entry was listed, the result was demonstrable:
deleting `Depends(get_scim_tenant)` from all six SCIM `/Groups` handlers — the
credential that creates accounts and grants roles — left the suite green. Both
planted bypasses now fail.

Three companion tests keep the allowlists honest: an entry naming a route that
no longer exists fails (no silent rot), a route declared in both lists fails
(the weaker obligation would be the one satisfied), and a route that already
carries a real auth dependency may not be allowlisted at all (dead weight that
would pre-authorise it if its auth were later removed).

`test_split_endpoints_are_permission_gated` resolves the `require_permission`
gate by its **code object** — every closure that factory returns shares one
`__code__` — so it likewise can't be satisfied by an unrelated `checker` or
broken by renaming the real one.

### Adding a new endpoint

1. Pick the right role tuple from the matrix above (or invent one and update this doc).
2. Add `user: User = Depends(require_roles(ROLE_X, ...))` to the endpoint signature.
3. If the endpoint genuinely cannot take a JWT, put it in the right list in `tests/test_rbac.py` — `PUBLIC_BY_DESIGN` (and supply a probe that drives it anonymously) or `ALTERNATE_AUTH` (and name the gate it is actually protected by) — and add a row to the tables above.
4. Run `pytest tests/test_rbac.py` — the coverage gate will tell you if anything's missing.

### Not in this pass

- **Segregation of duties (SoD)** — *Done.* The classic AP invariant ("approver ≠ creator") ships as `services/approval_chain.check_segregation`, enforced in `services/review.approve_invoice` and default-ON (`require_segregation: true`). It keys on `Invoice.uploaded_by_id` **plus** `Invoice.segregation_actor_ids` (a recurring template's author and its material editors), so its correctness depends on every employee-facing creation path recording the creator — see § Segregation of duties on a workflow's approval step above, and its "NULL uploader" subsection for what NULL means and which paths legitimately produce it. The same set (plus the flag's recorded raiser) also governs **clearing a payment-blocking exception**, which is the other money-path sign-off an implicated actor could otherwise give themselves — see § Segregation of duties on the exception queue.
- **Per-org custom roles with teeth** — *Done.* Custom roles now grant access via the granular permission layer (`roles.permissions` + `require_permission`) — see § Granular permissions / segregation of duties above. A custom role granted, say, only `invoice.approve` can approve invoices but is 403'd on payment execution. Permission CRUD itself stays admin-only on purpose.
- **Audit log of denied requests** — denials are logged via Python `logging.warning` for now, not persisted to the `audit_log` table. If oncall wants to query historical denials, surface them via centralized log shipping (planned under SOC 2 readiness).
