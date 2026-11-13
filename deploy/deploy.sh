#!/usr/bin/env bash
# Deploy / update the minimal single-VM stack (docs/minimal-deployment.md).
# Run ON the VM from anywhere: preflight → pull main (or check out a release's
# commit, --ref) → hand over to the deploy.sh just checked out → decrypt
# secrets → build the backend → run migrations BEFORE the new code serves
# traffic (control plane + every tenant DB — same ordering contract as the
# future ECS pipeline) → roll containers and wait for the API healthcheck →
# reload Caddy → smoke-check the API through this VM's own Caddy.
#
# The VM builds and serves no SPA (docs/decisions.md §364). The release
# workflow builds it in CI and publishes it to S3 + CloudFront after this
# script succeeds (.github/workflows/vm-deploy.yml, deploy/publish-frontend.sh).
#
# Usage: deploy.sh [--no-pull | --ref <sha>] [--backend-only]
#
# --ref <sha> deploys exactly that commit, which must be on origin/main — what a
# published release runs over SSM (.github/workflows/vm-deploy.yml,
# infra/deploy.tf). Without it, main's tip. --backend-only is what every deploy
# is now; it is still accepted, and handed on when the script re-runs itself,
# so a rollback to an older commit keeps the meaning it had there.
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT=$(cd .. && pwd)

# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "deploy.sh: $*" >&2
	exit 1
}

USAGE="usage: deploy.sh [--no-pull | --ref <sha>] [--backend-only]"
DO_PULL=1 REF=""
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
	--backend-only) SCOPE+=("$1") ;;
	--frontend-only)
		die "--frontend-only: the VM no longer builds or serves the SPA (docs/decisions.md §364). It ships to S3 + CloudFront from the release workflow; to publish one by hand, see deploy/README.md § The SPA on S3 + CloudFront."
		;;
	*) die "$USAGE" ;;
	esac
	shift
done
[ "$DO_PULL" = 1 ] || [ -z "$REF" ] ||
	die "--ref and --no-pull contradict each other: --ref checks a commit out. $USAGE"

# ── Preflight — fail with a clear message before doing any work ──────────────
for cmd in docker sops git flock curl python3; do
	command -v "$cmd" >/dev/null || die "'$cmd' not installed — run deploy/bootstrap-vm.sh first."
done
docker compose version >/dev/null 2>&1 || die "docker compose plugin missing — run deploy/bootstrap-vm.sh first."
# CI renders compose.prod.yml with the compose bootstrap-vm.sh pins (#575), so
# any other version runs this deploy untested — #569 was a warning only the
# VM's compose printed. Said loudly, not refused: re-running bootstrap is the
# fix, and it should not have to wait for a deploy that is already underway.
COMPOSE_PIN=$(feoh_compose_pin bootstrap-vm.sh) || die "bootstrap-vm.sh has no usable COMPOSE_VERSION pin."
COMPOSE_RUNNING="v$(docker compose version --short 2>/dev/null | sed 's/^v//')"
[ "$COMPOSE_RUNNING" = "$COMPOSE_PIN" ] ||
	echo "WARN: docker compose here is ${COMPOSE_RUNNING}, but bootstrap-vm.sh pins ${COMPOSE_PIN} — the version CI checks deploy/ with. Re-run ./bootstrap-vm.sh to converge." >&2
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

# Per-VM vanity host list for Caddy (gitignored) — seed from the example so
# the Caddyfile's `import tenants.caddy` always resolves. Then take out any
# platform host block an older add-tenant.sh wrote: CloudFront serves those
# names, and their `import spa` names a snippet the Caddyfile no longer has, so
# the Caddy the roll below recreates would refuse the whole config (lib.sh).
[ -f tenants.caddy ] || cp tenants.caddy.example tenants.caddy
APP_DOMAIN=$(feoh_env APP_DOMAIN)
feoh_prune_platform_hosts "$APP_DOMAIN" || die "could not prune the platform hosts from deploy/tenants.caddy."

# ── Backend ──────────────────────────────────────────────────────────────────
echo "==> building backend image"
"${COMPOSE[@]}" build api
# Local mode starts the Postgres container here; with an external database
# there is none to start — the migration below is the first thing to reach it.
"${COMPOSE[@]}" up -d "${DB_SERVICES[@]}" redis
echo "==> running migrations (control plane + every tenant DB)"
# init_control_plane.py, then alembic upgrade head, then
# migrate_all_tenants.py — the sequence lives in that one file, which CI's
# day-one job also runs against an empty database (#575), so the rehearsal
# cannot drift from this step.
"${COMPOSE[@]}" run --rm api sh scripts/deploy_migrate.sh

# ── Roll + verify ────────────────────────────────────────────────────────────
echo "==> rolling containers"
"${COMPOSE[@]}" up -d --wait --wait-timeout 300

# Pick up Caddyfile / tenants.caddy edits — a reload, or a recreate when the
# container's single-file mounts are stale (lib.sh). A failure here is a real
# config error — do not suppress it.
feoh_caddy_reload || die "caddy did not take the new config — see the error above; the site may be serving the previous config."

# Reclaim disk: every deploy leaves the previous api image dangling, and on
# a 30 GB volume months of deploys pile up until Postgres runs out of space.
# Dangling-only — tagged images and volumes are untouched. The BuildKit
# cache is capped rather than purged so rebuilds stay fast.
docker image prune -f >/dev/null ||
	echo "WARN: dangling-image prune failed (non-fatal — the deploy carries on)" >&2
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

# What the VM served before §364, and nothing reads now. A rollback to an older
# commit builds and publishes its own release, so none of it is needed; it is
# left for the operator to delete rather than removed by a deploy.
if [ -e www ]; then
	echo "NOTE: deploy/www/ still holds SPA releases from before the VM stopped serving the SPA (docs/decisions.md §364). Once this deploy is verified: rm -rf $(pwd)/www" >&2
fi
if docker volume inspect feoh-prod_pnpm-store >/dev/null 2>&1; then
	echo "NOTE: the frontend build's pnpm cache volume is unused since the VM stopped building the SPA. Drop it once: docker volume rm feoh-prod_pnpm-store" >&2
fi

# Prove the stack actually serves, from outside the containers: the API's own
# healthcheck stays green through a Caddy that serves nothing (#579). smoke.sh
# checks api.<domain>/api/health, that /api/public-config matches deploy/.env
# (#575) and, when the VM serves one, a vanity host's TLS — the same script the
# ECS release workflow ends with. The SPA's own check runs after the publish,
# against CloudFront (deploy/smoke-frontend.sh).
./smoke.sh --vm ||
	die "post-deploy smoke check failed (above) — the stack is up but not serving as configured."

echo "deploy complete — API healthcheck and smoke check passed."
