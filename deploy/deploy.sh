#!/usr/bin/env bash
# Deploy / update the minimal single-VM stack (docs/minimal-deployment.md).
# Run ON the VM from anywhere: preflight → pull main → decrypt secrets →
# build frontend (in a node:24 container — no Node/pnpm needed on the VM) →
# build backend → run migrations BEFORE the new code serves traffic (control
# plane + every tenant DB — same ordering contract as the future ECS
# pipeline) → roll containers and wait for the API healthcheck.
#
# Usage: deploy.sh [--no-pull] [--backend-only|--frontend-only]
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT=$(cd .. && pwd)

COMPOSE=(docker compose -f compose.prod.yml)
# Node matches CI's setup-node (24). pnpm is deliberately not pinned here — the
# frontend build reads it from package.json (below). The named volume caches the
# pnpm store across deploys so rebuilds don't re-download the world.
NODE_IMAGE=node:24-alpine

die() {
	echo "deploy.sh: $*" >&2
	exit 1
}

DO_PULL=1 DO_BACKEND=1 DO_FRONTEND=1
for arg in "$@"; do
	case "$arg" in
	--no-pull) DO_PULL=0 ;;
	--backend-only) DO_FRONTEND=0 ;;
	--frontend-only) DO_BACKEND=0 ;;
	*) die "usage: deploy.sh [--no-pull] [--backend-only|--frontend-only]" ;;
	esac
done

# ── Preflight — fail with a clear message before doing any work ──────────────
for cmd in docker sops git; do
	command -v "$cmd" >/dev/null || die "'$cmd' not installed — run deploy/bootstrap-vm.sh first."
done
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing — run deploy/bootstrap-vm.sh first."
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon — is it running, and are you in the docker group? (log out/in after bootstrap-vm.sh)"
[ -f prod.sops.yaml ] || die "prod.sops.yaml missing — copy infra-secrets/feohledger/prod.sops.yaml here (template: deploy/prod.sops.yaml.example; see deploy/README.md)."

if [ "$DO_PULL" = 1 ]; then
	git -C "$REPO_ROOT" pull --ff-only
fi

# Secrets: decrypt prod.sops.yaml to .env and check it before it replaces the
# current one (decrypt-env.sh — also runnable on its own). Everything this
# script writes from here on is owner-only.
umask 077
./decrypt-env.sh

# Per-VM tenant host list for Caddy (gitignored) — seed from the example so
# the Caddyfile's `import tenants.caddy` always resolves.
[ -f tenants.caddy ] || cp tenants.caddy.example tenants.caddy

# ── Frontend ─────────────────────────────────────────────────────────────────
if [ "$DO_FRONTEND" = 1 ]; then
	# PUBLIC_API_URL is baked into the static build ($env/static/public).
	# PUBLIC_SITE_URL prefixes the absolute og:image URL in src/app.html (link
	# previews); SvelteKit would substitute an empty string for it silently, which
	# is why it comes from APP_DOMAIN, which decrypt-env.sh refuses empty.
	API_DOMAIN=$(grep -E '^API_DOMAIN=' .env | tail -1 | cut -d= -f2- || true)
	APP_DOMAIN=$(grep -E '^APP_DOMAIN=' .env | tail -1 | cut -d= -f2- || true)
	# pnpm's version is declared once, as `packageManager` in package.json
	# (frontend/CLAUDE.md § The lockfile) — the field CI's pnpm/action-setup
	# reads, so this builds with the pnpm that wrote the lockfile. Read after the
	# pull so a bump lands on the next deploy. `npm i -g` rejects corepack's
	# `+sha512.<hash>` integrity suffix, so the pattern stops before it.
	PNPM_SPEC=$(sed -nE 's/^[[:space:]]*"packageManager":[[:space:]]*"(pnpm@[^"+]+).*/\1/p' "$REPO_ROOT/frontend/package.json")
	[ -n "$PNPM_SPEC" ] || die "frontend/package.json declares no pnpm packageManager, so there is no pnpm version to build with."
	echo "==> building frontend (${PNPM_SPEC}, PUBLIC_API_URL=https://${API_DOMAIN}, PUBLIC_SITE_URL=https://${APP_DOMAIN})"
	docker run --rm \
		-v "$REPO_ROOT":/repo -w /repo/frontend \
		-v feoh-prod-pnpm-store:/pnpm-store \
		-e npm_config_store_dir=/pnpm-store \
		-e PUBLIC_API_URL="https://${API_DOMAIN}" \
		-e PUBLIC_SITE_URL="https://${APP_DOMAIN}" \
		"$NODE_IMAGE" sh -ec "npm i -g ${PNPM_SPEC} >/dev/null 2>&1 && pnpm install --frozen-lockfile && pnpm build"
fi

# ── Backend ──────────────────────────────────────────────────────────────────
if [ "$DO_BACKEND" = 1 ]; then
	echo "==> building backend image"
	"${COMPOSE[@]}" build api
	"${COMPOSE[@]}" up -d postgres redis
	echo "==> running migrations (control plane + every tenant DB)"
	"${COMPOSE[@]}" run --rm api sh -c \
		"alembic upgrade head && python scripts/migrate_all_tenants.py"
fi

# ── Roll + verify ────────────────────────────────────────────────────────────
echo "==> rolling containers"
"${COMPOSE[@]}" up -d --wait --wait-timeout 300

# Pick up Caddyfile / tenants.caddy edits without a container restart. A
# failure here is a real config error — do not suppress it.
"${COMPOSE[@]}" exec caddy caddy reload --config /etc/caddy/Caddyfile

# Reclaim disk: every deploy leaves the previous api image dangling, and on
# a 30 GB volume months of deploys pile up until Postgres runs out of space.
# Dangling-only — tagged images and volumes are untouched. The BuildKit
# cache is capped rather than purged so rebuilds stay fast.
docker image prune -f >/dev/null ||
	echo "WARN: dangling-image prune failed (non-fatal — the deploy itself succeeded)" >&2
docker builder prune -f --keep-storage 5g >/dev/null ||
	echo "WARN: builder-cache prune failed (non-fatal; check 'docker builder prune' flags)" >&2

echo "deploy complete — API healthcheck passed."
