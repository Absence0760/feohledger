#!/usr/bin/env bash
# Remove a tenant end-to-end from the minimal single-VM stack: stop serving its
# host, then destroy its documents, database and control-plane records, then
# delete its backup objects.
#
# This is the deletion `/legal/dpa` § 13 and clause 10 of the Terms promise a
# customer within 60 days of termination. It is the inverse of add-tenant.sh and
# the most destructive script in the repo — nothing it does can be undone from
# the application.
#
# Order matters and is not the obvious one:
#
#   1. Caddy host block + reload  — stop serving before anything is destroyed,
#      so nobody meets a half-deleted tenant.
#   2. scripts/delete_tenant.py   — documents, then the tenant DB, then the
#      control-plane rows (see that module for why THAT order).
#   3. Backup objects             — last, because until the live data is gone
#      the backups are still the thing you would restore from if a step failed.
#
# Usage: remove-tenant.sh <slug> [--dry-run] [--yes]
set -euo pipefail
cd "$(dirname "$0")"

COMPOSE=(docker compose -f compose.prod.yml)

die() {
	echo "remove-tenant.sh: $*" >&2
	exit 1
}
usage() {
	die "usage: remove-tenant.sh <slug> [--dry-run] [--yes]"
}

SLUG="${1:-}"
[ -n "$SLUG" ] || usage
shift
DRY_RUN=0 ASSUME_YES=0
while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run) DRY_RUN=1 && shift ;;
	--yes) ASSUME_YES=1 && shift ;;
	*) usage ;;
	esac
done
echo "$SLUG" | grep -Eq '^[a-z0-9](-?[a-z0-9])*$' || die "invalid slug '$SLUG'"
[ -f .env ] || die "deploy/.env missing — run deploy.sh at least once first."

read_env() {
	grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true
}

APP_DOMAIN=$(read_env APP_DOMAIN)
[ -n "$APP_DOMAIN" ] || die "APP_DOMAIN not set in the sops env."
HOST="${SLUG}.${APP_DOMAIN}"
DB_NAME="feoh_${SLUG}"

BUCKET="${BACKUP_S3_BUCKET:-}"
[ -n "$BUCKET" ] || BUCKET=$(read_env BACKUP_S3_BUCKET)

# Same region fallback as backup.sh: on EC2 the CLI infers it from IMDS, off-EC2
# there is none, so take what the sops env already carries.
if [ -z "${AWS_DEFAULT_REGION:-}" ] && [ -z "${AWS_REGION:-}" ]; then
	REGION=$(read_env AWS_REGION)
	[ -z "$REGION" ] || export AWS_DEFAULT_REGION="$REGION"
fi

# --- 0. Show what is about to go, from the application's own inventory -------

echo "==> inventory"
"${COMPOSE[@]}" exec -T api python scripts/delete_tenant.py --slug "$SLUG" --dry-run

backup_keys() {
	# Every version and delete marker of this tenant's nightly dumps, across
	# every date prefix. `--output text` keeps jq out of the dependency list —
	# the VM installs docker, git, cronie, awscli and sops, and nothing else.
	[ -n "$BUCKET" ] || return 0
	# Each list is filtered SEPARATELY and the results flattened after. The
	# obvious shape — `[Versions, DeleteMarkers][][?ends_with(...)]` — parses
	# fine, runs fine, and matches NOTHING: the filter binds to the flattened
	# projection rather than to its elements. Verified against real S3
	# semantics; the failure is silent, so it would have reported "removed 0
	# version(s)" while leaving every backup in place, under a confirmation
	# saying the backups were deleted.
	#
	# The `/` in the suffix is load-bearing too: without it `feoh_acme.dump`
	# would also match a tenant named `not-acme`.
	aws s3api list-object-versions --bucket "$BUCKET" --prefix "pg/" \
		--query "[Versions[?ends_with(Key, '/${DB_NAME}.dump')], DeleteMarkers[?ends_with(Key, '/${DB_NAME}.dump')]][][].[Key,VersionId]" \
		--output text 2>/dev/null | grep -v '^None' || true
}

if [ -n "$BUCKET" ]; then
	BACKUP_COUNT=$(backup_keys | grep -c . || true)
	echo "Backup objects:    ${BACKUP_COUNT} version(s) of pg/*/${DB_NAME}.dump in s3://${BUCKET}"
else
	echo "Backup objects:    BACKUP_S3_BUCKET is not set — no backup store to clean."
fi
echo "Caddy host:        ${HOST}"

if [ "$DRY_RUN" -eq 1 ]; then
	echo
	echo "--dry-run: nothing was changed."
	exit 0
fi

# --- 1. Confirm -------------------------------------------------------------

if [ "$ASSUME_YES" -ne 1 ]; then
	echo
	echo "This destroys the tenant's documents, database, control-plane records"
	echo "and backups. It cannot be undone."
	printf "Type the slug (%s) to confirm: " "$SLUG"
	read -r TYPED
	[ "$TYPED" = "$SLUG" ] || die "confirmation did not match — nothing was changed."
fi

# --- 2. Stop serving the host ----------------------------------------------

if grep -q "^${HOST} {" tenants.caddy 2>/dev/null; then
	echo "==> removing the Caddy host block for ${HOST}"
	# Drop the block from its host line to its closing brace. add-tenant.sh
	# writes a fixed three-line shape, and awk tracks the brace rather than
	# assuming a line count, so a hand-edited block with extra directives is
	# removed whole instead of leaving an orphan `}` that breaks the config.
	awk -v host="${HOST} {" '
		$0 == host { skipping = 1; next }
		skipping && $0 == "}" { skipping = 0; next }
		skipping { next }
		{ print }
	' tenants.caddy >tenants.caddy.tmp
	mv tenants.caddy.tmp tenants.caddy
	"${COMPOSE[@]}" exec caddy caddy reload --config /etc/caddy/Caddyfile
else
	echo "==> no Caddy host block for ${HOST} (already removed)"
fi

# --- 3. Documents, database, control plane ---------------------------------

echo "==> deleting tenant data"
"${COMPOSE[@]}" exec -T api python scripts/delete_tenant.py --slug "$SLUG" --yes

# --- 4. Backups -------------------------------------------------------------

if [ -n "$BUCKET" ]; then
	echo "==> deleting backup objects from s3://${BUCKET}"
	REMOVED=0
	while IFS=$'\t' read -r KEY VERSION; do
		[ -n "$KEY" ] || continue
		aws s3api delete-object --bucket "$BUCKET" --key "$KEY" --version-id "$VERSION" >/dev/null
		REMOVED=$((REMOVED + 1))
	done < <(backup_keys)
	echo "    removed ${REMOVED} object version(s)"
fi

# --- 5. The written confirmation, and what it does not cover ----------------

cat <<-EOF

	Tenant ${SLUG} removed.

	  host stopped:     ${HOST}
	  tenant database:  ${DB_NAME}
	  backups:          $([ -n "$BUCKET" ] && echo "pg/*/${DB_NAME}.dump removed from s3://${BUCKET}" || echo "no backup store configured")

	Two residues remain, exactly as /legal/dpa § 13 discloses:

	  - The nightly CONTROL-PLANE dump (pg/*/feohledger.dump) is shared across
	    every tenant and is not selectively editable, so this tenant's employee
	    rows persist inside it until it ages out on the ordinary backup
	    retention cycle (90 days by default).
	  - Any audit event already shipped to write-once archival cannot be deleted
	    before its retention period expires.

	Send the customer the confirmation printed above under "Tenant deletion".
EOF
