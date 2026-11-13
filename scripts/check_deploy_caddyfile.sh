#!/usr/bin/env bash
#
# Validate deploy/Caddyfile with the Caddy image deploy/compose.prod.yml pins,
# exactly as the VM mounts it, with one vanity host block so the `vanity`
# snippet is adapted too (docs/decisions.md §364).
#
# A Caddyfile Caddy refuses is an outage of the API host: deploy.sh's roll
# recreates Caddy with it, and nothing else on the VM answers :443. This runs
# the check in CI instead (the `deploy-compose` job), which also covers a
# Dependabot bump of the Caddy image: the image comes from the compose file, so
# the PR's own run validates the new digest against this Caddyfile.
#
# It works on a scratch copy of the two config files and runs the image with no
# network and no volumes (`docker run`, not `compose run`: the compose network
# pins a subnet that can collide with one already on the machine, and the
# compose volumes would be created for nothing). The image ref is read from
# compose.prod.yml, never restated here.
#
# Usage: scripts/check_deploy_caddyfile.sh [<repo-root>]
# Its test: backend/tests/test_deploy_release.py (the CI wiring).
set -euo pipefail

root=$(cd "${1:-$(dirname "$0")/..}" && pwd)
[ -f "$root/deploy/Caddyfile" ] || {
	echo "check_deploy_caddyfile: no deploy/Caddyfile under ${root}" >&2
	exit 2
}

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp "$root/deploy/Caddyfile" "$work/Caddyfile"
# What deploy.sh seeds, plus one vanity host as the runbook writes it.
cat "$root/deploy/tenants.caddy.example" >"$work/tenants.caddy"
printf '\nacme.acmecorp.example {\n\timport vanity\n}\n' >>"$work/tenants.caddy"

# A scratch deploy/ with a placeholder .env, only so compose can interpolate the
# file (the api's `env_file: .env` must exist) and list its images.
mkdir "$work/deploy"
cp "$root/deploy/compose.prod.yml" "$work/deploy/"
ln -s "$root/backend" "$work/backend"
printf '%s\n' 'APP_DOMAIN=x' 'API_DOMAIN=x' 'ACME_EMAIL=x' 'AWS_REGION=x' >"$work/deploy/.env"
images=$(cd "$work/deploy" && docker compose -f compose.prod.yml config --images)
image=$(printf '%s\n' "$images" | grep -E '^caddy:' || true)
[ "$(printf '%s\n' "$image" | grep -c .)" = 1 ] || {
	echo "check_deploy_caddyfile: expected exactly one caddy image in deploy/compose.prod.yml, found: ${image:-none}" >&2
	exit 2
}

echo "==> caddy validate with ${image}"
docker run --rm --network none \
	-e APP_DOMAIN=feohledger.example \
	-e API_DOMAIN=api.feohledger.example \
	-e ACME_EMAIL=ops@feohledger.example \
	-v "$work/Caddyfile:/etc/caddy/Caddyfile:ro" \
	-v "$work/tenants.caddy:/etc/caddy/tenants.caddy:ro" \
	"$image" caddy validate --adapter caddyfile --config /etc/caddy/Caddyfile
echo "deploy/Caddyfile adapts and validates with the pinned Caddy image."
