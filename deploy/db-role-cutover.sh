#!/usr/bin/env bash
# Move the app off the RDS master user onto its own least-privilege roles
# (docs/decisions.md §310). Run on the VM, from deploy/, while deploy/.env still
# signs in as the MASTER (the state before the cutover, or the break-glass URL
# afterwards). Idempotent: a second run changes nothing, and re-running it is
# also how ownership is handed back after a rollback window or a restore that
# created objects as the master.
#
# What it does, through the same pgtools path as psql.sh / backup.sh:
#   1. template1 gets the pgvector extension (as the master — pgvector is not a
#      trusted extension on RDS, so only rds_superuser may create it). Every
#      tenant database the app creates afterwards is copied from template1 and
#      starts with it, and provisioning's CREATE EXTENSION IF NOT EXISTS is a
#      no-op the app is allowed to make.
#   2. sql/db-role-cluster.sql: two roles. feoh_owner (NOLOGIN, CREATEDB) owns
#      everything; feoh_app (LOGIN, rds_iam, no password) signs in and acts as
#      feoh_owner. Neither has CREATEROLE or rds_superuser. The master joins
#      feoh_owner — what lets it transfer ownership, and what makes rollback
#      work — but never feoh_app, so RDS keeps accepting its password. Then the
#      control plane and every feoh_* database are handed to feoh_owner.
#   3. sql/db-role-database.sql in each of those databases: every schema,
#      relation, type and routine the master owns handed to feoh_owner, then a
#      check that nothing but extensions is left with the master.
#
# Then switch the app (deploy/README.md § Database role): in prod.sops.yaml,
# FEOH_DATABASE_URL becomes postgresql+asyncpg://feoh_app@<endpoint>:5432/feohledger
# (no password) and FEOH_DATABASE_IAM_AUTH "true"; redeploy.
#
# Usage: db-role-cutover.sh [--check | --verify-login]
#   --check         read-only report: the roles, their attributes, memberships
#                   and defaults, who owns each database, how many objects the
#                   master still owns in each, whether template1 has pgvector.
#                   Changes nothing — and, run after the cutover, proves the
#                   master still signs in with its password.
#   --verify-login  mint an IAM token for feoh_app on this VM and sign in with
#                   it — proves the rds-db:connect grant and rds_iam before
#                   prod.sops.yaml is switched. Changes nothing.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh

die() {
	echo "db-role-cutover.sh: $*" >&2
	exit 1
}

OWNER_ROLE=feoh_owner
APP_ROLE=feoh_app
IAM_ROLE=rds_iam

MODE=apply
case "${1:-}" in
"") ;;
--check) MODE=check ;;
--verify-login) MODE=verify ;;
*) die "usage: db-role-cutover.sh [--check | --verify-login]" ;;
esac

[ -f .env ] || die "deploy/.env missing — run ./decrypt-env.sh first."
feoh_load_db_mode || die "could not determine the database mode from deploy/.env."
[ "$DB_MODE" = external ] ||
	die "this is for an external database (RDS). The local Postgres container is reached as its own superuser over a socket the outside cannot reach; it has no master user to move off."

# Every query here is the master's, in databases the app now owns (it owns
# `public` in each): resolve names from the catalog only, so an app-planted
# function or operator can never run with the master's privileges. lib.sh's
# feoh_pg_run hands PGOPTIONS to the pgtools container; the SQL files set the
# same search_path themselves as well.
export PGOPTIONS="-c search_path=pg_catalog,pg_temp"

# psql against one database, SQL on stdin, stopping at the first error.
pg_sql() {
	feoh_pg psql -X -q -v ON_ERROR_STOP=1 -d "$1"
}

ROLE_SETTINGS="SET feoh.owner_role = '${OWNER_ROLE}'; SET feoh.app_role = '${APP_ROLE}'; SET feoh.iam_role = '${IAM_ROLE}';"
TENANT_DBS_SQL="SELECT datname FROM pg_database WHERE datname = current_database() OR datname LIKE 'feoh\\_%' ORDER BY datname"

if [ "$MODE" = verify ]; then
	# The app's own sign-in, exactly as the app will make it: a token minted
	# here from the instance profile, over verify-full.
	FEOH_PGUSER=$APP_ROLE
	FEOH_DB_IAM=1
	FEOH_PGREGION="${FEOH_PGREGION:-${AWS_REGION:-${AWS_DEFAULT_REGION:-$(feoh_env AWS_REGION)}}}"
	[ -n "$FEOH_PGREGION" ] || die "no AWS_REGION in deploy/.env; the token is signed per region."
	# The token is a bearer credential: verified TLS or nothing, as the app and
	# decrypt-env.sh will insist once prod.sops.yaml is switched.
	[ "${PGSSLMODE:-$(feoh_env PGSSLMODE)}" = verify-full ] ||
		die "PGSSLMODE is not verify-full; an IAM token is only ever sent to a verified server (and the app would refuse to boot without it)."
	who=$(feoh_pg psql -X -At -v ON_ERROR_STOP=1 -d "$FEOH_PGDATABASE" -c \
		"SELECT session_user || '|' || current_user || '|' || (SELECT rolsuper OR rolcreaterole FROM pg_roles WHERE rolname = current_user)") ||
		die "${APP_ROLE} could not sign in with an IAM token — check the instance role's rds-db:connect grant (terraform apply), that ./db-role-cutover.sh has run, and PGSSLMODE."
	# Proves the role default took effect too: the app must act as the owner.
	[ "$who" = "${APP_ROLE}|${OWNER_ROLE}|false" ] ||
		die "signed in, but as '${who}' (session|current|privileged) — expected ${APP_ROLE}|${OWNER_ROLE}|false. Re-run ./db-role-cutover.sh and --check."
	echo "==> signed in as ${APP_ROLE}, acting as ${OWNER_ROLE}"
	echo "db-role-cutover.sh: ${APP_ROLE} signs in with an IAM token. Switch prod.sops.yaml next (deploy/README.md § Database role)."
	exit 0
fi

[ "$FEOH_DB_IAM" = 0 ] ||
	die "deploy/.env already signs in with IAM (as ${FEOH_PGUSER}); this must run as the RDS master. For a re-run after a restore or a rollback window, use the break-glass master URL (deploy/README.md § Database role)."
case "$FEOH_PGUSER" in
"$APP_ROLE" | "$OWNER_ROLE") die "deploy/.env signs in as ${FEOH_PGUSER}; this must run as the RDS master." ;;
esac

if [ "$MODE" = check ]; then
	echo "==> roles ${OWNER_ROLE} / ${APP_ROLE} (connected as ${FEOH_PGUSER} by password)"
	pg_sql "$FEOH_PGDATABASE" <<SQL
\\pset footer off
SELECT r.rolname, r.rolcanlogin AS login, r.rolcreatedb AS createdb,
       r.rolcreaterole AS createrole, r.rolsuper AS superuser,
       pg_has_role(r.rolname, '${IAM_ROLE}', 'MEMBER') AS rds_iam,
       EXISTS (SELECT 1 FROM pg_roles s WHERE s.rolname = 'rds_superuser')
         AND pg_has_role(r.rolname, 'rds_superuser', 'MEMBER') AS rds_superuser,
       pg_has_role(current_user, r.rolname, 'SET') AS master_can_set,
       pg_has_role(current_user, r.rolname, 'USAGE') AS master_inherits,
       (SELECT string_agg(c, ', ') FROM unnest(d.setconfig) AS c) AS defaults
FROM pg_roles r
LEFT JOIN pg_db_role_setting d ON d.setrole = r.oid AND d.setdatabase = 0
WHERE r.rolname IN ('${OWNER_ROLE}', '${APP_ROLE}') ORDER BY r.rolname;
SQL
	echo "==> database owners"
	pg_sql "$FEOH_PGDATABASE" <<SQL
\\pset footer off
SELECT datname, pg_get_userbyid(datdba) AS owner FROM pg_database
WHERE datname = current_database() OR datname LIKE 'feoh\\_%' ORDER BY datname;
SQL
	echo "==> objects the master still owns (outside extensions)"
	for db in $(feoh_pg psql -X -At -d "$FEOH_PGDATABASE" -c "$TENANT_DBS_SQL"); do
		count=$(feoh_pg psql -X -At -v ON_ERROR_STOP=1 -d "$db" -c \
			"SELECT count(*) FROM pg_shdepend s WHERE s.dbid = (SELECT oid FROM pg_database WHERE datname = current_database()) AND s.refclassid = 'pg_authid'::regclass AND s.refobjid = (SELECT oid FROM pg_roles WHERE rolname = current_user) AND s.deptype = 'o' AND s.classid <> 'pg_extension'::regclass AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid = s.classid AND d.objid = s.objid AND d.deptype = 'e')")
		echo "  ${db}: ${count}"
	done
	echo "==> template1 has pgvector: $(feoh_pg psql -X -At -d template1 -c "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")"
	exit 0
fi

echo "==> template1: pgvector, so every new tenant database starts with it"
pg_sql template1 <<<"CREATE EXTENSION IF NOT EXISTS vector;"

echo "==> cluster: roles ${OWNER_ROLE} / ${APP_ROLE}, their grants, database ownership"
{
	echo "$ROLE_SETTINGS"
	cat sql/db-role-cluster.sql
} | pg_sql "$FEOH_PGDATABASE"

DBS=$(feoh_pg psql -X -At -v ON_ERROR_STOP=1 -d "$FEOH_PGDATABASE" -c "$TENANT_DBS_SQL")
grep -qx "$FEOH_PGDATABASE" <<<"$DBS" || die "the control plane ${FEOH_PGDATABASE} is not in the database list — wrong server?"
for db in $DBS; do
	echo "==> ${db}: objects"
	{
		echo "$ROLE_SETTINGS"
		cat sql/db-role-database.sql
	} | pg_sql "$db"
done

echo
echo "db-role-cutover.sh: done — $(wc -w <<<"$DBS") database(s) owned by ${OWNER_ROLE}; the app signs in as ${APP_ROLE}."
echo "Next: ./db-role-cutover.sh --check (the master still signs in by password), --verify-login, then switch prod.sops.yaml (deploy/README.md § Database role)."
