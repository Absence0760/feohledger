# shellcheck shell=bash
# The variables this file sets are its outputs, read by the sourcing script.
# shellcheck disable=SC2034
# Shared by every deploy script (sourced, never run): the ONE place the
# database mode is decided, and the compose invocation that follows from it —
# plus feoh_sync_source, deploy.sh's choice of which commit to build (bottom).
#
#   external — the sops env sets FEOH_DATABASE_URL (the documented path:
#              Amazon RDS). The `postgres` service never starts; pg client
#              tools run in the one-shot `pgtools` service against the URL's
#              host. PGSSLMODE comes from the sops env (decrypt-env.sh refuses
#              anything weaker than `require`).
#   local    — FEOH_DATABASE_URL unset: the Postgres container under the
#              `localdb` profile, reached with `exec` exactly as before.
#
# Callers `cd` into deploy/ first, then `. ./lib.sh` and call
# `feoh_load_db_mode` once deploy/.env exists (deploy.sh: after decrypt-env.sh).
# It sets:
#   DB_MODE      external | local
#   COMPOSE      the compose command array, with `--profile localdb` in local mode
#   DB_SERVICES  the services that must be up for the API to reach its database
#                (`postgres` in local mode, nothing in external mode)
# and, in external mode, the libpq connection variables for `pgtools`
# (feoh_pg_run). decrypt-env.sh sources this file too, to validate the URL with
# the same parser the scripts use.

# The value of KEY in deploy/.env (last one wins, as for compose), or empty.
feoh_env() {
	grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# The external-database URL contract, as one ERE: an asyncpg URL with user,
# password, a host name (use the RDS endpoint: verify-full checks the
# certificate against it, so an IP or a CNAME of your own fails the handshake;
# IPv6 literals are not accepted at all), an optional port, a database, and NO
# query string. TLS is set by PGSSLMODE alone, which asyncpg (the app) and libpq
# (pg_dump & co.) both read; a `?ssl=` on the URL would reach the app but not
# libpq, so the two halves would disagree about it. User and password are
# URL-unreserved characters or %XX escapes — anything else percent-encoded
# (`openssl rand -hex 24` needs none). Groups: 1 user, 3 password, 5 host,
# 7 port, 8 database.
FEOH_EXTERNAL_DB_URL_RE='^postgresql\+asyncpg://(([A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+):(([A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)@([A-Za-z0-9.-]+)(:([0-9]+))?/([A-Za-z0-9_-]+)$'

# Percent-decode (a URL-encoded password: `p%40ss` -> `p@ss`).
feoh_urldecode() {
	printf '%b' "${1//%/\\x}"
}

feoh_load_db_mode() {
	COMPOSE=(docker compose -f compose.prod.yml)
	# Compose gives the shell environment precedence over .env when it
	# interpolates, so mirror that here or the scripts and compose could
	# disagree about which database the stack is on.
	local url="${FEOH_DATABASE_URL:-$(feoh_env FEOH_DATABASE_URL)}"
	if [ -z "$url" ]; then
		DB_MODE=local
		COMPOSE+=(--profile localdb)
		DB_SERVICES=(postgres)
		return 0
	fi
	DB_MODE=external
	DB_SERVICES=()
	if ! [[ "$url" =~ $FEOH_EXTERNAL_DB_URL_RE ]]; then
		echo "FEOH_DATABASE_URL does not match the external-database contract (see deploy/prod.sops.yaml.example) — run ./decrypt-env.sh for the details." >&2
		return 1
	fi
	FEOH_PGUSER=$(feoh_urldecode "${BASH_REMATCH[1]}")
	FEOH_PGHOST="${BASH_REMATCH[5]}"
	FEOH_PGPORT="${BASH_REMATCH[7]:-5432}"
	FEOH_PGDATABASE="${BASH_REMATCH[8]}"
	# Exported so `docker compose run -e PGPASSWORD` (no `=value`) hands it to
	# the container from this process's environment — never from an argv that
	# `ps` would show.
	PGPASSWORD=$(feoh_urldecode "${BASH_REMATCH[3]}")
	export PGPASSWORD
}

# Run a Postgres client tool against this VM's database, whichever mode it is
# in: `feoh_pg pg_dump -Fc -d mydb`. stdin/stdout pass through (no TTY), so it
# pipes. Local mode `exec`s into the running container as the `postgres`
# superuser over its socket (what backup.sh/restore.sh always did); external
# mode runs the one-shot `pgtools` container as the URL's user (RDS: the master
# user, a member of rds_superuser — NOT a superuser).
feoh_pg() {
	if [ "$DB_MODE" = external ]; then
		feoh_pg_run "$@"
	else
		"${COMPOSE[@]}" exec -T postgres "$1" -U postgres "${@:2}"
	fi
}

# The external half of feoh_pg: the one-shot `pgtools` container, libpq
# connection variables from the URL (feoh_load_db_mode).
feoh_pg_run() {
	"${COMPOSE[@]}" run --rm -T --no-deps \
		-e PGHOST="$FEOH_PGHOST" \
		-e PGPORT="$FEOH_PGPORT" \
		-e PGUSER="$FEOH_PGUSER" \
		-e PGPASSWORD \
		pgtools "$@"
}

# ── Source: which commit the deploy builds ───────────────────────────────────
#
# feoh_sync_source <repo-root> [<sha>] brings the checkout to the commit to
# deploy, and always leaves it on the `main` branch:
#
#   no sha — fast-forward main to origin/main (a manual `./deploy.sh`).
#   sha    — exactly that commit (a release: .github/workflows/vm-deploy.yml
#            passes the commit the release tag points at, through deploy.sh
#            --ref). It must be a full sha and on origin/main — a release tag
#            on a branch is refused, not deployed. `main` is moved to it, so an
#            older sha is a rollback, and because that commit is an ancestor of
#            origin/main the next plain pull still fast-forwards. A modified
#            tracked file is refused: the deploy would no longer be the commit
#            it names.
feoh_sync_source() {
	local root=$1 ref=${2:-}
	if [ -z "$ref" ]; then
		git -C "$root" pull --ff-only
		return
	fi
	if ! [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
		echo "--ref takes a full 40-character commit sha, got '${ref}'." >&2
		return 1
	fi
	if ! git -C "$root" diff --quiet HEAD --; then
		echo "the checkout has modified tracked files; a --ref deploy would not be the commit it names. Commit or discard them first (git -C ${root} status)." >&2
		return 1
	fi
	git -C "$root" fetch --quiet origin main || return 1
	if ! git -C "$root" cat-file -e "${ref}^{commit}" 2>/dev/null; then
		echo "commit ${ref} is not in origin — was the release made from another repository?" >&2
		return 1
	fi
	if ! git -C "$root" merge-base --is-ancestor "$ref" origin/main; then
		echo "commit ${ref} is not on origin/main; only commits on main are deployed. Tag a commit on main and publish the release again." >&2
		return 1
	fi
	# Explicit returns throughout: callers run this as `feoh_sync_source … || die`,
	# where errexit does not apply inside the function.
	git -C "$root" checkout --quiet -B main "$ref" || return 1
	echo "==> source: main at ${ref}"
}
