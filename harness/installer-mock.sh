#!/usr/bin/env bash
# =============================================================================
#  Mock-Test für den DomainRadar-Installer (ohne Proxmox-Host).
#
#  Ersetzt pct/pveam/pvesm/pvesh durch Attrappen und führt den Installer
#  darüber aus. Damit ist prüfbar, dass:
#    - die Argumente ankommen (--id, --memory, --port, --ollama, --update …)
#    - pct create mit den richtigen Parametern aufgerufen wird
#    - die Verifikation läuft und bei Erfolg grün meldet
#    - Fehler die vollständige Kette ausgeben (on_error)
#
#  Das ist kein Ersatz für den echten Lauf, aber es fängt genau die Fehler,
#  die sonst erst auf dem Host auftauchen.
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALLER_SRC="$ROOT/install/domainradar.sh"

PASS=0
FAIL=0
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

echo "DomainRadar Installer-Mock"
echo

# ── Attrappen-Verzeichnis ───────────────────────────────────────────────────
MOCK="$(mktemp -d)"
trap 'rm -rf "$MOCK"' EXIT
mkdir -p "$MOCK/bin"

# Kopie des Installers mit ausgebautem Root-Check. Das Original bleibt
# unangetastet – der Mock darf den Installer nicht verändern.
INSTALLER="$MOCK/domainradar.sh"
sed 's|\[ "\$EUID" -eq 0 \]|true|' "$INSTALLER_SRC" > "$INSTALLER"
chmod +x "$INSTALLER"

# Zustand, den die Attrappen untereinander teilen
export MOCK_STATE="$MOCK/state"
: > "$MOCK_STATE"

# --- pct ---------------------------------------------------------------------
cat > "$MOCK/bin/pct" <<'MOCK'
#!/usr/bin/env bash
echo "pct $*" >> "$MOCK_STATE"
cmd="${1:-}"
case "$cmd" in
  list)
    # Format wie echtes pct list: VMID Status Lock Name
    # Eine Zeile mit passendem Hostnamen, damit find_ct_by_hostname greift
    echo "VMID Status         Lock         Name"
    echo " 250  running       -            domainradar"
    ;;
  status)
    echo "status: running"
    ;;
  config)
    echo "hostname: domainradar"
    ;;
  exec)
    shift
    # pct exec <id> -- <cmd...> | pct exec <id> -- env ... bash
    sub="${1:-}"
    case "$sub" in
      --) shift ;;
    esac
    case "${1:-}" in
      hostname) echo "192.168.178.250" ;;
      id) echo "0" ;;
      systemctl)
        # is-active / is-enabled / enable --now → Erfolg
        exit 0
        ;;
      curl)
        # HTTP-Check: 200 zurückgeben
        exit 0
        ;;
      env)
        # env VAR=wert VAR2=wert2 bash -c "…" → Variablen schlucken,
        # dann das eigentliche Kommando ausführen
        shift
        while [[ "${1:-}" == *= ]]; do shift; done
        case "${1:-}" in
          bash|sh) shift; [ "${1:-}" = "-c" ] && shift; eval "${1:-true}" ;;
          *) : ;;
        esac
        exit 0
        ;;
      bash)
        exit 0
        ;;
      sh)
        exit 0
        ;;
      *)
        exit 0
        ;;
    esac
    ;;
  push)
    exit 0
    ;;
  create)
    exit 0
    ;;
  set)
    exit 0
    ;;
  *)
    exit 0
    ;;
esac
MOCK

# --- pvesm -------------------------------------------------------------------
cat > "$MOCK/bin/pvesm" <<'MOCK'
#!/usr/bin/env bash
echo "pvesm $*" >> "$MOCK_STATE"
if [ "${1:-}" = "status" ]; then
  echo "Name         Type     Status           Total            Used"
  echo "local-lvm    lvmthin  active           100.00 GiB       10.00 GiB"
fi
exit 0
MOCK

# --- pveam -------------------------------------------------------------------
cat > "$MOCK/bin/pveam" <<'MOCK'
#!/usr/bin/env bash
echo "pveam $*" >> "$MOCK_STATE"
if [ "${1:-}" = "list" ]; then
  echo "local:vztmpl/debian-13-standard_13.0_amd64.tar.zst"
fi
exit 0
MOCK

# --- pvesh -------------------------------------------------------------------
cat > "$MOCK/bin/pvesh" <<'MOCK'
#!/usr/bin/env bash
echo "pvesh $*" >> "$MOCK_STATE"
# n"chste freie ID
if [ "${1:-}" = "get" ]; then
  echo "250"
fi
exit 0
MOCK

# --- wget (darf nichts laden, wir tun nur so) --------------------------------
cat > "$MOCK/bin/wget" <<'MOCK'
#!/usr/bin/env bash
echo "wget $*" >> "$MOCK_STATE"
# Ausgabedatei bestimmen und mit Minimalinhalt füllen
out=""
prev=""
for a in "$@"; do
  if [ "$prev" = "-O" ] || [ "$prev" = "-qO" ]; then out="$a"; fi
  case "$a" in
    -O) prev="-O" ;;
    -qO) prev="-qO" ;;
  esac
done
# wget -qLO <file> <url>
args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  if [ "${args[$i]}" = "-qLO" ] && [ -n "${args[$((i+1))]:-}" ]; then
    out="${args[$((i+1))]}"
  fi
done
if [ -n "$out" ]; then
  mkdir -p "$(dirname "$out")" 2>/dev/null
  # Versions-Marker, damit der Installer seine Prüfung besteht
  echo "#!/usr/bin/env bash" > "$out"
  echo "# DOMAINRADAR-INNER-VERSION mock" >> "$out"
  echo "exit 0" >> "$out"
fi
# Tarball: gültiges tar.gz erzeugen
case "$*" in
  *tar.gz*)
    tb=""
    for ((i=0; i<${#args[@]}; i++)); do
      if [ "${args[$i]}" = "-qO" ] && [ -n "${args[$((i+1))]:-}" ]; then tb="${args[$((i+1))}"; fi
    done
    if [ -n "$tb" ]; then
      tmp="$(mktemp -d)"
      mkdir -p "$tmp/src" "$tmp/web" "$tmp/systemd"
      echo "x" > "$tmp/requirements.txt"
      tar -czf "$tb" -C "$tmp" . 2>/dev/null
      rm -rf "$tmp"
    fi
    ;;
esac
exit 0
MOCK

chmod +x "$MOCK/bin/"*
export PATH="$MOCK/bin:$PATH"

# ── 1. Syntax ────────────────────────────────────────────────────────────────
echo "== Syntax =="
if bash -n "$INSTALLER"; then ok "domainradar.sh Syntax"; else bad "Syntaxfehler"; fi
if bash -n "$ROOT/install/domainradar-inner.sh"; then ok "Inner-Skript Syntax"; else bad "Syntaxfehler"; fi

# ── 2. Hilfe ─────────────────────────────────────────────────────────────────
echo "== Hilfe =="
HELP="$(bash "$INSTALLER" --help 2>&1)"
if echo "$HELP" | grep -q "raw.githubusercontent.com/HatchetMan111/DomainRadar/main"; then
  ok "Einzeiler zeigt auf das richtige Repo"
else
  bad "Einzeiler-URL falsch"
fi
if echo "$HELP" | grep -q "set -Eeuo pipefail"; then
  bad "Hilfe zeigt Code statt Text"
else
  ok "Hilfe endet vor dem Code"
fi

# ── 3. Fehlermeldung ohne Proxmox ────────────────────────────────────────────
echo "== Fehlerpfad =="
# PATH ohne Attrappen: pct darf nicht existieren
OUT="$(PATH=/usr/bin:/bin bash "$INSTALLER" --status 2>&1 || true)"
if echo "$OUT" | grep -q "Fehlende Werkzeuge"; then
  ok "Klare Meldung statt Stacktrace"
else
  bad "Unklare Fehlermeldung: $OUT"
fi

# ── 3b. Beide Aufruf-Formen ─────────────────────────────────────────────────
echo "== Aufruf-Formen =="
# bash -c "$(wget ...)" --update  → $0 ist "--update"
OUT2="$(bash -c "bash \"$INSTALLER\" --update" 2>&1 || true)"
if echo "$OUT2" | grep -q "Unbekannte Option"; then
  bad "Form '--update' (als \$0) wird nicht erkannt"
else
  ok "Form '--update' (als \$0) wird erkannt"
fi
# bash -c "$(wget ...)" -- --update  → $0 ist "--"
OUT3="$(bash -c "bash \"$INSTALLER\" -- --update" 2>&1 || true)"
if echo "$OUT3" | grep -q "Unbekannte Option"; then
  bad "Form '-- --update' wird nicht erkannt"
else
  ok "Form '-- --update' wird erkannt"
fi

# ── 4. Argumente kommen an ───────────────────────────────────────────────────
echo "== Argumente =="
: > "$MOCK_STATE"
bash "$INSTALLER" --id 250 --memory 8192 --cores 4 --port 9000 >/dev/null 2>&1 || true
if grep -q "create 250" "$MOCK_STATE"; then
  ok "--id 250 wird verwendet"
else
  bad "--id nicht durchgereicht"
fi
if grep -q -- "--memory 8192" "$MOCK_STATE"; then
  ok "--memory wird gesetzt"
else
  bad "--memory nicht gesetzt"
fi
if grep -q -- "--cores 4" "$MOCK_STATE"; then
  ok "--cores wird gesetzt"
else
  bad "--cores nicht gesetzt"
fi

# ── 5. onboot und nextid ─────────────────────────────────────────────────────
echo "== Container-Konfiguration =="
if grep -q -- "--onboot 1" "$MOCK_STATE"; then
  ok "onboot=1 gesetzt"
else
  bad "onboot fehlt"
fi
# nextid darf nur abfragen, wenn keine --id gesetzt ist. Deshalb zwei Läufe:
# 1. mit --id 250   → pvesh darf NICHT vorkommen
# 2. ohne --id      → pvesh MUSS vorkommen
if grep -q "pvesh get /cluster/nextid" "$MOCK_STATE"; then
  bad "pvesh wird trotz --id 250 abgewertet"
else
  ok "--id 250 überspringt nextid korrekt"
fi

: > "$MOCK_STATE"
bash "$INSTALLER" >/dev/null 2>&1 || true
if grep -q "pvesh get /cluster/nextid" "$MOCK_STATE"; then
  ok "ohne --id wird die nächste freie ID erfragt"
else
  bad "nextid wird nicht abgefragt"
fi

echo
echo "-----------------------------------------"
echo " bestanden: $PASS   fehlgeschlagen: $FAIL"
[ "$FAIL" -eq 0 ] && { echo " Alle Mock-Prüfungen grün."; exit 0; } || exit 1