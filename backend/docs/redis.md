# Redis

Redis 7 — required at runtime for auth, MFA, SSO, and rate-limiting. Not optional.

## Running

Started as part of the Docker Compose stack:

```bash
cd backend
docker compose up -d redis
```

## Access

- **Host:** `localhost`
- **Port:** `6379`
- **No password** (development only)
- Configurable via `FEOH_REDIS_URL` (default `redis://localhost:6379`)

## What's stored

Every key has a TTL — Redis is used as a transient store, not a database.

| Key prefix | TTL | What it does |
|---|---|---|
| `token:blocked:<jti>` | remaining JWT lifetime | JWT blocklist. `POST /api/auth/logout` writes here; `get_current_user` checks every authenticated request. |
| `mfa:email_otp:<user_id>` | `FEOH_MFA_EMAIL_OTP_TTL_SECONDS` (default 6 min) | Server-secret-keyed PBKDF2 digest of an outstanding email-OTP backup code — keyed (secret salt) so the low-entropy 6-digit code can't be brute-forced from the digest. Single-use. |
| `sso:state:<state>` | `FEOH_SSO_STATE_TTL_SECONDS` (default 10 min) | OIDC state + nonce binding. CSRF + replay protection across the authorize/callback hop. |
| `sso:discovery:<sha256(url)>` | 1 day | Cached OIDC discovery document. |
| `sso:jwks:<sha256(uri)>` | 1 day | Cached IdP JWKS for ID-token signature verification. |
| `ratelimit:signup:<ip>` | sliding window | Self-service signup rate limiter (`FEOH_SIGNUP_RATE_LIMIT_PER_HOUR`). |
| `erpcalls:<provider>:<key digest>:<YYYYMMDD>:total` / `…:org:<org_id>` | 2 days | ERP API calls made on one subscription key per UTC day, in all and per org — Blackbaud's shared SKY quota (`services/erp_call_budget.py`, decisions §322). The key is a SHA-256 digest of the subscription key, never the key. |
| `erpcalls:<provider>:<key digest>:cooldown` | the provider's `Retry-After` | A 429 / quota 403 every tenant on that key waits out before its next call. |
| `erpcalls:<provider>:<key digest>:<YYYYMMDD>:notice:<org_id>:<notice>` | 2 days | Claim (SET NX) that the org's admins got today's `erp_call_quota` notice; released when nobody could be told. |

Code: `app/redis.py` (blocklist), `app/services/mfa.py` (email-OTP), `app/services/sso.py` (state, discovery, JWKS), `app/services/rate_limit.py` (signup), `app/services/erp_call_budget.py` (ERP call budget — fails OPEN on a Redis error, unlike auth). The table is the main keys, not every one: webhook dedup, OAuth refresh locks, MFA and password-reset state live here too (see each module).

## Operational impact

- **A Redis outage takes auth down.** The blocklist check is on every request; a connection error becomes a 500. Plan for an HA Redis (ElastiCache + replica or similar) in production.
- **Restart loses sessions.** Without persistence, all blocklist entries vanish on Redis restart — already-revoked tokens become valid again until they naturally expire (≤30 min). Configure RDB/AOF persistence or accept the bounded risk.
- **Don't share with other apps.** Key prefixes are unscoped; running another service against the same Redis risks key collisions.

## Connecting via CLI

```bash
# Using redis-cli directly
redis-cli -h localhost -p 6379

# Or via Docker
docker exec -it backend-redis-1 redis-cli

# Test connection
> PING
PONG
```

## Inspecting state

```bash
# Outstanding email-OTPs (debugging MFA delivery)
redis-cli --scan --pattern 'mfa:email_otp:*'

# Outstanding SSO state bindings
redis-cli --scan --pattern 'sso:state:*'

# Active blocklist (revoked tokens)
redis-cli --scan --pattern 'token:blocked:*'
```

## Data persistence

Redis is in-memory and is **not** persisted across container restarts in the current Docker Compose config. This is fine for dev. Production must enable persistence (RDB snapshots or AOF) or use a managed service — see operational impact above.
