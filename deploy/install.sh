#!/usr/bin/env bash
# Installiert oder aktualisiert Musterdepot auf Debian/Ubuntu (auch im LXC-Container).
# Als root im Projektverzeichnis ausführen:
#   ./deploy/install.sh
# Update: git pull && ./deploy/install.sh   (Daten und /etc/musterdepot.env bleiben erhalten)
set -euo pipefail

APP_DIR=/opt/musterdepot
DATA_DIR=/var/lib/musterdepot
ENV_FILE=/etc/musterdepot.env
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then echo "Bitte als root ausführen (z. B. mit sudo)." >&2; exit 1; fi
if ! command -v apt-get >/dev/null; then echo "Dieses Skript ist für Debian/Ubuntu gedacht." >&2; exit 1; fi

echo "==> Pakete installieren"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv rsync curl >/dev/null

echo "==> Systembenutzer 'musterdepot'"
id musterdepot &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin musterdepot

echo "==> Programmdateien nach $APP_DIR"
mkdir -p "$APP_DIR" "$DATA_DIR"
rsync -a --delete --exclude venv --exclude '__pycache__' "$SRC_DIR/app" "$SRC_DIR/requirements.txt" "$APP_DIR/"
[[ -x "$APP_DIR/venv/bin/python" ]] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"
chown -R musterdepot:musterdepot "$DATA_DIR"
chmod 750 "$DATA_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  cp "$SRC_DIR/deploy/musterdepot.env" "$ENV_FILE"
  chmod 640 "$ENV_FILE"
  chgrp musterdepot "$ENV_FILE"
  echo "    $ENV_FILE angelegt"
fi
cp "$SRC_DIR/deploy/musterdepot.service" /etc/systemd/system/musterdepot.service

echo "==> Dienst starten"
systemctl daemon-reload
systemctl enable musterdepot >/dev/null
systemctl restart musterdepot

PORT=$(. "$ENV_FILE"; echo "${MUSTERDEPOT_PORT:-8080}")
for _ in $(seq 1 30); do
  curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 1
done
if ! curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
  echo "Der Dienst antwortet nicht. Protokoll: journalctl -u musterdepot -n 50" >&2
  systemctl --no-pager --lines=20 status musterdepot || true
  exit 1
fi

if [[ -f "$DATA_DIR/initial-admin-password.txt" ]]; then
  echo
  echo "==> Erstes Admin-Konto:"
  cat "$DATA_DIR/initial-admin-password.txt"
  echo "(Nach dem ersten Login Passwort ändern und Datei löschen: rm $DATA_DIR/initial-admin-password.txt)"
fi
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Fertig: http://${IP:-localhost}:$PORT"
