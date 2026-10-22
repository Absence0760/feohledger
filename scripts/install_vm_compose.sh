#!/usr/bin/env bash
#
# Install, for the current user, the Docker Compose plugin version the
# production VM runs, so CI checks deploy/ with the compose that will run it
# (#575 § Environment parity).
#
# The version is NOT written here. It is read from `COMPOSE_VERSION=` in
# deploy/bootstrap-vm.sh, the line that installs compose on the VM, so the two
# cannot disagree: bumping that pin is what moves CI too. #569 is why it
# matters: a compose warning that only the VM's v2.32.4 printed went unseen
# because every other machine ran a newer compose.
#
# Usage: scripts/install_vm_compose.sh              # install + verify
#        scripts/install_vm_compose.sh --print-version
#
# Installs to ${DOCKER_CONFIG:-~/.docker}/cli-plugins/docker-compose, which the
# docker CLI searches before the system plugin directories, then fails unless
# `docker compose version` reports exactly the pinned version. The download is
# checked against the release's published sha256. Linux only (CI and the VM).
# Its tests: backend/tests/test_deploy_compose_parity.py
set -euo pipefail

deploy=$(cd "$(dirname "$0")/../deploy" && pwd)
# shellcheck source=deploy/lib.sh
. "$deploy/lib.sh"

die() {
	echo "install_vm_compose: $*" >&2
	exit 1
}

# The same parser deploy.sh uses: exactly one vX.Y.Z, or a refusal.
version=$(feoh_compose_pin "$deploy/bootstrap-vm.sh") || die "no usable compose pin (see above)."

if [ "${1:-}" = --print-version ]; then
	printf '%s\n' "$version"
	exit 0
fi
[ $# -eq 0 ] || die "usage: $0 [--print-version]"

[ "$(uname -s)" = Linux ] || die "installs the linux binary the VM runs; this is $(uname -s)."
asset="docker-compose-linux-$(uname -m)"
url="https://github.com/docker/compose/releases/download/${version}/${asset}"
dest_dir="${DOCKER_CONFIG:-$HOME/.docker}/cli-plugins"
mkdir -p "$dest_dir"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl -fsSL --retry 3 -o "$work/$asset" "$url" || die "could not download ${url}"
curl -fsSL --retry 3 -o "$work/$asset.sha256" "$url.sha256" || die "could not download ${url}.sha256"
want=$(cut -d' ' -f1 <"$work/$asset.sha256")
got=$(sha256sum "$work/$asset" | cut -d' ' -f1)
if [ -z "$want" ] || [ "$want" != "$got" ]; then
	die "${asset} ${version}: sha256 ${got} does not match the published ${want}."
fi
install -m 0755 "$work/$asset" "$dest_dir/docker-compose"

running=$(docker compose version --short)
[ "v${running#v}" = "$version" ] ||
	die "installed ${version} to ${dest_dir}, but 'docker compose' runs ${running} — another plugin directory wins."
echo "docker compose ${version} (deploy/bootstrap-vm.sh's pin) installed to ${dest_dir}"
