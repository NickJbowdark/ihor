#!/usr/bin/env bash
# GIM server setup for a fresh Ubuntu 24.04 VPS (e.g. Hetzner Cloud). READ BEFORE RUNNING.
# Run as root:  bash setup_server.sh  -- it does NOT open any public port for the dashboard.
set -euo pipefail

REPO_URL="${1:?usage: setup_server.sh <git-repo-url> [branch]}"
BRANCH="${2:-main}"

apt-get update && apt-get -y upgrade
apt-get install -y python3 python3-venv python3-pip git ufw unattended-upgrades sqlite3

# Firewall: SSH only. The dashboard stays on 127.0.0.1 and is reached via Tailscale.
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

# Dedicated unprivileged user
id gim >/dev/null 2>&1 || useradd --system --create-home --home-dir /opt/gim --shell /usr/sbin/nologin gim
if [ ! -d /opt/gim/.git ]; then
  sudo -u gim git clone --branch "$BRANCH" "$REPO_URL" /opt/gim
fi
cd /opt/gim
sudo -u gim python3 -m venv .venv
sudo -u gim .venv/bin/pip install --upgrade pip
sudo -u gim .venv/bin/pip install -e .
sudo -u gim mkdir -p data logs reports/daily
if [ ! -f .env ]; then
  sudo -u gim cp .env.example .env
  chmod 600 .env
  echo ">>> Edit /opt/gim/.env now (user agent, AI provider, budget, dashboard password)."
fi

cp deploy/hetzner/*.service deploy/hetzner/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now gim-dashboard.service gim-collect.timer gim-backup.timer
echo ">>> Daily brief timer is NOT enabled automatically. When AI + e-mail are configured:"
echo "    systemctl enable --now gim-daily-brief.timer"

# Tailscale (private access from your phone/laptop, no public port):
echo ">>> Install Tailscale:  curl -fsSL https://tailscale.com/install.sh | sh && tailscale up"
echo ">>> Then publish the dashboard inside your tailnet only:  tailscale serve --bg 8501"
