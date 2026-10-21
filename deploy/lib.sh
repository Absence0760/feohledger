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
#              anything weaker than `require`, and anything but `verify-full`
#              with IAM auth). Two ways to sign in (docs/decisions.md §310):
#                IAM  — FEOH_DATABASE_IAM_AUTH true and a URL with NO password
#                       (the app role, `feoh_app`). Each pg tool run gets a
#                       fresh 15-minute RDS auth token, minted here on the VM
#                       with the instance profile and passed as PGPASSWORD
#                       through the environment.
#                password — the URL carries one (the RDS master: the state
#                       before db-role-cutover.sh, and the break-glass path).
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
# (feoh_pg_run), plus
#   FEOH_DB_IAM    1 when the tools sign in with an IAM token, else 0
#   FEOH_PGREGION  the region the token is signed for (IAM mode only)
# decrypt-env.sh sources this file too, to validate the URL with the same
# parser the scripts use.

# The value of KEY in deploy/.env (last one wins, as for compose), or empty.
feoh_env() {
	grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true
}

# The external-database URL contract, as one ERE: an asyncpg URL with a user,
# an optional password (absent exactly when FEOH_DATABASE_IAM_AUTH is on — the
# callers check which), a host name (use the RDS endpoint: verify-full checks
# the certificate against it, and an IAM token is signed for that exact name,
# so an IP or a CNAME of your own fails; IPv6 literals are not accepted at all),
# an optional port, a database, and NO query string. TLS is set by PGSSLMODE
# alone, which asyncpg (the app) and libpq (pg_dump & co.) both read; a `?ssl=`
# on the URL would reach the app but not libpq, so the two halves would
# disagree about it. User and password are URL-unreserved characters or %XX
# escapes — anything else percent-encoded (`openssl rand -hex 24` needs none).
# Groups: 1 user, 4 password (empty when absent), 6 host, 8 port, 9 database.
FEOH_EXTERNAL_DB_URL_RE='^postgresql\+asyncpg://(([A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+)(:(([A-Za-z0-9._~-]|%[0-9A-Fa-f]{2})+))?@([A-Za-z0-9.-]+)(:([0-9]+))?/([A-Za-z0-9_-]+)$'

# FEOH_DATABASE_IAM_AUTH, read the way the app reads it (pydantic's bool): these
# spellings are true, unset/empty is false, and anything else is an error
# (decrypt-env.sh refuses it, because the app would refuse to boot on it).
feoh_is_true() {
	case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
	1 | true | t | yes | y | on) return 0 ;;
	*) return 1 ;;
	esac
}
feoh_is_false() {
	case "$(printf '%s' "$1" | tr '[:upper:]' '[:lower:]')" in
	"" | 0 | false | f | no | n | off) return 0 ;;
	*) return 1 ;;
	esac
}

# An RDS IAM auth token for <host> <port> <user>, on stdout. Signed locally by
# the AWS CLI with the instance profile (bootstrap-vm.sh installs it); the VM
# role holds rds-db:connect on exactly the app role (infra/compute.tf). Valid
# for 15 minutes for NEW connections, so callers mint one per tool run. Nothing
# secret is on its argv; the token itself is the output.
feoh_iam_token() {
	aws rds generate-db-auth-token --hostname "$1" --port "$2" --username "$3" \
		--region "$FEOH_PGREGION" --output text
}

# Percent-decode (a URL-encoded password: `p%40ss` -> `p@ss`).
feoh_urldecode() {
	printf '%b' "${1//%/\\x}"
}

feoh_load_db_mode() {
	COMPOSE=(docker compose -f compose.prod.yml)
	FEOH_DB_IAM=0
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
	FEOH_PGHOST="${BASH_REMATCH[6]}"
	FEOH_PGPORT="${BASH_REMATCH[8]:-5432}"
	FEOH_PGDATABASE="${BASH_REMATCH[9]}"
	local password="${BASH_REMATCH[4]}"
	# Same precedence as the url above, and as compose.prod.yml passes it to the api.
	local iam="${FEOH_DATABASE_IAM_AUTH:-$(feoh_env FEOH_DATABASE_IAM_AUTH)}"
	if feoh_is_true "$iam"; then
		# Fail closed, as the app does: a password left beside IAM auth is the
		# long-lived credential the switch exists to retire.
		if [ -n "$password" ]; then
			echo "FEOH_DATABASE_IAM_AUTH is on but FEOH_DATABASE_URL carries a password — remove it (run ./decrypt-env.sh for the details)." >&2
			return 1
		fi
		FEOH_PGREGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(feoh_env AWS_REGION)}}"
		if [ -z "$FEOH_PGREGION" ]; then
			echo "FEOH_DATABASE_IAM_AUTH is on but no AWS_REGION is set; the auth token is signed per region." >&2
			return 1
		fi
		# Checked here, not only by decrypt-env.sh at deploy time: a shell override
		# or an edited .env would otherwise send the bearer token under `prefer`.
		local sslmode="${PGSSLMODE:-$(feoh_env PGSSLMODE)}"
		if [ "$sslmode" != verify-full ]; then
			echo "FEOH_DATABASE_IAM_AUTH is on but PGSSLMODE is not verify-full — an IAM token is only ever sent to a verified server." >&2
			return 1
		fi
		FEOH_DB_IAM=1
		# Minted per tool run by feoh_pg_run, never held here.
		unset PGPASSWORD
		return 0
	fi
	if [ -z "$password" ]; then
		echo "FEOH_DATABASE_URL has no password and FEOH_DATABASE_IAM_AUTH is not on — nothing would sign the connection in (run ./decrypt-env.sh for the details)." >&2
		return 1
	fi
	FEOH_DB_IAM=0
	# Exported so `docker compose run -e PGPASSWORD` (no `=value`) hands it to
	# the container from this process's environment — never from an argv that
	# `ps` would show.
	PGPASSWORD=$(feoh_urldecode "$password")
	export PGPASSWORD
}

# Run a Postgres client tool against this VM's database, whichever mode it is
# in: `feoh_pg pg_dump -Fc -d mydb`. stdin/stdout pass through (no TTY), so it
# pipes. Local mode `exec`s into the running container as the `postgres`
# superuser over its socket (what backup.sh/restore.sh always did); external
# mode runs the one-shot `pgtools` container as the URL's user — after the
# cutover `feoh_app`, which acts as `feoh_owner` (owner of every app database,
# CREATEDB, not a superuser, not in rds_superuser); before it, or on the
# break-glass path, the RDS master (a member of rds_superuser — NOT a superuser).
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
	if [ "${FEOH_DB_IAM:-0}" = 1 ]; then
		# A fresh token for this run: it outlives no connection it opens, and a
		# nightly backup's many runs each get their own.
		PGPASSWORD=$(feoh_iam_token "$FEOH_PGHOST" "$FEOH_PGPORT" "$FEOH_PGUSER") || {
			echo "could not mint an RDS IAM auth token for ${FEOH_PGUSER} (aws rds generate-db-auth-token failed — instance profile, region, or the rds-db:connect grant)." >&2
			return 1
		}
		export PGPASSWORD
	fi
	"${COMPOSE[@]}" run --rm -T --no-deps \
		-e PGHOST="$FEOH_PGHOST" \
		-e PGPORT="$FEOH_PGPORT" \
		-e PGUSER="$FEOH_PGUSER" \
		-e PGPASSWORD \
		-e PGOPTIONS \
		pgtools "$@"
}

# ── Provisioning a tenant ────────────────────────────────────────────────────
#
# feoh_tenant_argv <slug> <name> <admin-email> [<admin-password>] builds the
# command that provisions a tenant inside the api container, without running
# it. add-tenant.sh execs it into the running api; CI's day-one job
# (scripts/day-one/stack.sh) execs the same array into its api container, so
# the rehearsal provisions exactly as production does (#575). It sets:
#
#   FEOH_TENANT_ARGV      the scripts/create_tenant.py command line
#   FEOH_TENANT_PASSWORD  the admin's password: the one given, or a generated
#                         temporary one
#   FEOH_TENANT_FORCED    1 when the password was generated, which also forces
#                         a change at first login; 0 otherwise
#
# --skip-existing makes add-tenant.sh re-runnable: a run that provisioned the
# tenant but died before the Caddy step skips provisioning cleanly instead of
# tripping the deliberate duplicate-slug error.
feoh_tenant_argv() {
	local slug=$1 name=$2 email=$3 password=${4:-}
	FEOH_TENANT_FORCED=0
	if [ -z "$password" ]; then
		# Prefix guarantees the app's complexity rules (12+ chars, upper/lower/digit).
		password="Aa1-$(openssl rand -hex 12)" || return 1
		FEOH_TENANT_FORCED=1
	fi
	FEOH_TENANT_PASSWORD=$password
	FEOH_TENANT_ARGV=(python scripts/create_tenant.py
		--name "$name" --slug "$slug"
		--admin-email "$email" --admin-password "$password"
		--skip-existing)
	if [ "$FEOH_TENANT_FORCED" = 1 ]; then
		FEOH_TENANT_ARGV+=(--force-password-change)
	fi
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
#
# A sha whose compose.prod.yml does not mount ./www into Caddy is refused too
# (FEOH_WWW_MOUNT_RE, below).
FEOH_WWW_MOUNT_RE='^[[:space:]]*-[[:space:]]*\./www:/srv(:ro)?[[:space:]]*$'

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
	# A commit from before the www/ layout (docs/decisions.md §277) would be
	# deployed by its own deploy.sh and compose.prod.yml, which mount
	# ../frontend/build into Caddy again — the full-site 404 outage of #579 the
	# moment its build runs. Refused before the checkout moves.
	if ! git -C "$root" show "${ref}:deploy/compose.prod.yml" 2>/dev/null |
		grep -Eq "$FEOH_WWW_MOUNT_RE"; then
		echo "commit ${ref} predates the deploy/www/ SPA layout (docs/decisions.md §277): its compose file mounts frontend/build into Caddy, which serves every page as an empty 404 after a rebuild (#579). To roll the SPA back, swap to the previous release instead (deploy/README.md § Rolling the frontend back); rolling the stack back to a pre-layout commit is not supported." >&2
		return 1
	fi
	# Explicit returns throughout: callers run this as `feoh_sync_source … || die`,
	# where errexit does not apply inside the function.
	git -C "$root" checkout --quiet -B main "$ref" || return 1
	echo "==> source: main at ${ref}"
}

# ── The SPA Caddy serves: published releases, swapped atomically ─────────────
#
# Caddy bind-mounts deploy/www/ at /srv and serves /srv/current, a RELATIVE
# symlink (`releases/<id>`, so it resolves the same inside the container) to the
# live build (docs/decisions.md §277). Never mount the build output itself: the
# SvelteKit build deletes and recreates frontend/build, a bind mount pins the
# directory's inode, and the container is left holding an orphaned, empty
# directory — every page 404s with an empty body and runc refuses `exec`
# ("current working directory is outside of container mount namespace root"),
# which is the v0.1.0 outage (#579). www/ itself is never removed, so the mount
# stays valid; only names inside it change, and a rename inside a mounted
# directory is visible to the container at once.
#
# feoh_publish_frontend <build-dir> <www-dir> <release-id> copies a finished
# build to <www-dir>/releases/<release-id>, then points `current` at it with a
# rename (`ln -sfn` to a temp name, then `mv -T` — one rename(2)), so a request
# sees the old release or the new one, never a missing or half-copied one. The
# release it replaced stays, linked as `previous`, for an instant rollback
# (deploy/README.md § Rolling the frontend back); every other one is pruned.
feoh_publish_frontend() {
	local build=$1 www=$2 id=$3 prev="" keep_prev="" rel
	if ! [ -s "$build/index.html" ]; then
		echo "${build}/index.html is missing or empty — refusing to publish a build that would serve nothing." >&2
		return 1
	fi
	if ! [[ "$id" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
		echo "release id '${id}' is not a plain name." >&2
		return 1
	fi
	mkdir -p "$www/releases" || return 1
	if [ -e "$www/releases/$id" ]; then
		echo "release ${id} is already published; refusing to overwrite a release that may be live." >&2
		return 1
	fi
	# A copy, not a move: frontend/build belongs to the build container, and a
	# copy cut short only ever leaves a staging name that nothing serves.
	rm -rf "$www/releases/.staging-"* || return 1
	cp -R --preserve=mode,timestamps "$build" "$www/releases/.staging-$id" || return 1
	mv -T "$www/releases/.staging-$id" "$www/releases/$id" || return 1
	# Only a release that still exists is worth keeping as `previous`; a
	# dangling `current` names nothing to roll back to.
	if [ -L "$www/current" ] && [ -e "$www/current/index.html" ]; then
		prev=$(readlink "$www/current") || return 1
	fi
	ln -sfn "releases/$id" "$www/current.new" || return 1
	mv -T "$www/current.new" "$www/current" || return 1
	if [ -n "$prev" ] && [ "$prev" != "releases/$id" ]; then
		ln -sfn "$prev" "$www/previous.new" || return 1
		mv -T "$www/previous.new" "$www/previous" || return 1
	fi
	if [ -L "$www/previous" ]; then
		keep_prev=$(readlink "$www/previous") || return 1
	fi
	for rel in "$www"/releases/*; do
		[ -e "$rel" ] || continue
		case "releases/${rel##*/}" in
		"releases/$id" | "$keep_prev") ;;
		*) rm -rf "$rel" || return 1 ;;
		esac
	done
	echo "==> frontend: published release ${id}${prev:+ (previous: ${prev#releases/})}"
}

# feoh_caddy_reload makes the running Caddy serve the config on disk. Caddy
# mounts Caddyfile and tenants.caddy as single files, and a single-file bind
# mount pins the inode in the same way: a file replaced by rename — every git
# checkout that changes the Caddyfile, an editor that saves by rename — leaves
# the container reading the old one, and `caddy reload` then reloads the OLD
# config and reports success. So compare what the container sees with the host
# files first: identical → a zero-downtime reload; different (or unreadable) →
# recreate the container, which re-resolves every mount. Writers still rewrite
# these files in place (remove-tenant.sh does) so the reload is the usual path.
# Needs COMPOSE (feoh_load_db_mode); run from deploy/.
feoh_caddy_reload() {
	local f stale=""
	for f in Caddyfile tenants.caddy; do
		if ! "${COMPOSE[@]}" exec -T caddy cat "/etc/caddy/$f" 2>/dev/null | cmp -s - "$f"; then
			stale=$f
			break
		fi
	done
	if [ -n "$stale" ]; then
		echo "==> caddy: the running container does not see the current ${stale} (a file replaced on the host keeps its old inode in a bind mount) — recreating caddy"
		"${COMPOSE[@]}" up -d --no-deps --force-recreate --wait caddy || return 1
	else
		"${COMPOSE[@]}" exec -T caddy caddy reload --config /etc/caddy/Caddyfile || return 1
	fi
}

# feoh_smoke_check <host> <path> fetches https://<host><path> through THIS VM's
# Caddy — `--resolve <host>:443:127.0.0.1`, so it depends on neither public DNS
# nor the network hairpinning back to the VM's own public IP — under the real
# hostname, so SNI, the certificate and the site block are the ones users hit.
# It passes on HTTP 200 with a non-empty body: the v0.1.0 outage answered 404
# with an empty one while the API healthcheck stayed green. It retries for a
# while (FEOH_SMOKE_ATTEMPTS × FEOH_SMOKE_INTERVAL seconds, default 20 × 3): a
# just-recreated Caddy may still be loading its certificates, and on a first
# deploy issuing them.
feoh_smoke_check() {
	local host=$1 path=$2 attempts=${FEOH_SMOKE_ATTEMPTS:-20} interval=${FEOH_SMOKE_INTERVAL:-3}
	local url="https://${host}${path}" body err code="000" i
	body=$(mktemp) || return 1
	err=$(mktemp) || return 1
	for ((i = 1; i <= attempts; i++)); do
		# curl creates its -o file on the first byte, so an empty answer would
		# otherwise leave the previous attempt's body in place to be counted.
		: >"$body"
		code=$(curl -sS -o "$body" -w '%{http_code}' --max-time 10 \
			--resolve "${host}:443:127.0.0.1" "$url" 2>"$err") || true
		[ -n "$code" ] || code="000"
		if [ "$code" = 200 ] && [ -s "$body" ]; then
			rm -f "$body" "$err"
			echo "==> smoke: ${url} → 200"
			return 0
		fi
		if [ "$i" -lt "$attempts" ]; then
			sleep "$interval"
		fi
	done
	echo "smoke check failed: ${url} answered HTTP ${code} with $(wc -c <"$body" | tr -d ' ') byte(s) after ${attempts} attempt(s). $(head -c 300 "$err")" >&2
	rm -f "$body" "$err"
	return 1
}
