#!/usr/bin/env bash
# Provision a tenant end-to-end on the minimal single-VM stack: tenant DB +
# org + admin user (via the same provision_tenant path as self-service
# signup), the Caddy host block, and a zero-downtime reload. With the
# recommended wildcard DNS record (*.APP_DOMAIN → this VM) no DNS work is
# needed at all.
#
# Usage: add-tenant.sh <slug> --name "Acme Corp" --admin-email a@acme.com
#                      [--admin-password <pw>]
# Without --admin-password a temp password is generated and first-login
# change is forced.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "add-tenant.sh: $*" >&2
	exit 1
}
usage() {
	die "usage: add-tenant.sh <slug> --name <company> --admin-email <email> [--admin-password <pw>]"
}

SLUG="${1:-}"
[ -n "$SLUG" ] || usage
shift
NAME="" EMAIL="" PASSWORD=""
while [ $# -gt 0 ]; do
	case "$1" in
	--name) NAME="${2:-}" && shift 2 ;;
	--admin-email) EMAIL="${2:-}" && shift 2 ;;
	--admin-password) PASSWORD="${2:-}" && shift 2 ;;
	*) usage ;;
	esac
done
{ [ -n "$NAME" ] && [ -n "$EMAIL" ]; } || usage
echo "$SLUG" | grep -Eq '^[a-z0-9](-?[a-z0-9])*$' || die "invalid slug '$SLUG' (lowercase letters, digits, single hyphens)"
[ -f .env ] || die "deploy/.env missing — run deploy.sh at least once first."
# Same compose invocation as every other deploy script (deploy/lib.sh) — the
# api it execs into is the same container in either database mode.
feoh_load_db_mode || die "could not determine the database mode from deploy/.env."

APP_DOMAIN=$(grep -E '^APP_DOMAIN=' .env | tail -1 | cut -d= -f2- || true)
[ -n "$APP_DOMAIN" ] || die "APP_DOMAIN not set in the sops env."
HOST="${SLUG}.${APP_DOMAIN}"

FORCE_CHANGE=()
if [ -z "$PASSWORD" ]; then
	# Prefix guarantees the app's complexity rules (12+ chars, upper/lower/digit).
	PASSWORD="Aa1-$(openssl rand -hex 12)"
	FORCE_CHANGE=(--force-password-change)
fi

# --skip-existing makes the whole script re-runnable: if a previous run
# provisioned the tenant but died before the Caddy step, the re-run skips
# provisioning cleanly instead of tripping the deliberate duplicate-slug
# error, and still repairs the Caddy side below.
"${COMPOSE[@]}" exec -T api python scripts/create_tenant.py \
	--name "$NAME" --slug "$SLUG" \
	--admin-email "$EMAIL" --admin-password "$PASSWORD" \
	--skip-existing "${FORCE_CHANGE[@]}"

if ! grep -q "^${HOST} {" tenants.caddy 2>/dev/null; then
	printf '\n%s {\n\timport spa\n}\n' "$HOST" >>tenants.caddy
fi
# Reload unconditionally (zero-downtime, no-op when nothing changed) — inside
# the grep guard a re-run after "block appended but reload failed" would skip
# the reload forever and the tenant host would never start serving.
"${COMPOSE[@]}" exec caddy caddy reload --config /etc/caddy/Caddyfile

echo
echo "tenant ready: https://${HOST}"
echo "  admin login: ${EMAIL}"
if [ ${#FORCE_CHANGE[@]} -gt 0 ]; then
	echo "  temp password: ${PASSWORD}  (share securely; change forced at first login)"
fi
echo "  DNS: covered by a wildcard *.${APP_DOMAIN} record; otherwise add an A record for ${HOST}."
