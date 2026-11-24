#!/usr/bin/env bash
# =============================================================================
#  📡 DomainRadar – One-Click-Installer (Proxmox VE)
#  https://github.com/HatchetMan111/DomainRadar
#
#  Baut einen lokalen Radar für neu beobachtete .de-Domains: liest
#  Certificate-Transparency-Logs (RFC 6962) direkt aus – ohne crt.sh, ohne
#  API-Key – ruft die neuen Webseiten auf und ordnet ein, was sie anbieten.
#  Vollständig lokal: eine SQLite-Datei, keine Cloud, keine Weitergabe.
#
#  Architektur im Container: natives Python 3 (FastAPI + uvicorn) + SQLite +
#  React/Vite-Frontend (Build einmalig beim Install, danach statisch) +
#  systemd-Services für API und vier Timer für die Pipeline. Kein Docker nötig.
#
#  Installation (auf dem Proxmox-Host als root):
#    bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/DomainRadar/main/install/domainradar.sh)"
#
#  Mit Optionen (direkt hinter dem schließenden Anführungszeichen):
#    ... " --id 250 --memory 8192 --cores 4 --ollama
#
#  Lokales LLM (Ollama, CPU-Inferenz) gleich mitinstallieren:
#    ... " --ollama
#
#  Update / Status / Deinstall / Debug:
#    ... " --update | --status | --uninstall | --debug
#
#  Hinweis: Die Web-UI bindet an 0.0.0.0 und hat KEINE Authentifizierung.
#  Wer den Port aus dem LAN erreichbar macht, stellt eine öffentliche
#  Beobachtungsdatenbank ins Netz – inklusive der Einstellungsseite, auf der
#  API-Keys hinterlegt werden. Hinter Firewall belassen oder Reverse-Proxy
#  mit Basic-Auth davorsetzen.
# =============================================================================

set -Eeuo pipefail

# ── Defaults ────────────────────────────────────────────────────────────────
APP_NAME="domainradar"
CT_HOSTNAME="domainradar"
REPO_URL="https://github.com/HatchetMan111/DomainRadar.git"
RAW_URL="https://raw.githubusercontent.com/HatchetMan111/DomainRadar/main/install/domainradar.sh"
ONE_LINER="bash -c \"\$(wget -qLO - ${RAW_URL})\""
APP_PORT="8477"
DEFAULT_CORES=2
DEFAULT_RAM=2048
DEFAULT_SWAP=512
DEFAULT_DISK=8
HEAVY_CORES=4
HEAVY_RAM=8192
HEAVY_DISK=24

CT_ID=""
CT_STORAGE=""
CT_BRIDGE="vmbr0"
CT_IP=""
CT_TEMPLATE=""
WEB_PORT="$APP_PORT"
MODE="install"
USE_CRON=0
INSTALL_OLLAMA=0
DEBUG=0
ASSUME_YES=0

CT_CORES="${CT_CORES:-$DEFAULT_CORES}"
CT_MEMORY="${CT_MEMORY:-$DEFAULT_RAM}"
CT_SWAP="${CT_SWAP:-$DEFAULT_SWAP}"
CT_ROOTFS="${CT_ROOTFS:-$DEFAULT_DISK}"
UNPRIVILEGED="${UNPRIVILEGED:-1}"
TZ_NAME="${TZ:-Europe/Berlin}"
LOG_FILE="/tmp/${APP_NAME}-install.log"
STAGE_DIR=""

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
  local ec=$1 line=$2 cmd=$3 f=1
  msg_error "Installationsfehler – Exit-Code ${ec} in Zeile ${line}"
  msg_error "Fehlgeschlagener Befehl: ${cmd}"
  # Funktionskette von der Fehlerstelle nach außen
  while (( f < ${#FUNCNAME[@]} )); do
    msg_error "  ↳ ${FUNCNAME[$f]}() @ ${BASH_SOURCE[$((f-1))]:-?}:${BASH_LINENO[$((f-1))]:-?}"
    f=$((f+1))
  done
  if [ -f "$LOG_FILE" ]; then
    msg_info "Letzte Logzeilen (${LOG_FILE}):"
    tail -n 15 "$LOG_FILE" | sed 's/^/    /' >&2 || true
  fi
  # Container-Logs sind die eigentliche Spur, wenn der Aufbau schiefging
  if [ -n "${CT_ID:-}" ] && pct status "$CT_ID" &>/dev/null; then
    msg_info "Container-Logs (Setup + Service):"
    pct exec "$CT_ID" -- sh -c '
        tail -n 40 /var/log/domainradar-setup.log 2>/dev/null
        echo "--- systemctl status domainradar-api ---"
        systemctl status domainradar-api --no-pager 2>/dev/null | head -n 12
        echo "--- journalctl -u domainradar-api ---"
        journalctl -u domainradar-api --no-pager 2>/dev/null | tail -n 20
    ' 2>/dev/null | sed 's/^/    /' >&2 || true
    msg_info "Container ${CT_ID} wurde NICHT gelöscht – Inspektion: pct enter ${CT_ID}"
    msg_info "Aufräumen: pct stop ${CT_ID} && pct destroy ${CT_ID} --purge"
  fi
  msg_info "Vollständiges Log: ${LOG_FILE}"
  msg_info "Neu ausführen mit vollem bash -x-Trace:  ${ONE_LINER} --debug"
  exit "$ec"
}

usage() {
  sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

parse_args() {
  # $0-Fallback: `bash -c "$(wget ...)" --id 250` belegt $0 mit dem ersten
  # Flag statt $1 – deshalb $0 wieder in die Argumentliste einsortieren.
  local -a args=("$@")
  if [[ "${0:0:1}" == "-" ]]; then
    args=("$0" "$@")
  fi
  # Trennzeichen "bash -c ... -- --update" abwerfen: $0 ist dann "--"
  if [[ "${args[0]:-}" == "--" ]]; then
    args=("${args[@]:1}")
  fi

  while [[ ${#args[@]} -gt 0 ]]; do
    case "${args[0]}" in
      --id|-ctid)       CT_ID="${args[1]}"; args=("${args[@]:2}") ;;
      --hostname)       CT_HOSTNAME="${args[1]}"; args=("${args[@]:2}") ;;
      --storage)        CT_STORAGE="${args[1]}"; args=("${args[@]:2}") ;;
      --cores)          CT_CORES="${args[1]}"; args=("${args[@]:2}") ;;
      --memory)         CT_MEMORY="${args[1]}"; args=("${args[@]:2}") ;;
      --swap)           CT_SWAP="${args[1]}"; args=("${args[@]:2}") ;;
      --rootfs)         CT_ROOTFS="${args[1]}"; args=("${args[@]:2}") ;;
      --bridge)         CT_BRIDGE="${args[1]}"; args=("${args[@]:2}") ;;
      --ip)             CT_IP="${args[1]}"; args=("${args[@]:2}") ;;
      --port)           WEB_PORT="${args[1]}"; args=("${args[@]:2}") ;;
      --update)         MODE="update"; args=("${args[@]:1}") ;;
      --uninstall)      MODE="uninstall"; args=("${args[@]:1}") ;;
      --status)         MODE="status"; args=("${args[@]:1}") ;;
      --cron)           USE_CRON=1; args=("${args[@]:1}") ;;
      --ollama)         INSTALL_OLLAMA=1; args=("${args[@]:1}") ;;
      --no-ollama)      INSTALL_OLLAMA=0; args=("${args[@]:1}") ;;
      --yes|-y)         ASSUME_YES=1; args=("${args[@]:1}") ;;
      --debug)          DEBUG=1; args=("${args[@]:1}") ;;
      --help|-h)        usage ;;
      *)                die "Unbekannte Option: ${args[0]} (--help für Hilfe)" ;;
    esac
  done

  if [ "$DEBUG" -eq 1 ]; then
    set -x
  fi
}

trap 'on_error $? $LINENO "$BASH_COMMAND"' ERR

require_host_prereqs() {
  local missing=()
  for tool in pct pveam pvesm pvesh; do
    command -v "$tool" >/dev/null 2>&1 || missing+=("$tool")
  done
  (( ${#missing[@]} == 0 )) || die "Fehlende Werkzeuge auf dem Proxmox-Host: ${missing[*]}"
  [ "$EUID" -eq 0 ] || die "Bitte als root ausführen."
}

find_ct_by_hostname() {
  pct list 2>/dev/null | awk -v h="$CT_HOSTNAME" 'NR>1 && $2==h {print $1; exit}'
}

next_free_ctid() {
  if [ -n "$CT_ID" ]; then echo "$CT_ID"; return; fi
  pvesh get /cluster/nextid 2>/dev/null || {
    local max
    max=$(pct list 2>/dev/null | awk 'NR>1 {print $1}' | sort -n | tail -1)
    echo $(( ${max:-100} + 1 ))
  }
}

pick_storage() {
  if [ -n "$CT_STORAGE" ]; then echo "$CT_STORAGE"; return; fi
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
  if [ -z "$id" ]; then msg_warn "Kein Container '$CT_HOSTNAME' vorhanden"; exit 0; fi
  echo "Container: $id"
  pct status "$id" || true
  local ip
  ip="$(pct exec "$id" -- hostname -I 2>/dev/null | awk '{print $1}')"
  echo "IP: ${ip:-unbekannt}"
  echo "Web-UI: http://${ip:-<ip>}:$WEB_PORT"
  pct exec "$id" -- systemctl is-active domainradar-api 2>/dev/null || true
}

do_uninstall() {
  local id
  id="$(find_ct_by_hostname)"
  [ -n "$id" ] || die "Kein Container '$CT_HOSTNAME' vorhanden"
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
    msg_info "Container '$CT_HOSTNAME' existiert bereits ($existing) – Update-Modus"
    MODE="update"
    CT_ID="$existing"
  fi

  ctid="${CT_ID:-$(next_free_ctid)}"
  CT_ID="$ctid"  # für on_error sichtbar
  storage="$(pick_storage)"
  template="${CT_TEMPLATE:-$(pick_template)}"

  if [ "$MODE" = "update" ]; then
    msg_info "Aktualisiere Container $ctid"
  else
    msg_info "Erstelle Container $ctid"
    pct create "$ctid" "$template" \
      --hostname "$CT_HOSTNAME" \
      --cores "$CT_CORES" \
      --memory "$CT_MEMORY" \
      --swap "$CT_SWAP" \
      --rootfs "${storage}:${CT_ROOTFS}" \
      --net0 "name=eth0,bridge=${CT_BRIDGE},ip=${CT_IP:-dhcp},firewall=0" \
      --features "nesting=1,keyctl=1" \
      --unprivileged "$UNPRIVILEGED" \
      --onboot 1 \
      --start 1 \
      --description "${APP_NAME} – Radar für neue .de-Domains (Web-UI auf Port ${WEB_PORT})" \
      --tags "domainradar" || die "pct create fehlgeschlagen"
    msg_ok "Container $ctid erstellt"
  fi

  verify_ct_started "$ctid"
  wait_ct_network "$ctid"

  msg_info "Lade Skripte"
  # Bewusst global (nicht `local`): die EXIT-Falle läuft erst nach dem Ende
  # dieser Funktion und würde eine lokale Variable nicht mehr sehen.
  STAGE_DIR="$(mktemp -d)"
  stage="$STAGE_DIR"
  trap 'rm -rf "${STAGE_DIR:-}"' EXIT

  local ok=0
  for base in "https://raw.githubusercontent.com/HatchetMan111/DomainRadar/main" \
              "https://cdn.jsdelivr.net/gh/HatchetMan111/DomainRadar@main"; do
    if wget -qLO "$stage/inner.sh" "$base/install/domainradar-inner.sh" 2>/dev/null; then
      ok=1; break
    fi
  done
  [ "$ok" -eq 1 ] || die "Konnte domainradar-inner.sh nicht von GitHub laden"
  grep -q "DOMAINRADAR-INNER-VERSION" "$stage/inner.sh" \
    || die "Inner-Skript ohne Versions-Marker – CDN-Lag? Bitte erneut versuchen."

  # Quelldateien als Tarball holen (ein Request statt vieler Einzelabrufe)
  local tarball="$stage/src.tar.gz"
  ok=0
  for base in "https://codeload.github.com/HatchetMan111/DomainRadar/tar.gz/refs/heads/main" \
              "https://github.com/HatchetMan111/DomainRadar/archive/refs/heads/main.tar.gz"; do
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
      APP_PORT="$WEB_PORT" \
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
  echo "  Web-UI:    http://${ip:-<container-ip>}:$WEB_PORT"
  echo "  API:       http://${ip:-<container-ip>}:$WEB_PORT/api/stats"
  echo "  Projekt:   $REPO_URL"
  echo "  Container: $ctid  (Hostname: $CT_HOSTNAME)"
  echo "  Logs:      pct exec $ctid -- journalctl -u domainradar-collect -n 30"
  echo
  msg_warn "Die Web-UI hat KEINE Authentifizierung und bindet an 0.0.0.0."
  msg_warn "Nicht aus dem Internet erreichbar machen – API-Keys stehen in den Einstellungen."
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

  # uvicorn braucht einen Moment – 15 s Geduld statt sofortigem Fehlschlag
  local code="" _
  for _ in $(seq 1 15); do
    code="$(pct exec "$id" -- curl -s -o /dev/null -w '%{http_code}' -m 4 "http://127.0.0.1:$WEB_PORT/api/health" 2>/dev/null || true)"
    [ "$code" = "200" ] && break
    sleep 1
  done

  if [ "$code" = "200" ]; then
    msg_ok "Web-UI antwortet (HTTP 200 auf Port $WEB_PORT)"
  else
    msg_error "Web-UI antwortet nicht (HTTP ${code:-keine Antwort})"
    pct exec "$id" -- journalctl -u domainradar-api -n 30 --no-pager >&2 || true
    die "Verifikation fehlgeschlagen"
  fi

  # Ohne aktive Timer passiert nach dem ersten Lauf nichts mehr
  local t active=0
  for t in collect enrich classify changes; do
    if pct exec "$id" -- systemctl is-enabled --quiet "domainradar-$t.timer" 2>/dev/null; then
      active=$((active+1))
    else
      msg_warn "Timer domainradar-$t.timer ist nicht aktiviert"
    fi
  done
  if [ "$active" -eq 4 ]; then
    msg_ok "Alle 4 Zeitpläne aktiv (collect, enrich, classify, changes)"
  fi
}

main() {
  header
  parse_args "$@"

  # Werkzeuge zuerst prüfen – auch --status und --uninstall brauchen pct.
  # Ohne das würde `pct list | awk` mit 127 abbrechen, bevor die saubere
  # Meldung überhaupt möglich ist.
  require_host_prereqs

  case "$MODE" in
    status)    do_status; exit 0 ;;
    uninstall) do_uninstall; exit 0 ;;
  esac

  if [ "$INSTALL_OLLAMA" -eq 1 ]; then
    msg_info "Ollama aktiv – Container wird leistungsfähiger dimensioniert"
    CT_CORES=$HEAVY_CORES; CT_MEMORY=$HEAVY_RAM; CT_ROOTFS=$HEAVY_DISK
  fi

  do_install
}

main "$@"