---
description: Find server-only secrets that may have leaked into a client bundle, git history, GitHub Actions logs, or a public asset
---

Audit for secrets / env vars / API keys that should be server-only but are reachable from a client bundle, a public asset, or git history.

## Goal

Two trust boundaries, and a leak crosses one of them:

- **Client vs server.** Anything reaching `frontend/` as `PUBLIC_*` ships inside the static bundle to every browser; everything else must stay backend-side. The frontend is `adapter-static` with no SSR, so it has no server-only path to hide a secret in.
- **Public repo vs secret store.** **This repo is public.** Per the root `CLAUDE.md`, production secrets live in the private estate repo `Absence0760/infra-secrets` (per-project subdir, AWS KMS) — an encrypted `*.sops` payload must **not** be committed here, because ciphertext in public history is a permanent artefact even after rotation. Today no `*.sops` file exists in this repo; a newly-tracked one is itself a finding.

Find any key on the wrong side of either boundary.

## What to check

1. **SOPS files — first confirm whether any exist here at all.**
   - `git ls-files '*.sops'` should return **nothing** in this repo (see Goal). If it returns a file, that is a **High** on its own: report it with the pointer to `~/github/project-mgmt/docs/secrets-management.md`, and treat every key inside it as needing rotation *if* it was ever a real value.
   - If one exists anyway, confirm it matches the unstructured-JSON SOPS shape before anything else:
     - A single top-level `"data": "ENC[AES256_GCM,data:...,iv:...,tag:...,type:str]"` blob holding the encrypted body.
     - A `"sops"` metadata object with `kms` recipient ARNs, `mac` (`ENC[...]`), `unencrypted_suffix`, `version`.
   - The whole-file blob shape is normal for SOPS-encrypted `.env` / `.tfvars` (unstructured input) — distinct from YAML SOPS where each value is individually `ENC[...]`. Either is fine; reject any file that's plaintext at the top level.
   - A SOPS file that's been edited without `sops <file>` (e.g. via `vim` on the encrypted blob) loses encryption integrity — the `mac` won't validate. Flag if you can confirm a recent direct edit.

2. **Plaintext SOPS siblings absent from git.**
   - `backend/.env`, `frontend/.env.local`, `infra/terraform.tfvars`: confirmed gitignored.
   - `git log --all --full-history -- backend/.env frontend/.env.local infra/terraform.tfvars` should return zero commits ever. If it returns any, the secret is permanently exposed and every value in it needs rotation — flag as Critical.

3. **`.env` files at workspace roots.**
   - `backend/.env`, `frontend/.env`, `frontend/.env.local`: gitignored (personal overrides). The committed env files are `backend/.env.development` + `frontend/.env.development` — safe local-dev defaults only (loopback URLs, mock adapters, the `change-me` JWT key, `PUBLIC_*`; no real secrets) — plus the KMS-encrypted `*.sops` files.
   - Confirm a committed `.env.development` holds no real secret: grep it for anything that looks live (non-loopback hostnames, base64 blobs, real-looking API keys). A real secret in a committed `.env.development` is Critical.
   - Run `git log --all --full-history -- backend/.env frontend/.env` to confirm neither plaintext-secret file has ever been committed.

4. **Client-bundle leakage (frontend).**
   - SvelteKit env vars are split: variables declared in `src/env.ts` with `public: true` are imported from `$app/env/public` and inlined into the client bundle; `$app/env/private` is server-only. Per `frontend/CLAUDE.md`, the frontend stays static — there's no `$env/dynamic/private` anywhere; if it appears, that's a Critical because it implies an SSR adapter was added.
   - Grep `frontend/src/` for `$app/env/private`, and `frontend/src/env.ts` for an entry without `public: true`. Every hit is a finding.
   - Grep `frontend/src/` for raw `process.env` references. SvelteKit's static build doesn't expose `process.env` to the client — any reference is either dead code or a bug.
   - Remember `PUBLIC_API_URL` is baked in **at build time**, not read at runtime: a value that is correct locally and wrong in the deployed bundle is a deployment bug, not a secret leak — but a *secret* baked the same way is unrecoverable without a rebuild.

5. **Server-only env touched from a non-server frontend path.**
   - The frontend has no server-only paths today (static adapter). Any reference to `$app/env/private` or to a non-`PUBLIC_*` env var from `frontend/src/` is a finding.

6. **Backend env hygiene (Python / FastAPI).**
   - Every setting is declared in `backend/app/config.py` under the `FEOH_` prefix. A secret read with a hardcoded fallback (`os.environ.get("X", "some-default")`, or a pydantic field defaulting to a usable key) is **Critical** per the root `CLAUDE.md` invariant — adapters must fail closed without a credential, not silently substitute one. The `change-me` JWT key in `.env.development` is the deliberate exception, and `bin/`'s deploy preflight already refuses it in a deployed env; confirm that guard still exists.
   - There are **two entry points**: `main.py` (local dev, loads `.env.development` then `.env`) and `app/main.py:app` (production, no dotenv). Grep everything reachable from `app/main.py` — and from any Lambda handler — for `dotenv`. Per the root `CLAUDE.md` § What not to do, a `dotenv` import reachable from a Lambda entry point is a finding.
   - `backend/.env.development` is the name list. Every `FEOH_*` a deployed env needs must have a home in the private secrets repo; report by **name**, never by value, and never decrypt anything into this transcript.

7. **GitHub Actions workflow secrets.**
   - `.github/workflows/*.yml`: every `env:` block should reference `${{ secrets.X }}` or `${{ vars.X }}`, never a literal value.
   - `actions-runner` / build steps should not `echo $SECRET_X` or `set -x` with secret values in scope.
   - The credential-bearing workflows here are `aws-deploy.yml`, `terraform.yml`, `mobile-release.yml`, `dependabot-lockfile.yml` (a fine-grained PAT) and `claude.yml`. Each must use OIDC (`aws-actions/configure-aws-credentials` with `role-to-assume`) rather than `aws-access-key-id` / `aws-secret-access-key`; a long-lived AWS key in any workflow is **Critical**.
   - `dependabot-lockfile.yml` specifically: its regenerate jobs run resolvers against PR-head code and must hold **no** secret (`persist-credentials: false`); only the separate `push` job touches the PAT, and it must never check out the PR head. A change that merges those two jobs is **Critical**.
   - Signing material for `mobile-release.yml` (keystore, provisioning profile, App Store key) must arrive from `secrets`, never a tracked file.

8. **Public asset leak.**
   - Search `frontend/static/` and `frontend/build/` (if present) for the key shapes this stack actually uses: `sk_live_` / `sk_test_` (Stripe), `feoh_live_` (this platform's own API keys), `sk-ant-` (Anthropic), `sk-` (OpenAI), `AKIA` (AWS), `xoxb-` / `hooks.slack.com` (Slack), `Bearer `, and hex strings ≥ 32 chars.
   - `feoh_live_…` deserves its own pass: it is a **customer's** API key, minted by `/api/api-keys` and shown exactly once. One in a log, a fixture, a test snapshot or a doc is **Critical** and needs that key revoked, not just deleted.

9. **Git history pickaxe.**
   - `git log --all -S 'FEOH_SECRET_KEY' -S 'FEOH_ANTHROPIC_API_KEY' -S 'FEOH_LITHIC_API_KEY' -S 'FEOH_BILLING_STRIPE_API_KEY' -S 'FEOH_APPROVAL_SIGNING_KEY' -S 'FEOH_EMAIL_ACTION_SIGNING_KEY' -S 'FEOH_SLACK_SIGNING_SECRET' -S 'AWS_ACCESS_KEY' -S 'sk-ant-' -S 'sk_live_' -S 'feoh_live_' --source --pretty=fuller`
   - The HMAC keys in that list are not "just" secrets: `FEOH_APPROVAL_SIGNING_KEY` backs the SOX non-repudiation signature on every approval, and `FEOH_EMAIL_ACTION_SIGNING_KEY` / `FEOH_SLACK_SIGNING_SECRET` / `FEOH_TEAMS_SECURITY_TOKEN` mint the tokens that let someone approve an invoice **with no login**. A leak of any of those is an approval-forgery capability — rotate first, investigate second.
   - The `-S` "pickaxe" finds commits that added or removed the literal string. A single touch on a real secret means that value is permanently exposed and needs rotation regardless of subsequent removal — flag as Critical with the recommendation "rotate the underlying credential, the value can be recovered from git history."

10. **`.gitignore` covers the right paths.**
    - Confirm `.gitignore` ignores: `backend/.env`, `frontend/.env`, `frontend/.env.local`, `mobile/.env`, `infra/terraform.tfvars`, `infra/*.tfstate*`, `.envrc`, and the mobile signing material (`mobile/android/key.properties`, `*.jks`, `*.keystore`, `*.p12`, `*.mobileprovision`). Any missing → Medium.

## Report

- **Critical** — a real secret in git history; an SSR adapter that exposes server-only env to the client; an AWS access key in a workflow; a secret read with a working hardcoded fallback; a leaked HMAC signing key that mints login-free approval tokens; a customer `feoh_live_…` API key anywhere in the tree.
- **High** — server-only env referenced from a non-server frontend path; `dotenv` reachable from a Lambda entry point; a workflow that logs an env var; an OIDC role whose `:sub` condition is missing or wildcarded; any `*.sops` payload committed to this public repo.
- **Medium** — a `FEOH_*` a deployed env needs with no home in the private secrets repo; a declared setting with no documented purpose; `.gitignore` missing a path.
- **Low** — undocumented env intent, a missing entry in `.env.development`, an overscoped credential (a write-scope provider token used only for reads).

For each: the literal env-var name and the file:line, what should change. **Never paste a found key value into the report — identify by name + location only.**

## Useful starting points

- `backend/app/config.py` — every `FEOH_*` setting and its default; the place a hardcoded fallback hides
- `backend/.env.development`, `frontend/.env.development`, `infra/terraform.tfvars.example` — the committed safe-default shapes
- `frontend/CLAUDE.md` — the static-only / no-SSR invariant
- `.github/workflows/{aws-deploy,terraform,mobile-release,dependabot-lockfile,claude}.yml` — the credential-bearing workflows
- `.github/workflows/gitleaks.yml` — the scanner already running in CI; a finding it should have caught is a *scanner* finding too
- `.claude/hooks/security-patterns.sh` — the pre-commit pattern guard
- `docs/secrets-rotation.md` — what to rotate, when, and how (the fix half of every Critical here)
- Root `CLAUDE.md` § Secrets, and `~/github/project-mgmt/docs/secrets-management.md` — why nothing encrypted is committed to this public repo

## Delegate to

Use the `repo-security-auditor` agent: `"Audit for server-only secrets that may have leaked into a client bundle, public asset, GitHub Actions log, or git history."`

Read-only. Recommendations only — never paste a found key into the report. Identify by name + location.
