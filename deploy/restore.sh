#!/usr/bin/env bash
# Restore Postgres from the nightly S3 backups written by deploy/backup.sh
# (docs/minimal-deployment.md § Backups). This is the other half of the DR
# story — run it against a scratch stack once BEFORE you ever need it.
#
# Streams everything straight from S3 (nothing persists on local disk):
#   1. role globals via psql — "already exists" errors are benign on a
#      non-fresh cluster (pg_dumpall globals aren't idempotent)
#   2. each requested DB via pg_restore --create; a DB that already exists
#      is skipped unless --force, which drops + recreates it
#
# The api container is stopped for the duration (its open connections would
# block DROP/CREATE DATABASE) and the stack is rolled back up at the end.
#
# With an external database (RDS — deploy/lib.sh decides) the tools run in the
# one-shot `pgtools` container against the RDS host, as its master user, and
# two things differ, both because that user is rds_superuser, not a superuser:
#   - role globals are NOT replayed. The app needs exactly one role — the
#     master user it connects as, which RDS already has — and a dump's roles
#     (the local container's `postgres` SUPERUSER, or RDS's own rds* roles)
#     cannot be recreated as they were dumped.
#   - each DB restores with --no-owner --no-acl, so every object is owned by
#     the master user the app connects as, whichever server it was dumped from
#     (a local-container dump names `postgres` as owner).
# For RDS this script is the provider-independent fallback: a point-in-time
# restore (docs/minimal-deployment.md § Backups) is the first choice.
#
# Usage: restore.sh <YYYY-MM-DD> [--force] [db ...]
#   db ...    restore only these databases (default: every .dump under the
#             date prefix)
#   --force   drop + recreate databases that already exist
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "restore.sh: $*" >&2
	exit 1
}

STAMP="${1:-}"
echo "$STAMP" | grep -Eq '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' ||
	die "usage: restore.sh <YYYY-MM-DD> [--force] [db ...]"
shift

FORCE=0
DBS=()
for arg in "$@"; do
	case "$arg" in
	--force) FORCE=1 ;;
	*) DBS+=("$arg") ;;
	esac
done

BUCKET="${BACKUP_S3_BUCKET:-}"
if [ -z "$BUCKET" ] && [ -f .env ]; then
	BUCKET=$(feoh_env BACKUP_S3_BUCKET)
fi
[ -n "$BUCKET" ] || die "BACKUP_S3_BUCKET not set (env var or deploy/.env)."

# Off-EC2 (e.g. Hetzner) there is no IMDS to infer a region from — reuse the
# AWS_REGION the sops env carries (same fallback as backup.sh).
if [ -z "${AWS_DEFAULT_REGION:-}" ] && [ -z "${AWS_REGION:-}" ] && [ -f .env ]; then
	REGION=$(feoh_env AWS_REGION)
	if [ -n "$REGION" ]; then
		export AWS_DEFAULT_REGION="$REGION"
	fi
fi

PREFIX="s3://${BUCKET}/pg/${STAMP}"

feoh_load_db_mode || die "could not determine the database mode from deploy/.env."
if [ "$DB_MODE" = external ]; then
	OWNERSHIP=(--no-owner --no-acl)
	LIST_DB="$FEOH_PGDATABASE"
else
	OWNERSHIP=()
	LIST_DB=postgres
fi

if [ ${#DBS[@]} -eq 0 ]; then
	mapfile -t DBS < <(aws s3 ls "${PREFIX}/" | awk '{print $NF}' | grep '\.dump$' | sed 's/\.dump$//')
fi
[ ${#DBS[@]} -gt 0 ] || die "no dumps found under ${PREFIX}/ (wrong date? wrong bucket?)"

# Defense against odd keys in the bucket ending up interpolated into SQL /
# shell below — backup.sh only ever writes feohledger / feoh_* dumps. Same
# shape the backend's _SAFE_DB_NAME allows: tenant slugs contain hyphens
# (feoh_acme-corp), so the hyphen must be admitted here too.
for db in "${DBS[@]}"; do
	echo "$db" | grep -Eq '^[a-z][a-z0-9_-]*$' || die "unexpected database name '$db' in the backup listing"
done

if [ ${#DB_SERVICES[@]} -gt 0 ]; then
	"${COMPOSE[@]}" up -d --wait "${DB_SERVICES[@]}"
fi
echo "==> stopping api (open connections block DROP/CREATE DATABASE)"
"${COMPOSE[@]}" stop api

if [ "$DB_MODE" = external ]; then
	echo "==> external database: role globals not replayed (the app's one role is the master user; see the header)"
else
	echo "==> restoring role globals ('already exists' errors are benign on a non-fresh cluster)"
	aws s3 cp "${PREFIX}/globals.sql.gz" - | gunzip | feoh_pg psql
fi

for db in "${DBS[@]}"; do
	EXISTS=$(feoh_pg psql -d "$LIST_DB" -Atc \
		"SELECT 1 FROM pg_database WHERE datname = '${db}'")
	CLEAN=()
	if [ "$EXISTS" = "1" ]; then
		if [ "$FORCE" != 1 ]; then
			echo "==> ${db}: already exists — skipped (re-run with --force to drop + recreate)"
			continue
		fi
		CLEAN=(--clean --if-exists)
		echo "==> ${db}: dropping + restoring"
	else
		echo "==> ${db}: restoring"
	fi
	# --create connects to the maintenance DB named by -d, then creates and
	# switches to the dumped one.
	aws s3 cp "${PREFIX}/${db}.dump" - |
		feoh_pg pg_restore --create "${CLEAN[@]}" "${OWNERSHIP[@]}" -d postgres
done

echo "==> rolling the stack back up"
"${COMPOSE[@]}" up -d --wait

echo "restore complete from ${PREFIX} (${#DBS[@]} database(s) processed)"
