# Getting Started

Full-stack accounts payable application with multi-tenant support. Everything runs locally for development.

## Prerequisites

- **Node.js** + **pnpm** (frontend)
- **Python 3.12+** (backend)
- **Docker & Docker Compose** (PostgreSQL, Redis, MinIO)

## Quick Start

### 1. Start infrastructure (PostgreSQL, Redis, MinIO)

```bash
cd backend
docker compose up -d
```

This starts:
- **PostgreSQL 16** on `localhost:5432` (includes `feohledger`, `feoh_acme`, `feoh_techflow` databases)
- **Redis 7** on `localhost:6379`
- **MinIO** on `localhost:9000` (console: `localhost:9001`)

> **Note:** If you have a local Postgres running on port 5432 (e.g. via Homebrew), stop it first:
> `brew services stop postgresql@17`

### 2. Start the backend API

```bash
cd backend
python3 -m venv .venv        # first time only
source .venv/bin/activate
pip install -e ".[dev]"       # first time only
python scripts/seed.py        # first time only — seeds control plane + 2 tenants
python main.py
```

### 3. Start the frontend

```bash
cd frontend
pnpm i                    # first time only
pnpm dev
```

> No `.env` setup: `backend/.env.development` and `frontend/.env.development`
> are committed with safe local defaults. The backend loads them via `main.py`;
> the frontend loads `.env.development` natively in Vite dev mode. For a
> personal override, drop a gitignored `backend/.env` / `frontend/.env.local` —
> it wins over the committed defaults.

### 4. Open the app

Access the app via a tenant subdomain:

- **Acme Corp:** http://acme.localhost:7777
- **TechFlow Inc:** http://techflow.localhost:7777

> `*.localhost` works natively in Chrome, Firefox, and Edge. See [multi-tenancy.md](multi-tenancy.md) for Safari setup.

## Demo Logins

All demo accounts use password `demo`.

Under `pnpm dev` the login page also shows a **Demo accounts (dev only)** panel
listing the current tenant's seeded users, and one click signs in as that user,
so you never have to type these. It is loaded behind `import.meta.env.DEV`, so
production builds don't contain it, and `devDemoLogin.test.ts` guards that.
The list lives in `frontend/src/lib/components/auth/DevDemoLogin.svelte`; keep
it in step with `backend/scripts/seed.py` when seeded users change.

**Acme Corp** (http://acme.localhost:7777):

| Email                      | Name            | Role       |
|----------------------------|-----------------|------------|
| `demo@acme.com`            | Alice Admin     | Admin      |
| `demo+apmanager@acme.com`  | Marcus Manager  | AP Manager |
| `demo+apclerk@acme.com`    | Clara Clerk     | AP Clerk   |
| `demo+cfo@acme.com`        | Frank CFO       | CFO        |

**TechFlow Inc** (http://techflow.localhost:7777):

| Email                   | Name           | Role     |
|-------------------------|----------------|----------|
| `admin@techflow.com`    | Tina TechAdmin | Admin    |
| `clerk@techflow.com`    | Carlos Clerk   | AP Clerk |

The seed script creates 2 organizations, 6 users, 4 roles, 10 vendors, and 10 invoices per tenant.

## What's Running Where

| Service        | URL                       |
|----------------|---------------------------|
| Frontend       | http://acme.localhost:7777 (or any tenant subdomain) |
| Backend API    | http://localhost:8000      |
| Swagger docs   | http://localhost:8000/docs |
| MinIO console  | http://localhost:9001      |
