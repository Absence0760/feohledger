#!/usr/bin/env bash
# One-time VM bootstrap for the minimal single-VM deployment
# (docs/minimal-deployment.md). Idempotent — safe to re-run.
#
# Targets Amazon Linux 2023 (the recommended AMI). On other distros it exits
# with the manual install list instead of guessing package names.
#
# Installs docker + the compose v2 plugin + sops + cronie (AL2023 ships no
# cron daemon), adds 2 GB swap, installs the nightly backup cron, and sets
# the IMDSv2 hop limit to 2 (containers cannot reach instance-profile
# credentials through Docker's NAT without it).
# Node/pnpm are NOT needed on the VM — deploy.sh builds the frontend inside a
# node:24 container.
set -euo pipefail
REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)

# Pinned versions — bump when new releases ship.
COMPOSE_VERSION=v2.32.4
SOPS_VERSION=3.9.4

if ! command -v dnf >/dev/null; then
	cat >&2 <<-'EOF'
		bootstrap-vm.sh targets Amazon Linux 2023. On this distro, install
		manually and re-run deploy.sh: docker + compose v2 plugin, git, sops,
		AWS CLI v2, 2 GB swap, and the backup cron (see deploy/README.md).
	EOF
	exit 1
fi

echo "==> packages (docker, git, cronie, aws cli)"
# cronie is NOT in the AL2023 base image (Amazon dropped cron for systemd
# timers) — without it /etc/cron.d/feoh-backup below is a file nothing reads
# and the nightly backup never runs.
sudo dnf install -y docker git cronie
command -v aws >/dev/null || sudo dnf install -y awscli-2
sudo systemctl enable --now docker
sudo systemctl enable --now crond
sudo usermod -aG docker "$USER"

if ! docker compose version >/dev/null 2>&1 && ! sudo docker compose version >/dev/null 2>&1; then
	echo "==> docker compose plugin ${COMPOSE_VERSION}"
	ARCH=$(uname -m) # x86_64 | aarch64
	sudo mkdir -p /usr/local/lib/docker/cli-plugins
	sudo curl -fsSL \
		"https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${ARCH}" \
		-o /usr/local/lib/docker/cli-plugins/docker-compose
	sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

if ! command -v sops >/dev/null; then
	echo "==> sops ${SOPS_VERSION} (rpm from GitHub releases — no dnf package exists)"
	sudo dnf install -y \
		"https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-${SOPS_VERSION}-1.$(uname -m).rpm"
fi

echo "==> automatic security updates (dnf-automatic)"
# An unattended pilot VM never gets OS patches otherwise. Security-only and
# auto-applied; docker/containerd are excluded because their package scripts
# restart the daemon — bouncing the whole stack at a random hour. NOTE: the
# exclude removes them from dnf-automatic's reporting too, so nothing nudges
# you about their pending patches — update them deliberately around deploy
# windows (see deploy/README.md § Deploys).
sudo dnf install -y dnf-automatic
sudo tee /etc/dnf/automatic.conf >/dev/null <<-'EOF'
	[commands]
	upgrade_type = security
	random_sleep = 3600
	apply_updates = yes

	[emitters]
	emit_via = stdio

	[base]
	exclude = docker* containerd*
EOF
sudo systemctl enable --now dnf-automatic.timer

if ! swapon --show | grep -q .; then
	echo "==> 2 GB swap"
	sudo fallocate -l 2G /swapfile
	sudo chmod 600 /swapfile
	sudo mkswap /swapfile
	sudo swapon /swapfile
	echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> nightly backup cron (/etc/cron.d/feoh-backup)"
# Explicit PATH: crond's default is /usr/bin:/bin, which misses aws when it
# was installed from the official zip (/usr/local/bin — the non-AL2023 path).
sudo tee /etc/cron.d/feoh-backup >/dev/null <<-EOF
	PATH=/usr/local/bin:/usr/bin:/bin
	17 3 * * * $USER $REPO_ROOT/deploy/backup.sh >> /var/log/feoh-backup.log 2>&1
EOF
sudo touch /var/log/feoh-backup.log
sudo chown "$USER" /var/log/feoh-backup.log

# IMDSv2 hop limit: 1 (the default) stops containers one NAT hop away from
# reaching instance-profile credentials. Harmless to re-apply.
TOKEN=$(curl -sf -X PUT http://169.254.169.254/latest/api/token \
	-H 'X-aws-ec2-metadata-token-ttl-seconds: 60' || true)
if [ -n "$TOKEN" ]; then
	IID=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
	REGION=$(curl -sf -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/placement/region)
	if aws ec2 modify-instance-metadata-options --instance-id "$IID" --region "$REGION" \
		--http-tokens required --http-put-response-hop-limit 2 >/dev/null 2>&1; then
		echo "==> IMDSv2 hop limit set to 2"
	else
		echo "WARN: could not set the IMDSv2 hop limit (instance profile lacks ec2:ModifyInstanceMetadataOptions)." >&2
		echo "      Run this from a machine with EC2 admin rights or containers cannot use the instance profile:" >&2
		echo "      aws ec2 modify-instance-metadata-options --instance-id $IID --region $REGION --http-tokens required --http-put-response-hop-limit 2" >&2
	fi
else
	echo "WARN: no EC2 instance metadata reachable — skipping the IMDSv2 hop-limit step (fine off-EC2, e.g. Hetzner)." >&2
fi

cat <<-EOF

	bootstrap complete. Next steps:
	  1. Log out and back in (docker group membership).
	  2. Copy infra-secrets' feohledger/prod.sops.yaml here as deploy/prod.sops.yaml
	     (template: deploy/prod.sops.yaml.example), then check it: ./decrypt-env.sh
	  3. ./deploy.sh
	  4. ./add-tenant.sh <slug> --name "Company" --admin-email admin@company.com
	EOF
