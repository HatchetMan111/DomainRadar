#!/usr/bin/env bash
# DomainRadar – inneres Installationsskript, läuft im LXC.
# Versions-Marker: DOMAINRADAR-INNER-VERSION 1
set -Eeuo pipefail

APP_DIR="${APP_DIR:-/opt/domainradar}"
APP_USER="${APP_USER:-domainradar}"
APP_PORT="${APP_PORT:-8477}"
TZ_NAME="${TZ:-Europe/Berlin}"

msg_info() { echo -e " \033[34mℹ\033[0m $*"; }
msg_ok()   { echo -e " \033[32m✓\033[0m $*"; }
msg_error(){ echo -e " \033[31m✗\033[0m $*" >&2; }

on_error() {
  local rc=$1 line=$2
  msg_error "Fehler im Inner-Skript (Exit $rc, Zeile $line): $BASH_COMMAND"
  exit "$rc"
}
trap 'on_error $? $LINENO' ERR

msg_info "Stelle sicher, dass DNS funktioniert"
for _ in 1 2 3; do
  if getent hosts deb.debian.org >/dev/null 2>&1; then break; fi
  sleep 2
done

msg_info "Installiere Pakete"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
  python3 python3-venv python3-pip sqlite3 curl ca-certificates \
  nodejs npm >/dev/null
if [ "${INSTALL_OLLAMA:-0}" = "1" ]; then
  msg_info "Installiere Ollama (CPU-Inferenz)"
  curl -fsSL https://ollama.com/install.sh | sh >/dev/null 2>&1 || \
    msg_error "Ollama-Installation fehlgeschlagen – Stufe 1 der Kaskade bleibt aus"
fi

msg_info "Lege Nutzer $APP_USER an"
if ! id "$APP_USER" >/dev/null 2>&1; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

msg_info "App-Dateien nach $APP_DIR"
mkdir -p "$APP_DIR"/{src,web,data}
cp -r /root/domainradar-src/. "$APP_DIR"/
rm -rf "$APP_DIR"/.env

msg_info "Python-Umgebung"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip >/dev/null
"$APP_DIR/venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"

if [ -f "$APP_DIR/web/package.json" ]; then
  msg_info "Baue Web-Oberfläche (React + Vite)"
  cd "$APP_DIR/web"
  npm ci --silent >/dev/null 2>&1 || npm install --silent >/dev/null
  npm run build --silent >/dev/null
  cd "$APP_DIR"
fi

msg_info "Konfiguration (.env)"
cat > "$APP_DIR/.env" <<EOF
DOMAINRADAR_DB=$APP_DIR/data/domainradar.db
DOMAINRADAR_PORT=$APP_PORT
DOMAINRADAR_WEB=$APP_DIR/web/dist
LLM_CHAIN=${LLM_CHAIN:-ollama,openrouter}
OLLAMA_BASE_URL=${OLLAMA_BASE_URL:-http://127.0.0.1:11434/v1}
OLLAMA_MODEL=${OLLAMA_MODEL:-llama3.2:3b}
OPENROUTER_MODEL=${OPENROUTER_MODEL:-deepseek/deepseek-chat}
OPENROUTER_API_KEY=${OPENROUTER_API_KEY:-}
EOF
chmod 600 "$APP_DIR/.env"

if [ "${INSTALL_OLLAMA:-0}" = "1" ]; then
  msg_info "Lade Standardmodell (llama3.2:3b)"
  ollama pull "${OLLAMA_MODEL:-llama3.2:3b}" >/dev/null 2>&1 || \
    msg_error "Modell konnte nicht geladen werden – Klassifikation nutzt dann OpenRouter"
fi

msg_info "Berechtigungen"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

msg_info "Systemd-Einheiten"
cp "$APP_DIR/systemd/"*.service "$APP_DIR/systemd/"*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now domainradar-api.service >/dev/null 2>&1
for t in collect enrich classify changes; do
  systemctl enable --now "domainradar-$t.timer" >/dev/null 2>&1 || true
done

msg_info "Warte auf API"
for _ in $(seq 1 30); do
  if curl -fs -m 2 "http://127.0.0.1:$APP_PORT/api/health" >/dev/null 2>&1; then
    msg_ok "API antwortet auf Port $APP_PORT"
    break
  fi
  sleep 1
done

# TZ setzen, damit Timercorrecte Zeiten melden
if [ -n "${TZ_NAME:-}" ]; then
  ln -snf "/usr/share/zoneinfo/$TZ_NAME" /etc/localtime 2>/dev/null || true
  echo "$TZ_NAME" > /etc/timezone 2>/dev/null || true
fi

msg_ok "Installation abgeschlossen"