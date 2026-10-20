# Troubleshooting

## `database "feohledger" does not exist`

A local PostgreSQL (e.g. from Homebrew) is likely running on port 5432, intercepting the connection before the Docker container.

**Fix:** Stop the local Postgres:

```bash
brew services stop postgresql@17   # adjust version as needed
```

Then re-run `seed.py` or restart the backend.

## Deploy fails: `relation "organizations" does not exist`, or `init_control_plane: … alembic_version … no organizations`

The control-plane database was migrated by Alembic while it was empty. Alembic's
chain has no revision that creates the control plane, so it stamped head over
nothing (`docs/decisions.md` §274). `deploy.sh` now runs
`scripts/init_control_plane.py` first, which builds an empty control plane
correctly — but a database already stamped that way is refused rather than
guessed at. It cannot hold any data, so recreate it and deploy again, from
`deploy/` on the VM:

```bash
./psql.sh -d postgres -c 'DROP DATABASE feohledger' -c 'CREATE DATABASE feohledger'
./deploy.sh
```

## Every page 404s while the API is healthy (production VM)

**Symptom.** The apex, every tenant host and `/legal/*` answer **404 with an
empty body**; `https://<API_DOMAIN>/api/health` is fine. A deploy may have
failed at the Caddy step with:

```
OCI runtime exec failed: exec failed: unable to start container process: current working directory is outside of container mount namespace root -- possible container breakout detected: unknown
```

**Cause.** Caddy is serving a directory that no longer exists on the host. A bind
mount pins the directory's inode: when the host directory is deleted and
recreated — as the SvelteKit build does to `frontend/build` — the container keeps
the orphaned, emptied original, so every file is missing, and runc refuses
`docker exec` because the container's working directory (`/srv`) is outside the
mount namespace. This was the v0.1.0 outage (#579), when Caddy mounted
`frontend/build` directly; Caddy now mounts `deploy/www/`, which is never
replaced, and serves the release its `current` symlink names
(`deploy/README.md` § How the SPA is served, `docs/decisions.md` §277). Seeing it
again means something replaced `deploy/www/` itself or Caddy is back on a
directory that gets rebuilt.

**Recovery** (on the VM; starting the container again re-resolves its bind
mounts by path):

```bash
docker restart feoh-prod-caddy-1
```

If `www/current` is missing or points at a bad build, roll the SPA back from
`deploy/` first: `ln -sfn "$(readlink www/previous)" www/current.new && mv -T www/current.new www/current`.

**A related, quieter case:** a Caddyfile or `tenants.caddy` change that "reloads
fine" but changes nothing — a single-file mount keeps a file replaced by rename
at its old inode. The deploy scripts detect it and recreate Caddy
(`feoh_caddy_reload`); by hand, `./compose.sh up -d --no-deps --force-recreate caddy`.

## `bcrypt` errors

```
AttributeError: module 'bcrypt' has no attribute '__about__'
```

That is `passlib` failing against bcrypt 5.x, and **this project no longer uses
passlib** — `backend/app/utils/passwords.py` implements `bcrypt_sha256` directly
(`docs/decisions.md` §151). If you see it, something is still importing passlib:
a stale virtualenv holding the old dependency set, or a local edit that
reintroduced it. Reinstall the venv rather than pinning bcrypt back down:

```bash
cd backend && rm -rf .venv && python3 -m venv .venv && source .venv/bin/activate && pip install -e ".[dev]"
```

```
ValueError: password cannot be longer than 72 bytes, truncate manually if necessary
```

bcrypt 4.1+ refuses a long secret where 4.0 silently truncated. Nothing in the
app hands bcrypt a raw password (the `bcrypt_sha256` pre-hash is always 44
bytes), so this means a new call site is using `bcrypt.hashpw` / `bcrypt.checkpw`
directly — which is also the 72-byte truncation bug the scheme exists to avoid.
Route it through `hash_password` / `verify_password` instead.

## Frontend shows "No tenant found"

You're accessing the app at `localhost:7777` without a subdomain. The app requires a tenant subdomain.

**Fix:** Use a tenant URL:
- http://acme.localhost:7777
- http://techflow.localhost:7777

## Subdomain not working in Safari

Safari may not resolve `*.localhost` to `127.0.0.1` automatically.

**Fix:** Add entries to `/etc/hosts`:

```
127.0.0.1 acme.localhost techflow.localhost
```

Chrome, Firefox, and Edge handle `*.localhost` natively.

## `Missing X-Tenant-Slug header` (400 error)

The backend requires an `X-Tenant-Slug` header on business endpoints (invoices, vendors, dashboard). This is sent automatically by the frontend when accessed via a subdomain.

If testing via curl, include the header:

```bash
curl http://localhost:8000/api/invoices \
  -H "Authorization: Bearer $TOKEN" \
  -H "X-Tenant-Slug: acme"
```

## `Unknown tenant: <slug>` (404 error)

The tenant slug doesn't match any organization in the control-plane database.

**Fix:** Either:
- Use a valid slug (`acme`, `techflow`)
- Provision a new tenant: `python scripts/create_tenant.py --name "Name" --slug yourslug --admin-email you@example.com --admin-password changeme`

## Frontend can't connect to backend

- Check that the backend is running on port 8000: `curl http://localhost:8000/api/health`
- Verify `PUBLIC_API_URL` in `frontend/.env` is set to `http://localhost:8000`
- CORS is regex-based and should accept any `*.localhost` subdomain automatically

## Docker services won't start

```bash
# Check what's running
docker compose ps

# View logs for errors
docker compose logs -f

# Full reset (removes all data)
docker compose down -v
docker compose up -d
```

## MinIO bucket doesn't exist

If file uploads fail because the `invoices` bucket hasn't been created:

1. Open the MinIO console at http://localhost:9001
2. Login with `minioadmin` / `minioadmin`
3. Go to Buckets → Create Bucket → name it `invoices`

Or via CLI:

```bash
mc alias set local http://localhost:9000 minioadmin minioadmin
mc mb local/invoices
```

## Seed script fails

Make sure Docker services are running first:

```bash
cd backend
docker compose up -d
python scripts/seed.py
```

If you see connection errors, wait a few seconds for PostgreSQL to finish starting, then retry.

The seed script creates tenant databases (`feoh_acme`, `feoh_techflow`) automatically if they don't exist. If Docker was reset with `down -v`, the init SQL re-creates them on startup as well.

## A QuickBooks Online tenant still posts on its pre-0115 links (local dev)

Since decisions §284, migration `0115_erp_link_realm` never writes
`settings.erp.legacy_link_realm` for a QuickBooks Online org, so its pre-0115
links are refused until the first chart and vendor sync re-judge them. An
earlier version of 0115 on the `fix/erp-qbo-live-test` branch did write it, and
a re-run never removes a key already present. A local database that ran that
version keeps trusting those links. Either downgrade to `0114_gl_code_width`
and upgrade again (this drops every link's realm stamp too, so re-sync after),
or delete the key:

```sql
UPDATE organizations SET settings = settings #- '{erp,legacy_link_realm}'
WHERE settings->'erp'->>'type' = 'quickbooks_online'
  AND coalesce(settings->'erp'->>'integration_method', 'merge_dev') <> 'merge_dev';
```
