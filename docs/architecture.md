# Architecture

## System Overview

```
acme.localhost:7777 ──┐                        ┌── feoh_acme DB
                      │                        │   (invoices, vendors, ...)
                      ├── Backend API :8000 ────┼── feoh_techflow DB
                      │   (shared FastAPI)      │
techflow.localhost:7777┘                       └── feohledger DB
                                                    (control plane: orgs, users, roles)
                              │
                       ┌──────┴───────┐       ┌─────────────────┐
                       │   Redis 7    │       │   MinIO (S3)    │
                       │   :6379      │       │   :9000/:9001   │
                       └──────────────┘       └─────────────────┘
```

## Tech Stack

| Layer          | Tech                                    |
|----------------|-----------------------------------------|
| Frontend       | SvelteKit 3, Svelte 5, TypeScript       |
| Backend        | FastAPI, SQLAlchemy 2 (async), Pydantic |
| Database       | PostgreSQL 16                           |
| Cache/Queue    | Redis 7                                 |
| Object Storage | MinIO (S3-compatible)                   |
| Auth           | JWT (python-jose) + bcrypt_sha256       |
| Migrations     | Alembic                                 |
| Package Mgrs   | pnpm (frontend), pip (backend)          |

## Multi-Tenancy

The app uses **subdomain-based routing** with **database-per-tenant isolation**:

- Each tenant gets a unique subdomain (e.g., `acme.app.com`, `techflow.app.com`)
- Each tenant gets their own PostgreSQL database (e.g., `feoh_acme`, `feoh_techflow`)
- A shared **control-plane DB** (`feohledger`) stores the tenant registry, users, and roles
- The frontend extracts the subdomain and sends an `X-Tenant-Slug` header on every API request
- The backend resolves the slug to the correct tenant database via `app/tenant.py`

See [multi-tenancy.md](multi-tenancy.md) for full details.

## Data Flow

1. User visits `acme.localhost:7777` — frontend extracts subdomain `acme`
2. User signs in at `/login` — frontend POSTs credentials to `/api/auth/login`
3. Backend validates against the control-plane DB and returns a JWT token
4. Token is stored in `localStorage` and attached as a `Bearer` header on all requests
5. Every request also includes `X-Tenant-Slug: acme` header
6. Backend resolves `acme` → `feoh_acme` database and routes the query there
7. On 401 responses, the token is cleared and the user is redirected to `/login`

All API calls go through `src/lib/api.ts` which handles auth headers, tenant headers, error responses, and token lifecycle.

## Project Structure

```
feohledger/
├── frontend/                  # SvelteKit SPA
│   ├── src/
│   │   ├── lib/
│   │   │   ├── api.ts         # API client (auth + tenant headers)
│   │   │   ├── tenant.ts      # Subdomain extraction
│   │   │   ├── components/    # Svelte components
│   │   │   ├── stores/        # Svelte 5 rune stores
│   │   │   └── types/         # TypeScript interfaces
│   │   └── routes/            # SvelteKit file-based routing
│   └── .env.development       # committed local-dev defaults (Vite loads in dev)
├── backend/                   # FastAPI API
│   ├── app/
│   │   ├── api/               # Route handlers
│   │   ├── models/            # SQLAlchemy ORM models
│   │   ├── schemas/           # Pydantic request/response models
│   │   ├── config.py          # Settings (pydantic-settings)
│   │   ├── database.py        # Control + tenant engine management
│   │   ├── tenant.py          # Tenant resolution (slug → DB session)
│   │   └── main.py            # FastAPI app entrypoint
│   ├── alembic/               # Database migrations
│   ├── scripts/
│   │   ├── seed.py            # Multi-tenant seeder
│   │   ├── create_tenant.py   # Provision new tenants
│   │   └── migrate_all_tenants.py  # Run migrations across all tenant DBs
│   ├── init-tenants.sql       # Docker init script for dev tenant DBs
│   └── docker-compose.yml     # Postgres, Redis, MinIO
├── docs/                      # Documentation
└── README.md
```
