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
#   - role globals are NOT replayed. The roles the app needs already exist
#     on the instance (the master, and the feoh_owner / feoh_app roles
#     db-role-cutover.sh made),
#     and a dump's roles (the local container's `postgres` SUPERUSER, or RDS's
#     own rds* roles) cannot be recreated as they were dumped.
#   - each DB restores with --no-owner --no-acl, so every object is owned by
#     the master user it runs as, whichever server it was dumped from (a
#     local-container dump names `postgres` as owner). It must run as the
#     master — see the IAM check below — and ./db-role-cutover.sh afterwards
#     moves ownership to feoh_owner again. Run that way (break-glass), the api
#     is deliberately left stopped at the end.
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
# A logical restore needs the RDS master. pg_restore --create recreates each
# database from template0, which has no pgvector, and only rds_superuser may
# CREATE EXTENSION vector (it is not a trusted extension on RDS); the app role
# would fail every tenant. So this is a break-glass operation: point
# FEOH_DATABASE_URL at the master for it, then re-run ./db-role-cutover.sh,
# which hands everything the restore created back to the app role, and switch
# back (docs/decisions.md §304, deploy/README.md § Database role). A
# point-in-time restore — the first choice on RDS — keeps the roles and needs
# none of this.
if [ "${FEOH_DB_IAM:-0}" = 1 ]; then
	die "deploy/.env signs in as the IAM app role, which cannot recreate the pgvector extension a restore needs. Restore as the RDS master (break-glass: FEOH_DATABASE_URL with the master password, FEOH_DATABASE_IAM_AUTH false — e.g. in this shell's environment for this one command), then run ./db-role-cutover.sh. See deploy/README.md § Database role."
fi
# Break-glass: .env signs in as the app role, and this shell overrides it with
# the master for this one run. The api must not be rolled back up from here —
# compose would hand it this shell's master URL — and the restored objects
# belong to the master until db-role-cutover.sh hands them back.
BREAK_GLASS=0
if feoh_is_true "$(feoh_env FEOH_DATABASE_IAM_AUTH)"; then
	BREAK_GLASS=1
fi
if [ "$DB_MODE" = external ]; then
	# The master replays dumps of databases the app owns: resolve the queries'
	# names from the catalog only (as db-role-cutover.sh does), never from a
	# schema the app could have planted a function in.
	export PGOPTIONS="-c search_path=pg_catalog,pg_temp"
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
	echo "==> external database: role globals not replayed (the roles the app needs already exist on the instance; see the header)"
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

if [ "$BREAK_GLASS" = 1 ]; then
	echo
	echo "restore complete from ${PREFIX} (${#DBS[@]} database(s) processed) — the api is left STOPPED:"
	echo "  1. in this shell (still the master): ./db-role-cutover.sh   — hands the restored objects to feoh_owner"
	echo "  2. from a CLEAN shell (no FEOH_DATABASE_URL override): ./compose.sh up -d --wait"
	echo "  3. rotate the master password (docs/secrets-rotation.md § RDS master password (break-glass))"
	exit 0
fi

echo "==> rolling the stack back up"
"${COMPOSE[@]}" up -d --wait

echo "restore complete from ${PREFIX} (${#DBS[@]} database(s) processed)"
