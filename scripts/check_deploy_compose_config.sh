#!/usr/bin/env bash
#
# Render deploy/compose.prod.yml in BOTH database modes and fail on any error
# or any warning (#575 § Environment parity).
#
# #569: in RDS mode the VM's compose printed a POSTGRES_PASSWORD "variable is
# not set" warning on every call — compose interpolates every service, the
# profiled-out `postgres` too — and nothing caught it, because no CI job ran
# compose against deploy/ at all, let alone with the VM's version. CI's
# `deploy-compose` job runs this after scripts/install_vm_compose.sh.
#
# Each mode gets a placeholder deploy/.env holding only what that mode's sops
# env must provide for compose (prod.sops.yaml.example), and the compose
# command comes from deploy/lib.sh's feoh_load_db_mode — the same array every
# deploy script runs — so the mode is selected exactly as on the VM. Every
# profile is activated on top (`tools`, `build`), since deploy.sh and backup.sh
# run those services with `run`.
#
# It works on a scratch copy of deploy/ (beside a link to backend/, which the
# compose file references), so it never touches a real deploy/.env.
#
# Usage: scripts/check_deploy_compose_config.sh [<repo-root>]
# Its tests: backend/tests/test_deploy_compose_parity.py
set -euo pipefail

root=$(cd "${1:-$(dirname "$0")/..}" && pwd)
[ -f "$root/deploy/compose.prod.yml" ] || {
	echo "check_deploy_compose_config: no deploy/compose.prod.yml under ${root}" >&2
	exit 2
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir "$work/deploy"
cp "$root/deploy/compose.prod.yml" "$root/deploy/lib.sh" "$work/deploy/"
ln -s "$root/backend" "$work/backend"

COMMON='APP_DOMAIN=feohledger.example
API_DOMAIN=api.feohledger.example
ACME_EMAIL=ops@feohledger.example
AWS_REGION=us-east-1'

echo "docker compose $(docker compose version --short)"
failed=0
for mode in local external; do
	if [ "$mode" = local ]; then
		env_text="POSTGRES_PASSWORD=placeholder-password
${COMMON}"
	else
		env_text="FEOH_DATABASE_URL=postgresql+asyncpg://feohadmin:placeholder@db.feohledger.example:5432/feohledger
PGSSLMODE=verify-full
${COMMON}"
	fi
	printf '%s\n' "$env_text" >"$work/deploy/.env"
	# A subshell per mode: lib.sh sets COMPOSE / DB_MODE / PGPASSWORD globals,
	# and FEOH_DATABASE_URL in this environment would override the .env.
	# errexit does not apply inside an `if` condition, hence the explicit exits.
	if ! (
		cd "$work/deploy" || exit 1
		unset FEOH_DATABASE_URL
		# shellcheck source=deploy/lib.sh
		. ./lib.sh || exit 1
		feoh_load_db_mode || exit 1
		if [ "$DB_MODE" != "$mode" ]; then
			echo "the ${mode}-mode placeholder env selected ${DB_MODE} mode" >&2
			exit 1
		fi
		echo "==> ${mode}: ${COMPOSE[*]} --profile tools --profile build config"
		rc=0
		"${COMPOSE[@]}" --profile tools --profile build config -q 2>"../stderr" || rc=$?
		if [ "$rc" -ne 0 ] || [ -s ../stderr ]; then
			echo "FAIL ${mode} mode: compose exited ${rc} and wrote to stderr:" >&2
			cat ../stderr >&2
			exit 1
		fi
	); then
		failed=1
	fi
done

if [ "$failed" -ne 0 ]; then
	echo "deploy/compose.prod.yml does not render cleanly in every database mode — fix the warning at its source (a variable compose interpolates needs a default or a sops entry)." >&2
	exit 1
fi
echo "deploy/compose.prod.yml renders with no warnings in local and external mode."
