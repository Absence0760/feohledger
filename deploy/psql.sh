#!/usr/bin/env bash
# psql against this VM's database, whichever mode it runs in (deploy/lib.sh):
# `exec` into the local `postgres` container, or the one-shot `pgtools`
# container against RDS over verify-full TLS, the password passed through the
# environment. For the operator's ad-hoc queries — listing tenant slugs after a
# rebuild, checking an extension — where `./compose.sh exec postgres psql`
# only works in container mode.
#
# Usage: psql.sh [psql args…]    e.g. ./psql.sh -d feohledger -Atc "SELECT slug FROM organizations"
# With no -d, psql connects to the database named after the user (container:
# `postgres`; RDS: the master user's name, which usually does not exist) — so
# pass -d. Non-interactive (no TTY — it pipes, like backup.sh's calls): give
# it -c or SQL on stdin. For an interactive session in container mode use
# `./compose.sh exec postgres psql -U postgres`.
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh
[ -f .env ] || {
	echo "psql.sh: deploy/.env missing — run deploy.sh (or decrypt-env.sh) first." >&2
	exit 1
}
feoh_load_db_mode
feoh_pg psql "$@"
