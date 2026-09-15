#!/usr/bin/env bash
# Decrypt the VM's secrets file into deploy/.env, and check it before it
# replaces the one already there. deploy.sh runs this on every deploy; run it
# on its own after copying in a new or edited secrets file, to find a problem
# before a deploy does.
#
# deploy/prod.sops.yaml is a copy of infra-secrets/feohledger/prod.sops.yaml:
# flat YAML whose keys are the env var names (template:
# deploy/prod.sops.yaml.example). sops converts it to the dotenv that compose's
# env_file and the other deploy scripts read (docs/decisions.md §171).
#
# Usage: decrypt-env.sh
set -euo pipefail
cd "$(dirname "$0")"

die() {
	echo "decrypt-env.sh: $*" >&2
	exit 1
}

command -v sops >/dev/null || die "'sops' not installed — run deploy/bootstrap-vm.sh first."
[ -f prod.sops.yaml ] || die "prod.sops.yaml missing — copy infra-secrets/feohledger/prod.sops.yaml here (template: deploy/prod.sops.yaml.example; see deploy/README.md)."

# Decrypt fresh on every run (KMS access via the instance profile) into a temp
# file, check that, and only then move it into place. `sops -d >.env` would
# truncate .env before decrypting, and moving before checking would let a bad
# edit replace a working file — either way the nightly backup.sh and
# add-tenant.sh, which read .env between deploys, would break until the next
# good run.
umask 077
trap 'rm -f .env.tmp' EXIT
sops -d --input-type yaml --output-type dotenv prod.sops.yaml >.env.tmp

# sops emits every value unquoted, and compose's env_file parser then rewrites
# some without a word: an unquoted ` #` starts a comment (the rest of the value
# is dropped) and `$` interpolates (`x$yz` becomes `x`). A multi-line YAML value
# arrives flattened to a literal `\n`. Refuse all three, naming the key and never
# the value — this output lands in a terminal and a log.
MANGLED=$(grep -E '^[A-Za-z_][A-Za-z0-9_]*=.*([[:space:]]#|\$|\\n)' .env.tmp | cut -d= -f1 | tr '\n' ' ' || true)
[ -z "$MANGLED" ] ||
	die "compose would silently change the value of: ${MANGLED}(a ' #', a '\$' or a line break) — fix them in prod.sops.yaml."

# Everything compose interpolation / the app cannot default sensibly, plus
# the vars the app hard-refuses to boot without in a deployed env
# (FEOH_ENVIRONMENT=production arms those boot checks; FEOH_HCAPTCHA_SECRET
# is one of them) and the two S3 buckets uploads/backups silently need.
MISSING=""
for var in POSTGRES_PASSWORD APP_DOMAIN API_DOMAIN ACME_EMAIL AWS_REGION \
	FEOH_SECRET_KEY FEOH_ENVIRONMENT FEOH_S3_BUCKET BACKUP_S3_BUCKET FEOH_HCAPTCHA_SECRET; do
	grep -Eq "^${var}=.+" .env.tmp || MISSING="$MISSING $var"
done
[ -z "$MISSING" ] || die "required var(s) missing/empty in prod.sops.yaml:$MISSING (contract: deploy/prod.sops.yaml.example)"

# The app refuses to boot on a weak JWT key (config.py
# _require_real_secret_key_in_deployed_envs: not the default, >= 32 chars).
# The presence loop above passes any non-empty value, so a short key would
# survive and only surface ~5 minutes into a deploy as an `up -d --wait`
# healthcheck timeout, after the frontend build, image build, and migrations
# have all run. Mirror the boot rule here so it fails in the first second.
SECRET_KEY_VALUE=$(grep -E '^FEOH_SECRET_KEY=' .env.tmp | tail -1 | cut -d= -f2- || true)
case "$SECRET_KEY_VALUE" in
change-me-in-production)
	die "FEOH_SECRET_KEY is still the public default — generate one with 'openssl rand -hex 32'." ;;
esac
[ "${#SECRET_KEY_VALUE}" -ge 32 ] ||
	die "FEOH_SECRET_KEY is ${#SECRET_KEY_VALUE} chars; the app refuses to boot below 32 (openssl rand -hex 32)."

# Invoice extraction is the one adapter with no mock fallback in a deployed env
# (services/extraction.py resolve_platform_provider): with neither var set,
# every upload's extraction fails. Keying invoices in by hand is a legitimate
# choice, so this warns rather than refuses — deploy/prod.sops.yaml.example
# § Invoice extraction.
if ! grep -Eq '^(FEOH_ANTHROPIC_API_KEY|FEOH_EXTRACTION_PROVIDER)=.+' .env.tmp; then
	echo "WARN: neither FEOH_ANTHROPIC_API_KEY nor FEOH_EXTRACTION_PROVIDER is set — every invoice upload's extraction will fail (manual entry only). See deploy/prod.sops.yaml.example § Invoice extraction." >&2
fi

mv .env.tmp .env
echo "decrypt-env.sh: deploy/.env written and checked."
