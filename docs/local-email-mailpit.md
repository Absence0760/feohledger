# Local email preview with Mailpit

The app sends transactional email (signup verification, welcome + set-password link,
scheduled CFO reports) through pluggable adapters in
`backend/app/services/email_adapters/`:

| Provider (`FEOH_EMAIL_PROVIDER`) | What it does |
|---|---|
| `console` (default) | Logs the email to backend stdout — no rendering, no inbox |
| `smtp` | Sends over SMTP — point it at **Mailpit** locally to get a real web inbox |
| `ses` | AWS SES (production); LocalStack-aware for offline capture as JSON |

**Mailpit** is a local SMTP sink + web inbox: the app delivers mail to it over
SMTP and you read the rendered HTML at `http://localhost:8025`. It only captures
— it never relays to the real internet — so it's safe for dev. This is the
local-first guard rail applied to email.

## TL;DR

```bash
pnpm mail:up       # Mailpit — SMTP on :1025, web inbox on http://localhost:8025
# backend/.env:
#   FEOH_EMAIL_PROVIDER=smtp
#   FEOH_SMTP_HOST=localhost
#   FEOH_SMTP_PORT=1025
pnpm dev:backend   # restart to pick up the env
# trigger an email (sign up a tenant, run a scheduled report) → open :8025
pnpm mail:down     # stop it
```

## Configuration

The `smtp` adapter reads these settings (`app/config.py`), wired through the
email dispatcher:

| Setting | Default | Notes |
|---|---|---|
| `FEOH_SMTP_HOST` | `localhost` | Mailpit host |
| `FEOH_SMTP_PORT` | `1025` | Mailpit SMTP port |
| `FEOH_SMTP_USERNAME` | (empty) | Mailpit needs none |
| `FEOH_SMTP_PASSWORD` | (empty) | secret — set via sops for a real relay, never for Mailpit |
| `FEOH_SMTP_USE_TLS` | `false` | Mailpit is plaintext locally |
| `FEOH_EMAIL_FROM` | `no-reply@localhost` | From address |

The same `smtp` adapter works against any SMTP relay in a deployed environment —
set the host/port/credentials (credentials via sops) and flip
`FEOH_EMAIL_PROVIDER=smtp`.

## Inspecting captured mail

- **Web inbox:** http://localhost:8025 — rendered HTML + plaintext, headers,
  source, search.
- **API:** `curl http://localhost:8025/api/v1/messages` (list),
  `.../api/v1/message/<id>` (one). Useful for asserting in scripts.

## Coverage

The adapter itself (dispatcher selection, MIME build with text + HTML parts, the
SMTP connect/send) is locked by `backend/tests/test_smtp_email_adapter.py`,
which mocks `smtplib` and runs in CI without the container. Mailpit is the
hands-on complement for actually eyeballing rendered email.

## Notes

- Mailpit vs LocalStack SES: use **Mailpit** (`smtp` provider) for a nice inbox
  while iterating on email copy/HTML; use **LocalStack SES** (`ses` provider +
  `FEOH_AWS_ENDPOINT_URL`) when you specifically want to exercise the SES code
  path. Both capture rather than deliver.
- This is *outbound* email. Inbound email-to-invoice intake is a separate webhook
  path — see `backend/docs/email-intake.md`.
