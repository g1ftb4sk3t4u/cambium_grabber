#!/usr/bin/env bash
# Native CentOS install: no Docker required. Sets up a dedicated user, a venv
# under /opt/cambium-grabber, and a systemd timer that runs a headless daily
# check for new Cambium releases. Run the historical backfill once by hand
# afterwards (see the printed instructions at the end).
#
# Usage: sudo ./install.sh [/path/to/repo/checkout]

set -euo pipefail

REPO_SRC="${1:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
INSTALL_DIR="/opt/cambium-grabber"
ARCHIVE_DIR="/data/cambium_archive"
SERVICE_USER="cambium-grabber"

if [[ $EUID -ne 0 ]]; then
    echo "Run as root (sudo ./install.sh)" >&2
    exit 1
fi

# CentOS 7 went EOL in June 2024 - mirrorlist.centos.org is gone, so a fresh
# box's default repo config 404s on every yum/dnf call ("Cannot find a valid
# baseurl for repo: base/7/x86_64") before it ever gets to installing
# anything. Point it at vault.centos.org instead, idempotently. (Learned the
# hard way building the sibling MikroTik grabber - see that project's
# install.sh for the same fix.)
if [[ -f /etc/centos-release ]] && grep -q "release 7" /etc/centos-release && \
   [[ -f /etc/yum.repos.d/CentOS-Base.repo ]] && grep -q "^mirrorlist=" /etc/yum.repos.d/CentOS-Base.repo; then
    echo "==> CentOS 7 detected: repointing yum repos at vault.centos.org (mirrorlist.centos.org is dead post-EOL)"
    sed -i \
        -e 's|^mirrorlist=|#mirrorlist=|g' \
        -e 's|^#baseurl=http://mirror.centos.org|baseurl=http://vault.centos.org|g' \
        /etc/yum.repos.d/CentOS-Base.repo
fi

echo "==> Installing system packages"
# python3 -m venv (stdlib) is what's actually used below - CentOS 7/8's
# python3 is old enough (3.6) that this codebase avoids anything needing
# newer syntax (no `from __future__ import annotations`, which is a hard
# SyntaxError on 3.6 - also learned from the MikroTik grabber).
if command -v dnf >/dev/null 2>&1; then
    dnf install -y python3 python3-pip sudo rsync
else
    yum install -y python3 python3-pip sudo rsync
fi

echo "==> Creating service user"
id -u "$SERVICE_USER" &>/dev/null || useradd --system --home-dir "$INSTALL_DIR" --shell /sbin/nologin "$SERVICE_USER"

echo "==> Syncing application code to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rsync -a --delete \
    --exclude='.git' --exclude='cambium_archive' --exclude='venv' --exclude='.venv' --exclude='__pycache__' \
    "$REPO_SRC/" "$INSTALL_DIR/"

echo "==> Creating archive directory: $ARCHIVE_DIR"
mkdir -p "$ARCHIVE_DIR"
chown -R "$SERVICE_USER":"$SERVICE_USER" "$ARCHIVE_DIR" "$INSTALL_DIR"

echo "==> Creating virtualenv"
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo "==> Creating credentials file (fill this in before starting the timer!)"
CREDS_FILE="$INSTALL_DIR/credentials.env"
if [[ ! -f "$CREDS_FILE" ]]; then
    cat > "$CREDS_FILE" <<'EOF'
# Cambium account used for automated logins. This file is read by systemd's
# EnvironmentFile= - keep it out of git, keep it mode 600.
CAMBIUM_EMAIL=
CAMBIUM_PASSWORD=
EOF
fi
chown "$SERVICE_USER":"$SERVICE_USER" "$CREDS_FILE"
chmod 600 "$CREDS_FILE"

echo "==> Installing systemd units"
cp "$INSTALL_DIR/deploy/centos/cambium-grabber-watch.service" /etc/systemd/system/
cp "$INSTALL_DIR/deploy/centos/cambium-grabber-watch.timer" /etc/systemd/system/
cp "$INSTALL_DIR/deploy/centos/cambium-grabber-fullscan.service" /etc/systemd/system/
sed -i "s#/data/cambium_archive#${ARCHIVE_DIR}#g" /etc/systemd/system/cambium-grabber-watch.service /etc/systemd/system/cambium-grabber-fullscan.service

systemctl daemon-reload

echo "==> Allowing outbound HTTPS through firewalld (if active)"
if command -v firewall-cmd >/dev/null 2>&1 && systemctl is-active --quiet firewalld; then
    firewall-cmd --zone=public --add-service=https --permanent
    firewall-cmd --reload
fi

cat <<EOF

Done - but the timer is NOT enabled yet.

1. Fill in your Cambium account credentials:
     sudo \$EDITOR $CREDS_FILE

2. Verify login works:
     sudo -u $SERVICE_USER bash -c 'set -a; source $CREDS_FILE; set +a; $INSTALL_DIR/venv/bin/python $INSTALL_DIR/run.py login --output $ARCHIVE_DIR'

3. Enable the daily timer:
     sudo systemctl enable --now cambium-grabber-watch.timer
     systemctl list-timers cambium-grabber-watch.timer

Run the one-time historical backfill (current releases download first,
archives after - see README):
  sudo systemctl start cambium-grabber-fullscan.service
  sudo journalctl -u cambium-grabber-fullscan.service -f

Trigger a check manually:
  sudo systemctl start cambium-grabber-watch.service
  sudo journalctl -u cambium-grabber-watch.service -f

Archive lives at: $ARCHIVE_DIR
Credentials file: $CREDS_FILE (mode 600, not in git)
EOF
