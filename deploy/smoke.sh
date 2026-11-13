#!/usr/bin/env bash
# Post-deploy smoke check: is the deployed stack serving, and serving as it is
# configured? The last step of deploy.sh, and of the ECS release workflow
# (.github/workflows/aws-deploy.yml) — one script, so the two cannot drift
# (#575). Safe to run by hand at any time.
#
#   smoke.sh --vm
#       On the VM, from anywhere. Everything comes from deploy/.env and
#       deploy/tenants.caddy, and every request goes through THIS VM's Caddy
#       under the real hostname (lib.sh feoh_smoke_check), so public DNS is
#       not involved. It checks only what this VM serves: since
#       docs/decisions.md §364 the apex and the tenant subdomains are
#       CloudFront's, checked after each publish by smoke-frontend.sh, so the
#       apex is not fetched and the TLS check is a vanity host's.
#   smoke.sh --app <host> --api <host> --tenant <host> --signup true|false [--sitekey <key>]
#       From anywhere else, through public DNS. Every argument but --sitekey
#       is required: a check that is not asked for is not run.
#
# It checks, each with retries, and reports EVERY failure before exiting 1:
#   - the apex (https://<app>/) serves a page (not in --vm mode);
#   - https://<api>/api/health answers;
#   - a tenant host serves a page over a certificate curl accepts — the tenant
#     TLS path (in --vm mode, the first vanity host in tenants.caddy, which
#     this VM's Caddy issues for and proxies from CloudFront; a VM with none
#     says so instead of checking one);
#   - https://<api>/api/public-config reports signup exactly as configured,
#     and a captcha site key whenever signup is on (and, given --sitekey or
#     FEOH_HCAPTCHA_SITEKEY in deploy/.env, that key).
# Exit codes: 0 all passed, 1 a check failed, 2 bad usage or configuration.
set -euo pipefail
cd "$(dirname "$0")"

# shellcheck source=lib.sh
. ./lib.sh

usage() {
	echo "smoke.sh: $*" >&2
	echo "usage: smoke.sh --vm | smoke.sh --app <host> --api <host> --tenant <host> --signup true|false [--sitekey <key>]" >&2
	exit 2
}

VM=0 APP="" API="" TENANT="" SIGNUP="" SITEKEY=""
while [ $# -gt 0 ]; do
	case "$1" in
	--vm) VM=1 && shift ;;
	--app | --api | --tenant | --signup | --sitekey)
		[ $# -ge 2 ] || usage "$1 needs a value"
		case "$1" in
		--app) APP=$2 ;;
		--api) API=$2 ;;
		--tenant) TENANT=$2 ;;
		--signup) SIGNUP=$2 ;;
		--sitekey) SITEKEY=$2 ;;
		esac
		shift 2
		;;
	*) usage "unknown argument '$1'" ;;
	esac
done

command -v curl >/dev/null || usage "'curl' is not installed"
command -v python3 >/dev/null || usage "'python3' is not installed (it parses /api/public-config)"

if [ "$VM" = 1 ]; then
	[ -z "$APP$API$TENANT$SIGNUP$SITEKEY" ] || usage "--vm reads deploy/.env; it takes no other argument"
	[ -f .env ] || usage "deploy/.env missing — run deploy.sh first"
	API=$(feoh_env API_DOMAIN)
	SIGNUP=$(feoh_signup_enabled .env)
	SITEKEY=$(feoh_env FEOH_HCAPTCHA_SITEKEY)
	TENANT=$(feoh_first_vanity_host)
	[ -n "$API" ] || usage "API_DOMAIN missing from deploy/.env"
	export FEOH_SMOKE_RESOLVE=127.0.0.1
else
	if [ -z "$APP" ] || [ -z "$API" ] || [ -z "$TENANT" ] || [ -z "$SIGNUP" ]; then
		usage "--app, --api, --tenant and --signup are all required without --vm"
	fi
	export FEOH_SMOKE_RESOLVE=""
fi
case "$SIGNUP" in
true | false) ;;
*) usage "--signup is 'true' or 'false', not '${SIGNUP}'" ;;
esac

FAILED=()
fail() { FAILED+=("$1"); }

if [ "$VM" = 0 ]; then
	feoh_smoke_check "$APP" / || fail "apex https://${APP}/ does not serve a page"
fi
feoh_smoke_check "$API" /api/health || fail "https://${API}/api/health does not answer"
if [ -n "$TENANT" ]; then
	feoh_smoke_check "$TENANT" / ||
		fail "tenant host https://${TENANT}/ does not serve a page over a valid certificate"
else
	echo "NOTE: deploy/tenants.caddy lists no vanity host, so no TLS this VM issues beyond api. was checked; the apex and tenant hosts are CloudFront's (smoke-frontend.sh checks them after the publish)." >&2
fi

CONFIG=$(mktemp)
trap 'rm -f "$CONFIG"' EXIT
if feoh_smoke_check "$API" /api/public-config "$CONFIG"; then
	# Prints one line per mismatch; nothing when public-config agrees with the env.
	MISMATCH=$(python3 - "$CONFIG" "$SIGNUP" "$SITEKEY" <<-'PY'
		import json, sys
		path, signup, sitekey = sys.argv[1], sys.argv[2] == "true", sys.argv[3]
		try:
		    cfg = json.load(open(path))
		except ValueError:
		    print("is not JSON")
		    sys.exit()
		if not isinstance(cfg, dict):
		    print("is not a JSON object")
		    sys.exit()
		got = cfg.get("signup_enabled")
		if got is not signup:
		    print(f"reports signup_enabled={json.dumps(got)}, but the environment has it {'on' if signup else 'off'}")
		key = cfg.get("hcaptcha_sitekey")
		if signup and not (isinstance(key, str) and key.strip()):
		    print("has no hcaptcha_sitekey while signup is on, so /signup renders no captcha and every signup fails")
		elif signup and sitekey and key != sitekey:
		    print("serves a different hcaptcha_sitekey from the one configured")
	PY
	) || MISMATCH="could not be checked (python3 failed)"
	while IFS= read -r line; do
		[ -z "$line" ] || fail "https://${API}/api/public-config ${line}"
	done <<<"$MISMATCH"
else
	fail "https://${API}/api/public-config does not answer"
fi

if [ "${#FAILED[@]}" -gt 0 ]; then
	{
		echo
		echo "POST-DEPLOY SMOKE CHECK FAILED (${#FAILED[@]}):"
		printf '  - %s\n' "${FAILED[@]}"
	} >&2
	exit 1
fi
echo "==> smoke: every check passed (${APP:+apex, }API health, ${TENANT:-no vanity host}, public-config: signup ${SIGNUP})"
