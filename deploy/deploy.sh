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
# Matches CI (ci.yml pins pnpm 9 on Node 20). The named volume caches the
# pnpm store across deploys so rebuilds don't re-download the world.
NODE_IMAGE=node:24-alpine
PNPM_SPEC=pnpm@9

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
[ -f .env.sops ] || die ".env.sops missing — author it from deploy/env.example, encrypt into the infra-secrets repo, and copy it here (see deploy/README.md)."

if [ "$DO_PULL" = 1 ]; then
	git -C "$REPO_ROOT" pull --ff-only
fi

# Secrets: decrypt fresh on every deploy (KMS access via the instance
# profile). Both files are gitignored. Decrypt to a temp file and move into
# place atomically — `sops -d >.env` would truncate .env before decrypting,
# so a KMS outage / bad file would leave an empty .env that breaks the
# nightly backup.sh and add-tenant.sh until the next successful deploy.
umask 077
trap 'rm -f .env.tmp' EXIT
sops -d .env.sops >.env.tmp
mv .env.tmp .env

# Everything compose interpolation / the app cannot default sensibly, plus
# the vars the app hard-refuses to boot without in a deployed env
# (FEOH_ENVIRONMENT=production arms those boot checks; FEOH_HCAPTCHA_SECRET
# is one of them) and the two S3 buckets uploads/backups silently need.
MISSING=""
for var in POSTGRES_PASSWORD APP_DOMAIN API_DOMAIN ACME_EMAIL AWS_REGION \
	FEOH_SECRET_KEY FEOH_ENVIRONMENT FEOH_S3_BUCKET BACKUP_S3_BUCKET FEOH_HCAPTCHA_SECRET; do
	grep -Eq "^${var}=.+" .env || MISSING="$MISSING $var"
done
[ -z "$MISSING" ] || die "required var(s) missing/empty in the sops env:$MISSING (contract: deploy/env.example)"

# The app refuses to boot on a weak JWT key (config.py
# _require_real_secret_key_in_deployed_envs: not the default, >= 32 chars).
# The presence loop above passes any non-empty value, so a short key would
# survive preflight and only surface ~5 minutes later as an `up -d --wait`
# healthcheck timeout, after the frontend build, image build, and migrations
# have all run. Mirror the boot rule here so it fails in the first second.
SECRET_KEY_VALUE=$(grep -E '^FEOH_SECRET_KEY=' .env | tail -1 | cut -d= -f2- || true)
case "$SECRET_KEY_VALUE" in
change-me-in-production)
	die "FEOH_SECRET_KEY is still the public default — generate one with 'openssl rand -hex 32'." ;;
esac
[ "${#SECRET_KEY_VALUE}" -ge 32 ] ||
	die "FEOH_SECRET_KEY is ${#SECRET_KEY_VALUE} chars; the app refuses to boot below 32 (openssl rand -hex 32)."

# Invoice extraction is the one adapter with no mock fallback in a deployed env
# (services/extraction.py resolve_platform_provider): with neither var set,
# every upload's extraction fails. Keying invoices in by hand is a legitimate
# choice, so this warns rather than refuses — deploy/env.example § Invoice
# extraction.
if ! grep -Eq '^(FEOH_ANTHROPIC_API_KEY|FEOH_EXTRACTION_PROVIDER)=.+' .env; then
	echo "WARN: neither FEOH_ANTHROPIC_API_KEY nor FEOH_EXTRACTION_PROVIDER is set — every invoice upload's extraction will fail (manual entry only). See deploy/env.example § Invoice extraction." >&2
fi

# Per-VM tenant host list for Caddy (gitignored) — seed from the example so
# the Caddyfile's `import tenants.caddy` always resolves.
[ -f tenants.caddy ] || cp tenants.caddy.example tenants.caddy

# ── Frontend ─────────────────────────────────────────────────────────────────
if [ "$DO_FRONTEND" = 1 ]; then
	# PUBLIC_API_URL is baked into the static build ($env/static/public).
	API_DOMAIN=$(grep -E '^API_DOMAIN=' .env | tail -1 | cut -d= -f2- || true)
	echo "==> building frontend (PUBLIC_API_URL=https://${API_DOMAIN})"
	docker run --rm \
		-v "$REPO_ROOT":/repo -w /repo/frontend \
		-v feoh-prod-pnpm-store:/pnpm-store \
		-e npm_config_store_dir=/pnpm-store \
		-e PUBLIC_API_URL="https://${API_DOMAIN}" \
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
