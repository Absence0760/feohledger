#!/usr/bin/env bash
# The day-one stack (#575): an EMPTY database taken through exactly what a
# production deploy runs, then an API booted with production-shaped settings,
# for the Playwright suite in frontend/tests-e2e/day-one/ to drive.
#
# Exactly what a deploy runs, by running the same files rather than a copy of
# their command lines:
#   - migrations: backend/scripts/deploy_migrate.sh, the file deploy/deploy.sh
#     runs in the api container (and aws-deploy.yml in its Fargate task);
#   - provisioning: the command deploy/lib.sh::feoh_tenant_argv
#     builds, which deploy/add-tenant.sh execs into the api container;
#   - the API: the production image (backend/Dockerfile) and its entrypoint,
#     `uvicorn app.main:app` — not main.py, which loads .env.development.
#
# Usage: scripts/day-one/stack.sh <command>
#   up        start Postgres (empty), Redis, MinIO, Mailpit, LocalStack (+ KMS key)
#   build     build the api image from backend/Dockerfile
#   migrate   run the deploy's migration step (against an empty database first)
#   no-op     run the migration step AGAIN and fail unless it changed nothing
#   serve     start the API container and wait for /api/health
#   provision provision the first tenant the way add-tenant.sh does, and write
#             its admin login for the suite to $DAY_ONE_STATE_DIR/tenant.json
#   all       up, build, migrate, serve, provision, no-op — in that order
#   logs      the API container's log
#   down      remove the API container and the stack, data included
#
# Needs docker (compose v2.24+, for the override file's !reset / !override),
# curl, jq and openssl. Ports are the DAY_ONE_* variables below; the defaults
# avoid the everyday `pnpm db:up` stack, so both can run at once.
set -euo pipefail

REPO_ROOT=$(cd "$(dirname "$0")/../.." && pwd)
cd "$REPO_ROOT"

PG_PORT=${DAY_ONE_PG_PORT:-55432}
REDIS_PORT=${DAY_ONE_REDIS_PORT:-56379}
S3_PORT=${DAY_ONE_S3_PORT:-59000}
SMTP_PORT=${DAY_ONE_SMTP_PORT:-51025}
MAILPIT_PORT=${DAY_ONE_MAILPIT_PORT:-58025}
AWS_PORT=${DAY_ONE_AWS_PORT:-54566}
API_PORT=${DAY_ONE_API_PORT:-8000}
WEB_PORT=${DAY_ONE_WEB_PORT:-7777}
export DAY_ONE_PG_PORT=$PG_PORT DAY_ONE_REDIS_PORT=$REDIS_PORT DAY_ONE_S3_PORT=$S3_PORT \
	DAY_ONE_SMTP_PORT=$SMTP_PORT DAY_ONE_MAILPIT_PORT=$MAILPIT_PORT DAY_ONE_AWS_PORT=$AWS_PORT

# The first tenant, as an operator would type it into add-tenant.sh.
SLUG=${DAY_ONE_SLUG:-dayone}
TENANT_NAME=${DAY_ONE_TENANT_NAME:-Day One Trading Co}
ADMIN_EMAIL=${DAY_ONE_ADMIN_EMAIL:-owner@dayone-trading.example.com}

STATE_DIR=${DAY_ONE_STATE_DIR:-$REPO_ROOT/.day-one-state}
IMAGE=feoh-day-one-api
API_CONTAINER=feoh-day-one-api
KMS_ALIAS=alias/feohledger-app-local
# The profiles are named on every call, not only on `up`: naming a service on
# `up` starts it whatever its profile, but `down` only removes the services of
# the active profiles, and would leave Mailpit (with the last run's inbox) and
# LocalStack running into the next one.
COMPOSE=(docker compose -p feoh-day-one -f backend/docker-compose.yml -f scripts/day-one/compose.yml
	--profile mail --profile aws)
SERVICES=(postgres redis minio mailpit localstack)

die() {
	echo "day-one: $*" >&2
	exit 1
}

# Poll until a command succeeds — the readiness signal is the command itself.
wait_for() {
	local what=$1 attempts=$2
	shift 2
	local i
	for ((i = 1; i <= attempts; i++)); do
		if "$@" >/dev/null 2>&1; then
			echo "==> ${what} ready"
			return 0
		fi
		sleep 2
	done
	echo "day-one: ${what} did not become ready" >&2
	return 1
}

# The API's environment: production-shaped, which is the point. Every value a
# deployed environment's boot validators insist on is set the way
# deploy/prod.sops.yaml.example sets it, pointed at the local stand-ins —
# LocalStack for KMS, MinIO for S3, Mailpit for SMTP (a deployed environment
# refuses the `console` email adapter). Keys are generated per run, so nothing
# here is a committed secret. FEOH_CORS_ORIGINS names the browser's origins
# explicitly: a deployed environment admits no loopback origin by default
# (backend/app/main.py::_cors_allow_origins), and only an explicit list keeps one.
write_env() {
	mkdir -p "$STATE_DIR"
	chmod 700 "$STATE_DIR"
	if [ -f "$STATE_DIR/api.env" ]; then
		return 0
	fi
	(
		umask 077
		cat >"$STATE_DIR/api.env" <<EOF
FEOH_ENVIRONMENT=production
FEOH_DATABASE_URL=postgresql+asyncpg://postgres:postgres@127.0.0.1:${PG_PORT}/feohledger
FEOH_REDIS_URL=redis://127.0.0.1:${REDIS_PORT}
FEOH_SECRET_KEY=$(openssl rand -hex 32)
FEOH_APPROVAL_SIGNING_KEY=$(openssl rand -hex 32)
FEOH_CREDENTIAL_KMS_KEY_ID=${KMS_ALIAS}
FEOH_AWS_ENDPOINT_URL=http://127.0.0.1:${AWS_PORT}
AWS_ACCESS_KEY_ID=test
AWS_SECRET_ACCESS_KEY=test
AWS_DEFAULT_REGION=us-east-1
FEOH_S3_ENDPOINT_URL=http://127.0.0.1:${S3_PORT}
FEOH_S3_ACCESS_KEY=minioadmin
FEOH_S3_SECRET_KEY=minioadmin
FEOH_S3_BUCKET=invoices
FEOH_PUBLIC_URL=http://localhost:${WEB_PORT}
FEOH_API_PUBLIC_URL=http://localhost:${API_PORT}
FEOH_TENANT_URL_TEMPLATE=http://{slug}.localhost:${WEB_PORT}
FEOH_CORS_ORIGINS=["http://${SLUG}.localhost:${WEB_PORT}","http://localhost:${WEB_PORT}"]
FEOH_MFA_ENABLED=true
FEOH_HSTS_ENABLED=true
FEOH_SIGNUP_ENABLED=false
FEOH_DEPLOYED_REGION=us
FEOH_EMAIL_PROVIDER=smtp
FEOH_SMTP_HOST=127.0.0.1
FEOH_SMTP_PORT=${SMTP_PORT}
FEOH_EMAIL_FROM=no-reply@localhost
FEOH_SUBPROCESSOR_NOTICES_ENABLED=true
EOF
	)
}

# Run something in the api image, on the host network (the stack's ports are
# published on loopback), with the API's environment.
in_image() {
	docker run --rm --network host --env-file "$STATE_DIR/api.env" "$IMAGE" "$@"
}

cmd_up() {
	"${COMPOSE[@]}" up -d --wait "${SERVICES[@]}"
	wait_for "LocalStack KMS key ${KMS_ALIAS}" 60 \
		"${COMPOSE[@]}" exec -T localstack awslocal kms describe-key --key-id "$KMS_ALIAS" || exit 1
	wait_for "Mailpit" 30 curl -fsS "http://127.0.0.1:${MAILPIT_PORT}/api/v1/info" || exit 1
	# Empty means empty: the control plane database the deploy is about to
	# initialise holds no tables at all, and no tenant database exists.
	local tables dbs
	tables=$("${COMPOSE[@]}" exec -T postgres psql -U postgres -d feohledger -tAc \
		"SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'")
	dbs=$("${COMPOSE[@]}" exec -T postgres psql -U postgres -d postgres -tAc \
		"SELECT count(*) FROM pg_database WHERE datname LIKE 'feoh\\_%'")
	if [ "$tables" != 0 ] || [ "$dbs" != 0 ]; then
		die "Postgres is not empty (${tables} control-plane table(s), ${dbs} tenant database(s)) — run '$0 down' first."
	fi
	echo "==> Postgres is empty"
	# So is the inbox: the suite reads the invitation and reset emails from it.
	local mail
	mail=$(curl -fsS "http://127.0.0.1:${MAILPIT_PORT}/api/v1/messages?limit=1" | jq -r .total)
	[ "$mail" = 0 ] || die "Mailpit already holds ${mail} message(s) — run '$0 down' first."
}

cmd_build() {
	docker build -t "$IMAGE" backend
}

cmd_migrate() {
	write_env
	echo "==> the deploy's migration step (backend/scripts/deploy_migrate.sh)"
	in_image sh scripts/deploy_migrate.sh
}

# Every database's schema plus its Alembic version — what a second migration
# run must leave exactly as it found.
schema_snapshot() {
	local db
	for db in $("${COMPOSE[@]}" exec -T postgres psql -U postgres -d postgres -tAc \
		"SELECT datname FROM pg_database WHERE datname = 'feohledger' OR datname LIKE 'feoh\\_%' ORDER BY datname"); do
		echo "-- database ${db}"
		"${COMPOSE[@]}" exec -T postgres pg_dump -U postgres --schema-only --no-owner "$db" |
			grep -v '^-- Dumped \|^\\restrict \|^\\unrestrict '
		"${COMPOSE[@]}" exec -T postgres psql -U postgres -d "$db" -tAc \
			"SELECT 'alembic_version=' || version_num FROM alembic_version"
	done
}

cmd_no_op() {
	write_env
	local before after out
	before=$(mktemp)
	after=$(mktemp)
	out=$(mktemp)
	schema_snapshot >"$before"
	grep -q '^-- database feoh_' "$before" ||
		die "no tenant database to check — provision one first, so the re-run covers migrate_all_tenants.py too."
	echo "==> the deploy's migration step, again — it must change nothing"
	in_image sh scripts/deploy_migrate.sh 2>&1 | tee "$out"
	schema_snapshot >"$after"
	if ! diff -u "$before" "$after"; then
		die "the second migration run changed the schema (diff above): a deploy is not idempotent."
	fi
	if grep -q 'Running upgrade' "$out"; then
		die "the second migration run applied a revision (see 'Running upgrade' above)."
	fi
	grep -q '^Control plane already initialised\.$' "$out" ||
		die "init_control_plane.py did not report an initialised control plane on the second run."
	echo "==> second migration run was a no-op ($(grep -c '^-- database ' "$after") database(s) unchanged)"
	rm -f "$before" "$after" "$out"
}

cmd_serve() {
	write_env
	docker rm -f "$API_CONTAINER" >/dev/null 2>&1 || true
	# The image's own entrypoint, `uvicorn app.main:app`, on the port the suite
	# expects. Bound to loopback: host networking would otherwise expose it.
	docker run -d --name "$API_CONTAINER" --network host --env-file "$STATE_DIR/api.env" \
		"$IMAGE" uvicorn app.main:app --host 127.0.0.1 --port "$API_PORT" >/dev/null
	if ! wait_for "API on :${API_PORT}" 60 curl -fsS "http://127.0.0.1:${API_PORT}/api/health"; then
		docker logs "$API_CONTAINER" >&2 || true
		exit 1
	fi
}

cmd_provision() {
	write_env
	# shellcheck source=deploy/lib.sh
	. deploy/lib.sh
	# No --admin-password: the operator's path in #566, a generated temporary
	# password whose change is forced at first login.
	feoh_tenant_argv "$SLUG" "$TENANT_NAME" "$ADMIN_EMAIL" "" || die "could not build the provisioning command."
	docker exec "$API_CONTAINER" "${FEOH_TENANT_ARGV[@]}"
	[ "$FEOH_TENANT_FORCED" = 1 ] || die "add-tenant's default path should force a password change."
	(
		umask 077
		jq -n --arg slug "$SLUG" --arg name "$TENANT_NAME" --arg email "$ADMIN_EMAIL" \
			--arg password "$FEOH_TENANT_PASSWORD" \
			--arg mailpit "http://127.0.0.1:${MAILPIT_PORT}" \
			'{slug: $slug, name: $name, admin_email: $email, admin_temp_password: $password, mailpit_url: $mailpit}' \
			>"$STATE_DIR/tenant.json"
	)
	echo "==> tenant '${SLUG}' provisioned; admin login written to ${STATE_DIR}/tenant.json"
}

cmd_logs() {
	docker logs "$API_CONTAINER"
}

cmd_down() {
	docker rm -f "$API_CONTAINER" >/dev/null 2>&1 || true
	"${COMPOSE[@]}" down -v --remove-orphans
	rm -rf "$STATE_DIR"
}

case "${1:-}" in
up) cmd_up ;;
build) cmd_build ;;
migrate) cmd_migrate ;;
no-op) cmd_no_op ;;
serve) cmd_serve ;;
provision) cmd_provision ;;
all)
	cmd_up
	cmd_build
	cmd_migrate
	cmd_serve
	cmd_provision
	cmd_no_op
	;;
logs) cmd_logs ;;
down) cmd_down ;;
*)
	sed -n '/^# Usage:/,/^# Needs/p' "$0" | sed 's/^# \{0,1\}//' >&2
	exit 2
	;;
esac
