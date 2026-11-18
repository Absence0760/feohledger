#!/usr/bin/env bash
# Provision a tenant on the minimal single-VM stack: tenant DB + org + admin
# user, via the same provision_tenant path as self-service signup.
#
# Nothing on the web side: since docs/decisions.md §364 the VM serves only the
# API, and <slug>.<APP_DOMAIN> is answered by CloudFront's wildcard alias and
# certificate (infra/frontend.tf), so a new tenant needs no Caddy block, no
# reload and no DNS record. (A customer's own vanity domain is the exception,
# added by hand: docs/founder-runbooks/custom-domain-provisioning.md.)
#
# Usage: add-tenant.sh <slug> --name "Acme Corp" --admin-email a@acme.com
# The admin is created with no password and emailed a single-use set-password
# link; the script prints only the address it went to, never the link (this
# often runs through SSM, whose output is retained). If the email fails, the
# provisioning step exits 3: the tenant exists, and once mail works the admin
# sets a password with Forgot password on the sign-in page, or the operator
# re-sends the link:
#
#   add-tenant.sh <slug> --admin-email a@acme.com --resend-link
#
# which emails that admin a fresh link (every earlier one stops working),
# prints only a masked address, and is refused if the tenant or admin does not
# exist or the admin has already set a password.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "add-tenant.sh: $*" >&2
	exit 1
}
usage() {
	die "usage: add-tenant.sh <slug> --name <company> --admin-email <email>
       add-tenant.sh <slug> --admin-email <email> --resend-link"
}

SLUG="${1:-}"
[ -n "$SLUG" ] || usage
shift
NAME="" EMAIL="" RESEND=""
while [ $# -gt 0 ]; do
	case "$1" in
	--name) NAME="${2:-}" && shift 2 ;;
	--admin-email) EMAIL="${2:-}" && shift 2 ;;
	--resend-link) RESEND=1 && shift ;;
	*) usage ;;
	esac
done
[ -n "$EMAIL" ] || usage
if [ -n "$RESEND" ]; then
	[ -z "$NAME" ] || usage
else
	[ -n "$NAME" ] || usage
fi
echo "$SLUG" | grep -Eq '^[a-z0-9](-?[a-z0-9])*$' || die "invalid slug '$SLUG' (lowercase letters, digits, single hyphens)"
[ -f .env ] || die "deploy/.env missing — run deploy.sh at least once first."
# Same compose invocation as every other deploy script (deploy/lib.sh) — the
# api it execs into is the same container in either database mode.
feoh_load_db_mode || die "could not determine the database mode from deploy/.env."

APP_DOMAIN=$(grep -E '^APP_DOMAIN=' .env | tail -1 | cut -d= -f2- || true)
[ -n "$APP_DOMAIN" ] || die "APP_DOMAIN not set in the sops env."
HOST="${SLUG}.${APP_DOMAIN}"

if [ -n "$RESEND" ]; then
	feoh_tenant_resend_argv "$SLUG" "$EMAIL"
	"${COMPOSE[@]}" exec -T api "${FEOH_TENANT_ARGV[@]}"
	exit 0
fi

# The provisioning command itself is built by deploy/lib.sh, which CI's day-one
# job runs too (#575): no password, a set-password link by email only, and
# --skip-existing so a re-run of a tenant that already
# exists is a no-op rather than the deliberate duplicate-slug error.
feoh_tenant_argv "$SLUG" "$NAME" "$EMAIL"
"${COMPOSE[@]}" exec -T api "${FEOH_TENANT_ARGV[@]}"

echo
echo "tenant ready: https://${HOST}"
echo "  admin login: ${EMAIL}  (no password until they use the set-password link)"
echo "  web: served by CloudFront under the *.${APP_DOMAIN} wildcard; nothing to add on this VM or in DNS."
