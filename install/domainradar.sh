#!/usr/bin/env bash
# DomainRadar – Proxmox-VE-Installer im Stil der Community Scripts.
#
# Einzeiler:
#   bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/DomainRadar-Proxmox/main/install/domainradar.sh)"
#
# Optionen:
#   --ctid <id>      Container-ID (Standard: nächste freie)
#   --update         vorhandenen Container aktualisieren
#   --status         Status anzeigen
#   --uninstall      Container entfernen (fragt nach, --yes überspringt)
#   --cron           cron statt systemd-Timern verwenden
#   --no-ollama      Ollama nicht installieren
#   --debug          bash -x im Inner-Skript
#   --help           diese Hilfe
set -Eeuo pipefail

APP_NAME="DomainRadar"
APP_HOSTNAME="domainradar"
APP_PORT="8477"
REPO_RAW="https://raw.githubusercontent.com/HatchetMan111/DomainRadar-Proxmox/main"
DEFAULT_CORES=2
DEFAULT_RAM=2048
DEFAULT_SWAP=512
DEFAULT_DISK=8
HEAVY_CORES=4
HEAVY_RAM=8192
HEAVY_DISK=24

CTID=""
MODE="install"
USE_CRON=0
INSTALL_OLLAMA=0
DEBUG=0
ASSUME_YES=0

CTID="${CTID:-}"
CORES="${CORES:-$DEFAULT_CORES}"
RAM="${RAM:-$DEFAULT_RAM}"
SWAP="${SWAP:-$DEFAULT_SWAP}"
DISK="${DISK:-$DEFAULT_DISK}"
STORAGE="${STORAGE:-}"
TZ_NAME="${TZ:-Europe/Berlin}"
UNPRIVILEGED="${UNPRIVILEGED:-1}"

if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_INFO=$'\033[34m'; C_OK=$'\033[32m'
  C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_INFO=""; C_OK=""; C_WARN=""; C_ERR=""; C_BOLD=""
fi

msg_info() { echo "${C_INFO}ℹ${C_RESET} $*"; }
msg_ok()   { echo "${C_OK}✓${C_RESET} $*"; }
msg_warn() { echo "${C_WARN}!${C_RESET} $*"; }
msg_error(){ echo "${C_ERR}✗${C_RESET} $*" >&2; }
die()      { msg_error "$*"; exit 1; }

header() {
  echo "${C_BOLD}${APP_NAME} – Proxmox-VE-Installer${C_RESET}"
  echo
}

on_error() {
  local rc=$1 line=$2 f=1
  msg_error "Fehlerkette – Exit ${rc} in Zeile ${line}: ${BASH_COMMAND}"
  while (( f < ${#FUNCNAME[@]} )); do
    msg_error "  ↳ ${FUNCNAME[$f]}() @ ${BASH_SOURCE[$((f-1))]:-?}:${BASH_LINENO[$((f-1))]:-?}"
    f=$((f+1))
  done
  if [ -n "${LOG_FILE:-}" ] && [ -f "$LOG_FILE" ]; then
    msg_error "Letzte Zeilen des Installationslogs ($LOG_FILE):"
    tail -20 "$LOG_FILE" >&2 || true
  fi
  msg_error "Für die vollständige Fehlersuche: $0 --debug (bzw. bash -x erneut starten)"
}

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --ctid)     CTID="$2"; shift 2 ;;
      --update)   MODE="update"; shift ;;
      --status)   MODE="status"; shift ;;
      --uninstall) MODE="uninstall"; shift ;;
      --yes|-y)   ASSUME_YES=1; shift ;;
      --cron)     USE_CRON=1; shift ;;
      --no-ollama) INSTALL_OLLAMA=0; shift ;;
      --ollama)   INSTALL_OLLAMA=1; shift ;;
      --debug)    DEBUG=1; shift ;;
      --help|-h)  usage ;;
      *)          die "Unbekannte Option: $1 (siehe --help)" ;;
    esac
  done
  # Fallback für die Einzeiler-Variante: bash -c "$(...)" --flag übergibt --flag als $0
  if [ "${0#--}" != "$0" ]; then
    case "$0" in
      --update) MODE="update" ;;
      --status) MODE="status" ;;
    esac
  fi
}

require_host_prereqs() {
  local missing=()
  for tool in pct pveam pvesm pvesh; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  (( ${#missing[@]} == 0 )) || die "Fehlende Werkzeuge auf dem Proxmox-Host: ${missing[*]}"
  [ "$EUID" -eq 0 ] || die "Bitte als root ausführen."
}

find_ct_by_hostname() {
  pct list 2>/dev/null | awk -v h="$APP_HOSTNAME" 'NR>1 && $2==h {print $1; exit}'
}

next_free_ctid() {
  if [ -n "$CTID" ]; then echo "$CTID"; return; fi
  pvesh get /cluster/nextid 2>/dev/null || {
    local max
    max=$(pct list 2>/dev/null | awk 'NR>1 {print $1}' | sort -n | tail -1)
    echo $(( ${max:-100} + 1 ))
  }
}

pick_storage() {
  if [ -n "$STORAGE" ]; then echo "$STORAGE"; return; fi
  pvesm status 2>/dev/null | awk -F'[[:space:]]+' '$NF=="rootdir" {print $1; exit}' | head -1 \
    || echo "local-lvm"
}

pick_template() {
  local arch ref
  arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
  ref="$(pveam list local 2>/dev/null \
        | grep -oE "vztmpl/debian-1[23]-standard[^ ]*_${arch}\.tar\.zst" \
        | sort -rV | head -1 || true)"
  if [ -z "$ref" ]; then
    msg_info "Kein passendes Debian-Template vorhanden – lade herunter"
    pveam download local "debian-13-standard_${arch}.tar.zst" >/dev/null 2>&1 || true
    ref="$(pveam list local 2>/dev/null \
          | grep -oE "vztmpl/debian-1[23]-standard[^ ]*_${arch}\.tar\.zst" \
          | sort -rV | head -1 || true)"
  fi
  [ -n "$ref" ] || die "Kein Debian-Template gefunden (pveam download local …)"
  echo "local:$ref"
}

verify_ct_started() {
  local id=$1
  local status
  status="$(pct status "$id" 2>/dev/null | awk '{print $2}')"
  [ "$status" = "running" ] || die "Container $id läuft nicht (Status: ${status:-unbekannt})"
  msg_ok "Container $id läuft"
}

wait_ct_network() {
  local id=$1
  for _ in $(seq 1 30); do
    if pct exec "$id" -- getent hosts deb.debian.org >/dev/null 2>&1; then
      msg_ok "Netz im Container erreichbar"
      return 0
    fi
    sleep 2
  done
  die "Container $id bekommt keinen Netzwerkzugang (DNS/DHCP prüfen)"
}

do_status() {
  local id
  id="$(find_ct_by_hostname)"
  if [ -z "$id" ]; then msg_warn "Kein Container '$APP_HOSTNAME' vorhanden"; exit 0; fi
  echo "Container: $id"
  pct status "$id" || true
  local ip
  ip="$(pct exec "$id" -- hostname -I 2>/dev/null | awk '{print $1}')"
  echo "IP: ${ip:-unbekannt}"
  echo "Web-UI: http://${ip:-<ip>}:$APP_PORT"
  pct exec "$id" -- systemctl is-active domainradar-api 2>/dev/null || true
}

do_uninstall() {
  local id
  id="$(find_ct_by_hostname)"
  [ -n "$id" ] || die "Kein Container '$APP_HOSTNAME' vorhanden"
  if [ "$ASSUME_YES" -ne 1 ]; then
    read -r -p "Container $id ($APP_NAME) wirklich löschen? [j/N] " reply
    case "$reply" in j|ja|J|Ja|YES|yes|y) ;; *) echo "Abbruch."; exit 0 ;; esac
  fi
  pct stop "$id" >/dev/null 2>&1 || true
  pct destroy "$id" || die "Container $id konnte nicht gelöscht werden"
  msg_ok "Container $id gelöscht"
}

do_install() {
  local existing ctid storage template
  existing="$(find_ct_by_hostname)"
  if [ -n "$existing" ]; then
    msg_info "Container '$APP_HOSTNAME' existiert bereits ($existing) – Update-Modus"
    MODE="update"
    CTID="$existing"
  fi

  ctid="$(next_free_ctid)"
  storage="$(pick_storage)"
  template="$(pick_template)"

  if [ "$MODE" = "update" ]; then
    msg_info "Aktualisiere Container $ctid"
  else
    msg_info "Erstelle Container $ctid"
    pct create "$ctid" "$template" \
      --hostname "$APP_HOSTNAME" \
      --cores "$CORES" \
      --memory "$RAM" \
      --swap "$SWAP" \
      --rootfs "${storage}:${DISK}" \
      --net0 "name=eth0,bridge=vmbr0,ip=dhcp,firewall=0" \
      --features "nesting=1,keyctl=1" \
      --unprivileged "$UNPRIVILEGED" \
      --onboot 1 \
      --start 1 \
      --description "${APP_NAME} – Radar für neue .de-Domains (Web-UI auf Port ${APP_PORT})" \
      --tags "domainradar" || die "pct create fehlgeschlagen"
    msg_ok "Container $ctid erstellt"
  fi

  verify_ct_started "$ctid"
  wait_ct_network "$ctid"

  msg_info "Lade Skripte"
  local stage
  stage="$(mktemp -d)"
  trap 'rm -rf "$stage"' EXIT

  local ok=0
  for base in "$REPO_RAW" "https://cdn.jsdelivr.net/gh/HatchetMan111/DomainRadar-Proxmox@main"; do
    if wget -qLO "$stage/inner.sh" "$base/install/domainradar-inner.sh" 2>/dev/null; then
      ok=1; break
    fi
  done
  [ "$ok" -eq 1 ] || die "Konnte domainradar-inner.sh nicht von GitHub laden"
  grep -q "DOMAINRADAR-INNER-VERSION" "$stage/inner.sh" \
    || die "Inner-Skript ohne Versions-Marker – CDN-Lag? Bitte erneut versuchen."

  wget -qO- "$REPO_RAW/../requirements.txt" -O "$stage/requirements.txt" 2>/dev/null || true

  # Quelldateien als Tarball holen (ein Request statt vieler Einzelabrufe)
  local tarball="$stage/src.tar.gz"
  ok=0
  for base in "$REPO_RAW/../archive/refs/heads/main.tar.gz" \
              "https://codeload.github.com/HatchetMan111/DomainRadar-Proxmox/tar.gz/refs/heads/main"; do
    if wget -qO "$tarball" "$base" 2>/dev/null; then ok=1; break; fi
  done
  [ "$ok" -eq 1 ] || die "Konnte Quellcode nicht von GitHub laden"

  msg_info "Übertrage Dateien in Container $ctid"
  local work
  work="$(mktemp -d)"
  tar -xzf "$tarball" -C "$work" --strip-components=1 || die "Entpacken fehlgeschlagen"
  rm -rf "$work/.git"

  pct push "$ctid" "$work/src" /root/domainradar-src/src --perms 0755 >/dev/null
  pct push "$ctid" "$work/web" /root/domainradar-src/web --perms 0755 >/dev/null
  pct push "$ctid" "$work/systemd" /root/domainradar-src/systemd --perms 0755 >/dev/null
  pct push "$ctid" "$work/requirements.txt" /root/domainradar-src/requirements.txt --perms 0644 >/dev/null
  pct push "$ctid" "$stage/inner.sh" /root/domainradar-inner.sh --perms 0700 >/dev/null

  msg_info "Führe Installation im Container aus"
  LOG_FILE="${LOG_FILE:-/tmp/domainradar-install.log}"
  if ! pct exec "$ctid" -- env \
      APP_PORT="$APP_PORT" \
      INSTALL_OLLAMA="$INSTALL_OLLAMA" \
      TZ="$TZ_NAME" \
      bash -c "set -o pipefail; bash /root/domainradar-inner.sh 2>&1 | tee /var/log/domainradar-setup.log"
  then
    msg_error "Inneres Skript fehlgeschlagen – Details siehe oben"
    [ "$DEBUG" -eq 1 ] || msg_error "Erneut mit --debug starten für bash -x"
    exit 1
  fi

  if [ "$USE_CRON" -eq 1 ]; then
    msg_info "Richte cron statt systemd-Timern ein"
    pct exec "$ctid" -- bash -c "for t in collect enrich classify changes; do systemctl disable --now domainradar-\$t.timer >/dev/null 2>&1 || true; done" || true
    pct exec "$ctid" -- bash -c "cat > /etc/cron.d/domainradar <<'EOF'
*/5 * * * * domainradar /opt/domainradar/venv/bin/python -m domainradar.collector >> /var/log/domainradar.log 2>&1
*/10 * * * * domainradar /opt/domainradar/venv/bin/python -m domainradar.enricher >> /var/log/domainradar.log 2>&1
0 3 * * * domainradar /opt/domainradar/venv/bin/python -m domainradar.classifier >> /var/log/domainradar.log 2>&1
0 5 * * 1 domainradar /opt/domainradar/venv/bin/python -m domainradar.changes >> /var/log/domainradar.log 2>&1
EOF
chmod 644 /etc/cron.d/domainradar" || true
  fi

  verify_installation "$ctid"

  local ip
  ip="$(pct exec "$ctid" -- hostname -I 2>/dev/null | awk '{print $1}')"
  echo
  msg_ok "Fertig."
  echo
  echo "  Web-UI:   http://${ip:-<container-ip>}:$APP_PORT"
  echo "  API:      http://${ip:-<container-ip>}:$APP_PORT/api/stats"
  echo "  Container: $ctid"
  echo "  Logs:     pct exec $ctid -- journalctl -u domainradar-api -n 50"
  echo
}

verify_installation() {
  local id=$1
  msg_info "Verifiziere Installation"

  if ! pct exec "$id" -- systemctl is-active --quiet domainradar-api; then
    msg_error "Service domainradar-api läuft nicht"
    pct exec "$id" -- journalctl -u domainradar-api -n 30 --no-pager >&2 || true
    die "Verifikation fehlgeschlagen"
  fi
  msg_ok "Service domainradar-api ist aktiv"

  local code
  code="$(pct exec "$id" -- curl -s -o /dev/null -w '%{http_code}' -m 5 "http://127.0.0.1:$APP_PORT/api/health" || true)"
  if [ "$code" = "200" ]; then
    msg_ok "Web-UI antwortet (HTTP 200 auf Port $APP_PORT)"
  else
    msg_error "Web-UI antwortet nicht (HTTP ${code:-keine Antwort})"
    pct exec "$id" -- journalctl -u domainradar-api -n 30 --no-pager >&2 || true
    die "Verifikation fehlgeschlagen"
  fi
}

main() {
  header
  parse_args "$@"

  case "$MODE" in
    status)    do_status; exit 0 ;;
    uninstall) do_uninstall; exit 0 ;;
  esac

  require_host_prereqs

  if [ "$INSTALL_OLLAMA" -eq 1 ]; then
    msg_info "Ollama aktiv – Container wird leistungsfähiger dimensioniert"
    CORES=$HEAVY_CORES; RAM=$HEAVY_RAM; DISK=$HEAVY_DISK
  fi

  do_install
}

main "$@"