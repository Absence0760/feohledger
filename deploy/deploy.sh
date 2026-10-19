#!/usr/bin/env bash
# Deploy / update the minimal single-VM stack (docs/minimal-deployment.md).
# Run ON the VM from anywhere: preflight → pull main (or check out a release's
# commit, --ref) → hand over to the deploy.sh just checked out → decrypt
# secrets → build frontend (compose.prod.yml's one-shot `frontend-build` service, a
# node:24 container — no Node/pnpm needed on the VM) →
# build backend → run migrations BEFORE the new code serves traffic (control
# plane + every tenant DB — same ordering contract as the future ECS
# pipeline) → roll containers and wait for the API healthcheck → publish the
# frontend build as the live release (an atomic symlink swap under deploy/www/,
# which is what Caddy mounts) → reload Caddy → smoke-check the site and the API
# through this VM's own Caddy.
#
# Usage: deploy.sh [--no-pull | --ref <sha>] [--backend-only|--frontend-only]
#
# --ref <sha> deploys exactly that commit, which must be on origin/main — what a
# published release runs over SSM (.github/workflows/vm-deploy.yml,
# infra/deploy.tf). Without it, main's tip.
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT=$(cd .. && pwd)

# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "deploy.sh: $*" >&2
	exit 1
}

USAGE="usage: deploy.sh [--no-pull | --ref <sha>] [--backend-only|--frontend-only]"
DO_PULL=1 DO_BACKEND=1 DO_FRONTEND=1 REF=""
SCOPE=() # the build-scope flags, handed on unchanged when the script re-runs itself
while [ $# -gt 0 ]; do
	case "$1" in
	--no-pull) DO_PULL=0 ;;
	--ref)
		[ $# -ge 2 ] || die "$USAGE"
		REF=$2
		shift
		;;
	--ref=*) REF=${1#--ref=} ;;
	--backend-only)
		DO_FRONTEND=0
		SCOPE+=("$1")
		;;
	--frontend-only)
		DO_BACKEND=0
		SCOPE+=("$1")
		;;
	*) die "$USAGE" ;;
	esac
	shift
done
[ "$DO_PULL" = 1 ] || [ -z "$REF" ] ||
	die "--ref and --no-pull contradict each other: --ref checks a commit out. $USAGE"

# ── Preflight — fail with a clear message before doing any work ──────────────
for cmd in docker sops git flock curl; do
	command -v "$cmd" >/dev/null || die "'$cmd' not installed — run deploy/bootstrap-vm.sh first."
done
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing — run deploy/bootstrap-vm.sh first."
docker info >/dev/null 2>&1 || die "cannot talk to the docker daemon — is it running, and are you in the docker group? (log out/in after bootstrap-vm.sh)"
[ -f prod.sops.yaml ] || die "prod.sops.yaml missing — copy infra-secrets/feohledger/prod.sops.yaml here (template: deploy/prod.sops.yaml.example; see deploy/README.md)."

# One deploy at a time on this VM: a release deploy over SSM and a manual run
# from a Session Manager shell would otherwise build and migrate over each
# other. The lock is held by the open descriptor, so it survives the re-exec
# below; the variable tells the re-run script it already holds it.
if [ -z "${FEOH_DEPLOY_LOCK_HELD:-}" ]; then
	exec 9>.deploy.lock
	flock -n 9 || die "another deploy is already running on this VM (deploy/.deploy.lock)."
	export FEOH_DEPLOY_LOCK_HELD=1
fi

if [ "$DO_PULL" = 1 ]; then
	feoh_sync_source "$REPO_ROOT" "$REF" || die "could not bring the checkout to the commit to deploy."
	# Bash is still running the deploy.sh that was on disk before the checkout.
	# Hand over to the one just checked out, so a release that changes the
	# deploy steps is deployed BY those steps rather than its predecessor's.
	exec ./deploy.sh --no-pull ${SCOPE[@]+"${SCOPE[@]}"}
fi

# Secrets: decrypt prod.sops.yaml to .env and check it before it replaces the
# current one (decrypt-env.sh — also runnable on its own). Everything this
# script writes from here on is owner-only.
umask 077
./decrypt-env.sh

# Local Postgres container or external database (RDS)? Decided once, from the
# .env just written (deploy/lib.sh) — it picks the compose profile below.
feoh_load_db_mode || die "could not determine the database mode from deploy/.env."
echo "==> database: ${DB_MODE}"

# Per-VM tenant host list for Caddy (gitignored) — seed from the example so
# the Caddyfile's `import tenants.caddy` always resolves.
[ -f tenants.caddy ] || cp tenants.caddy.example tenants.caddy

# The SPA Caddy serves is the release `www/current` points at (deploy/lib.sh
# § The SPA Caddy serves). A backend-only deploy publishes none, so it needs one
# to exist already — on a VM moving to this layout, run a full deploy first.
WWW=www
# Created here, before any compose call, so it is this user's: a bind-mount
# source that does not exist yet is created by the Docker daemon, as root, and
# the publish step could then never write into it.
(umask 022 && mkdir -p "$WWW")
[ "$DO_FRONTEND" = 1 ] || [ -L "$WWW/current" ] ||
	die "no published frontend release under deploy/${WWW}/ — run a full ./deploy.sh (not --backend-only) first."

API_DOMAIN=$(feoh_env API_DOMAIN)
APP_DOMAIN=$(feoh_env APP_DOMAIN)

# ── Frontend ─────────────────────────────────────────────────────────────────
if [ "$DO_FRONTEND" = 1 ]; then
	# PUBLIC_API_URL is baked into the static build (declared `static` in frontend/src/env.ts).
	# PUBLIC_SITE_URL prefixes the absolute og:image URL in src/app.html (link
	# previews); SvelteKit would substitute an empty string for it silently, which
	# is why it comes from APP_DOMAIN, which decrypt-env.sh refuses empty.
	# pnpm's version is declared once, as `packageManager` in package.json
	# (frontend/CLAUDE.md § The lockfile) — the field CI's pnpm/action-setup
	# reads, so this builds with the pnpm that wrote the lockfile. Read after the
	# pull so a bump lands on the next deploy. `npm i -g` rejects corepack's
	# `+sha512.<hash>` integrity suffix, so the pattern stops before it.
	PNPM_SPEC=$(sed -nE 's/^[[:space:]]*"packageManager":[[:space:]]*"(pnpm@[^"+]+).*/\1/p' "$REPO_ROOT/frontend/package.json")
	[ -n "$PNPM_SPEC" ] || die "frontend/package.json declares no pnpm packageManager, so there is no pnpm version to build with."
	echo "==> building frontend (${PNPM_SPEC}, PUBLIC_API_URL=https://${API_DOMAIN}, PUBLIC_SITE_URL=https://${APP_DOMAIN})"
	# The container, its pinned Node image, the repo mount and the pnpm-store
	# cache are the `frontend-build` service in compose.prod.yml, where the
	# docker-compose Dependabot entry keeps the image current — nothing here
	# restates an image ref (docs/decisions.md §203). Naming the service
	# activates its `build` profile.
	"${COMPOSE[@]}" run --rm -T \
		-e PNPM_SPEC="$PNPM_SPEC" \
		-e PUBLIC_API_URL="https://${API_DOMAIN}" \
		-e PUBLIC_SITE_URL="https://${APP_DOMAIN}" \
		frontend-build
	# The build lands in frontend/build, which nothing serves — it is the
	# staging copy. It goes live as a release after the roll below, so a failed
	# migration or an API that never comes healthy leaves the old SPA in front
	# of the old API. Unique per run (the lock above serialises deploys).
	RELEASE_ID="$(date -u +%Y%m%dT%H%M%SZ)-$(git -C "$REPO_ROOT" rev-parse --short=12 HEAD)"
	publish_frontend() {
		# The site is public static files: world-readable, unlike the secrets
		# this script's umask exists for.
		(umask 022 && feoh_publish_frontend "$REPO_ROOT/frontend/build" "$WWW" "$RELEASE_ID") ||
			die "could not publish the frontend build as release ${RELEASE_ID}."
	}
	# A VM with no release yet (a first deploy, or the first on this layout):
	# no container serves www/ before the roll recreates Caddy onto it, so
	# publish now and the new Caddy starts with a site instead of a 404 window.
	if ! [ -L "$WWW/current" ]; then
		publish_frontend
		PUBLISHED=1
	fi
fi

# ── Backend ──────────────────────────────────────────────────────────────────
if [ "$DO_BACKEND" = 1 ]; then
	echo "==> building backend image"
	"${COMPOSE[@]}" build api
	# Local mode starts the Postgres container here; with an external database
	# there is none to start — the migration below is the first thing to reach it.
	"${COMPOSE[@]}" up -d "${DB_SERVICES[@]}" redis
	echo "==> running migrations (control plane + every tenant DB)"
	# init_control_plane.py builds an EMPTY control plane first (tables, system
	# roles, Alembic version row — one transaction) and is a no-op on an initialised one — Alembic's chain
	# has no step that creates it (app/services/control_plane_bootstrap.py).
	"${COMPOSE[@]}" run --rm api sh -c \
		"python scripts/init_control_plane.py && alembic upgrade head && python scripts/migrate_all_tenants.py"
fi

# ── Roll + verify ────────────────────────────────────────────────────────────
echo "==> rolling containers"
"${COMPOSE[@]}" up -d --wait --wait-timeout 300

if [ "$DO_FRONTEND" = 1 ] && [ "${PUBLISHED:-0}" = 0 ]; then
	publish_frontend
fi

# Pick up Caddyfile / tenants.caddy edits — a reload, or a recreate when the
# container's single-file mounts are stale (lib.sh). A failure here is a real
# config error — do not suppress it.
feoh_caddy_reload || die "caddy did not take the new config — see the error above; the site may be serving the previous config."

# Prove the site actually serves, from outside the containers: the API's own
# healthcheck stays green through a Caddy that serves nothing (#579).
if ! feoh_smoke_check "$APP_DOMAIN" / || ! feoh_smoke_check "$API_DOMAIN" /api/health; then
	die "post-deploy smoke check failed — the stack is up but not serving. See docs/troubleshooting.md § Every page 404s while the API is healthy; to roll the SPA back, deploy/README.md § Rolling the frontend back."
fi

# Reclaim disk: every deploy leaves the previous api image dangling, and on
# a 30 GB volume months of deploys pile up until Postgres runs out of space.
# Dangling-only — tagged images and volumes are untouched. The BuildKit
# cache is capped rather than purged so rebuilds stay fast.
docker image prune -f >/dev/null ||
	echo "WARN: dangling-image prune failed (non-fatal — the deploy itself succeeded)" >&2
docker builder prune -f --keep-storage 5g >/dev/null ||
	echo "WARN: builder-cache prune failed (non-fatal; check 'docker builder prune' flags)" >&2

# Switching a VM from the local container to RDS leaves the old container
# running: `up` never stops a service whose profile is no longer active. Say so
# rather than stop it — until the RDS cut-over is verified it is the fallback,
# and its volume is the data you migrated from.
if [ "$DB_MODE" = external ] &&
	[ -n "$(docker ps -q --filter label=com.docker.compose.project=feoh-prod --filter label=com.docker.compose.service=postgres)" ]; then
	echo "NOTE: the local postgres container is still running, but the API now uses the external database. Once the cut-over is verified: ./compose.sh --profile localdb stop postgres" >&2
fi

echo "deploy complete — API healthcheck and smoke check passed."
