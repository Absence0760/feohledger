#!/usr/bin/env bash
# `docker compose -f compose.prod.yml …` with the database mode applied — the
# way to run an ad-hoc compose command on the VM (logs, ps, a restart, bringing
# the stack back after a failed restore). A bare `docker compose` would not
# know whether this VM runs the local Postgres container (`localdb` profile) or
# an external database; deploy/lib.sh decides that from deploy/.env, here as in
# every other deploy script.
#
# Usage: compose.sh <compose args…>     e.g. ./compose.sh up -d --wait
set -euo pipefail
cd "$(dirname "$0")"
# shellcheck source=lib.sh
. ./lib.sh
[ -f .env ] || {
	echo "compose.sh: deploy/.env missing — run deploy.sh (or decrypt-env.sh) first." >&2
	exit 1
}
feoh_load_db_mode
exec "${COMPOSE[@]}" "$@"
