#!/usr/bin/env bash
# Nightly Postgres backup to S3 (docs/minimal-deployment.md § Backups).
# Dumps role definitions plus the control plane and EVERY feoh_* tenant DB
# (pg_dump custom format, already compressed), streaming straight to S3 —
# nothing persists on local disk. Credentials come from the EC2 instance
# profile; the target bucket from BACKUP_S3_BUCKET (env, or deploy/.env).
#
# Both database modes (deploy/lib.sh decides which):
#   local    — the tools run inside the Postgres container (`exec`), over its
#              local socket, exactly as before.
#   external — RDS. Its automated backups + point-in-time restore are the
#              PRIMARY recovery; these dumps are the long-retention,
#              provider-independent copy (restorable into a plain Postgres
#              anywhere). The tools run in the one-shot `pgtools` container
#              against the RDS host, TLS per PGSSLMODE (verify-full), the
#              password passed through the environment, never an argv.
#              Roles are dumped with --no-role-passwords: RDS does not let
#              even the master user read pg_authid.
#
# Cron (see deploy/README.md):
#   17 3 * * * /path/to/repo/deploy/backup.sh >> /var/log/feoh-backup.log 2>&1
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh

BUCKET="${BACKUP_S3_BUCKET:-}"
if [ -z "$BUCKET" ] && [ -f .env ]; then
	BUCKET=$(feoh_env BACKUP_S3_BUCKET)
fi
if [ -z "$BUCKET" ]; then
	echo "BACKUP_S3_BUCKET not set (env var or deploy/.env)." >&2
	exit 1
fi

# On EC2 the aws CLI infers the region from IMDS; off-EC2 (the Hetzner
# variant) there is no IMDS, so fall back to the AWS_REGION the sops env
# already carries.
if [ -z "${AWS_DEFAULT_REGION:-}" ] && [ -z "${AWS_REGION:-}" ] && [ -f .env ]; then
	REGION=$(feoh_env AWS_REGION)
	if [ -n "$REGION" ]; then
		export AWS_DEFAULT_REGION="$REGION"
	fi
fi

feoh_load_db_mode

if [ "$DB_MODE" = external ]; then
	# RDS keeps role passwords in pg_authid, which its master user cannot read.
	GLOBALS_FLAGS=(--globals-only --no-role-passwords)
	LIST_DB="$FEOH_PGDATABASE"
else
	GLOBALS_FLAGS=(--globals-only)
	LIST_DB=postgres
fi

STAMP=$(date -u +%F)
PREFIX="s3://${BUCKET}/pg/${STAMP}"

# Roles / globals — tiny, plain SQL.
feoh_pg pg_dumpall "${GLOBALS_FLAGS[@]}" |
	gzip | aws s3 cp - "${PREFIX}/globals.sql.gz"

DBS=$(feoh_pg psql -d "$LIST_DB" -Atc \
	"SELECT datname FROM pg_database WHERE datname = 'feohledger' OR datname LIKE 'feoh\\_%' ORDER BY datname")

for db in $DBS; do
	feoh_pg pg_dump -Fc -d "$db" |
		aws s3 cp - "${PREFIX}/${db}.dump"
done

echo "backup complete (${DB_MODE} database): ${PREFIX} ($(echo "$DBS" | wc -w) databases + globals)"

# Optional dead-man's-switch: ping a heartbeat URL (healthchecks.io-style)
# after a successful run, so backups that stop running get noticed instead
# of discovered during a restore. No-op when unset.
PING_URL="${BACKUP_PING_URL:-}"
if [ -z "$PING_URL" ] && [ -f .env ]; then
	PING_URL=$(feoh_env BACKUP_PING_URL)
fi
if [ -n "$PING_URL" ]; then
	curl -fsS -m 10 --retry 3 "$PING_URL" >/dev/null ||
		echo "WARN: backup heartbeat ping failed (backup itself succeeded)" >&2
fi
