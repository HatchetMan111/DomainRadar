#!/usr/bin/env bash
# =============================================================================
#  Mock-Test für den DomainRadar-Installer (ohne Proxmox-Host).
#
#  Ersetzt pct/pveam/pvesm/pvesh durch Attrappen und führt den Installer
#  darüber aus. Damit ist prüfbar, dass:
#    - die Argumente ankommen (--id, --memory, --port, --ollama, --update …)
#    - pct create mit den richtigen Parametern aufgerufen wird
#    - --rootfs ein echtes "<storage>:<gb>" ist (Regression: ":8" →
#      "unable to parse volume ID ':8'" auf echtem pct)
#    - --update/--status den Container am Hostnamen finden (pct list: Name
#      steht im letzten Feld)
#    - die Verifikation läuft und bei Erfolg grün meldet
#    - Fehler die vollständige Kette ausgeben (on_error)
#
#  Das ist kein Ersatz für den echten Lauf, aber es fängt genau die Fehler,
#  die sonst erst auf dem Host auftauchen.
#
#  Umgebungsvariable MOCK_HAVE_CT=1 simuliert einen vorhandenen Container
#  (pct list meldet dann VMID 250 mit Hostname domainradar).
# =============================================================================
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/.."
INSTALLER_SRC="${INSTALLER_SRC:-$ROOT/install/domainradar.sh}"

PASS=0
FAIL=0
ok()  { echo "  ✓ $*"; PASS=$((PASS+1)); }
bad() { echo "  ✗ $*"; FAIL=$((FAIL+1)); }

echo "DomainRadar Installer-Mock"
echo " Installer: $INSTALLER_SRC"
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

# Storage-Config wie auf einem echten PVE-Host: nur local-lvm darf rootdir
export PVE_STORAGE_CFG="$MOCK/storage.cfg"
cat > "$PVE_STORAGE_CFG" <<'CFG'
dir: local
	path /var/lib/vz
	content iso,vztmpl,backup

lvmthin: local-lvm
	thinpool pve/data
	vgname pve
	content rootdir,images
CFG

# --- pct ---------------------------------------------------------------------
cat > "$MOCK/bin/pct" <<'MOCK'
#!/usr/bin/env bash
echo "pct $*" >> "$MOCK_STATE"
cmd="${1:-}"
case "$cmd" in
  list)
    # Format wie echtes pct list: VMID Status Lock Name
    echo "VMID Status         Lock         Name"
    if [ "${MOCK_HAVE_CT:-0}" = "1" ]; then
      echo " 250  running       -            domainradar"
    fi
    ;;
  status)
    # Realistisches Verhalten: Config fehlt -> Fehlermeldung auf stderr, exit 2
    if [ -f "${MOCK_STATE}.missing" ]; then
      echo "Configuration file 'nodes/mock/lxc/250.conf' does not exist" >&2
      exit 2
    fi
    # exit 2, sobald der Container gestoppt ist
    if [ -f "${MOCK_STATE}.stopped" ]; then
      echo "status: stopped"
      exit 2
    fi
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
      getent) exit 0 ;;
      id) echo "0" ;;
      systemctl)
        # is-active / is-enabled / enable --now → Erfolg
        exit 0
        ;;
      curl)
        # HTTP-Check: 200 zurückgeben
        echo "200"
        exit 0
        ;;
      env)
        # env VAR=wert VAR2=wert2 bash -c "…" → Variablen schlucken,
        # dann das eigentliche Kommando ausführen
        shift
        while [[ "${1:-}" == *=* ]]; do shift; done
        case "${1:-}" in
          bash|sh)
            shift
            [ "${1:-}" = "-c" ] && shift
            # Inner-Skript-Datei existiert im Mock nicht – Erfolg simulieren
            case "${1:-}" in
              *domainradar-inner.sh*) exit 0 ;;
              *) eval "${1:-true}" ;;
            esac
            ;;
          *) : ;;
        esac
        exit 0
        ;;
      bash|sh)
        exit 0
        ;;
      *)
        exit 0
        ;;
    esac
    ;;
  start)
    rm -f "${MOCK_STATE}.stopped"
    exit 0
    ;;
  push)
    exit 0
    ;;
  create)
    # Wie echtes pct create: --rootfs muss "<storage>:<gb>" sein,
    # sonst "unable to parse volume ID ':8'"
    rootfs=""
    prev=""
    for a in "$@"; do
      [ "$prev" = "--rootfs" ] && rootfs="$a"
      prev="$a"
    done
    if [[ ! "$rootfs" =~ ^[^:]+:[0-9]+$ ]]; then
      echo "unable to parse volume ID '${rootfs}'" >&2
      exit 1
    fi
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
  # Realistisches Format: Name Type Status Total Used Available %
  # (pvesm status hat KEINE Content-Spalte – "rootdir" kommt hier nicht vor)
  echo "Name         Type     Status           Total (KiB)      Used (KiB)  Available (KiB)        %"
  echo "local        dir      active            102400000        20480000         81920000  20.00%"
  echo "local-lvm    lvmthin  active            204800000        10240000        194560000   5.00%"
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
for a in "$@"; do
  case "$a" in
    /cluster/nextid)
      echo "106"
      exit 0
      ;;
    */storage|*/storage/*)
      # Content-Spalten wie auf einem echten Host: nur local-lvm darf rootdir
      cat <<'JSON'
[{"storage":"local","type":"dir","content":"iso,vztmpl,backup","active":1},{"storage":"local-lvm","type":"lvmthin","content":"rootdir,images","active":1}]
JSON
      exit 0
      ;;
  esac
done
exit 0
MOCK

# --- wget (lädt nichts, simuliert nur) ---------------------------------------
cat > "$MOCK/bin/wget" <<'MOCK'
#!/usr/bin/env bash
echo "wget $*" >> "$MOCK_STATE"
args=("$@")
out=""
for ((i=0; i<${#args[@]}; i++)); do
  case "${args[$i]}" in
    -qLO|-O|-qO) out="${args[$((i+1))]:-}" ;;
  esac
done
if [ -n "$out" ]; then
  mkdir -p "$(dirname "$out")" 2>/dev/null
  case "$*" in
    *tar.gz*)
      tmp="$(mktemp -d)"
      mkdir -p "$tmp/src" "$tmp/web" "$tmp/systemd"
      echo "x" > "$tmp/requirements.txt"
      tar -czf "$out" -C "$tmp" . 2>/dev/null
      rm -rf "$tmp"
      ;;
    *)
      # Versions-Marker, damit der Installer seine Prüfung besteht
      echo "#!/usr/bin/env bash" > "$out"
      echo "# DOMAINRADAR-INNER-VERSION mock" >> "$out"
      echo "exit 0" >> "$out"
      ;;
  esac
fi
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
OUT="$(PATH=/usr/bin:/bin bash "$INSTALLER" --status 2>&1 || true)"
if echo "$OUT" | grep -q "Fehlende Werkzeuge"; then
  ok "Klare Meldung statt Stacktrace"
else
  bad "Unklare Fehlermeldung: $OUT"
fi

# ── 3b. Beide Aufruf-Formen ─────────────────────────────────────────────────
echo "== Aufruf-Formen =="
OUT2="$(bash -c "bash \"$INSTALLER\" --update" 2>&1 || true)"
if echo "$OUT2" | grep -q "Unbekannte Option"; then
  bad "Form '--update' (als \$0) wird nicht erkannt"
else
  ok "Form '--update' (als \$0) wird erkannt"
fi
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

# ── 4b. rootfs-Storage (Regressionstest für "unable to parse volume ID ':8'") ─
echo "== Storage / rootfs =="
if grep -q -- "--rootfs local-lvm:8" "$MOCK_STATE"; then
  ok "--rootfs ist 'local-lvm:8' (Storage erkannt, nicht ':8')"
else
  bad "--rootfs falsch: $(grep -o -- '--rootfs [^ ]*' "$MOCK_STATE" | head -1)"
fi

: > "$MOCK_STATE"
bash "$INSTALLER" --id 251 --storage local --rootfs 20 >/dev/null 2>&1 || true
if grep -q -- "--rootfs local:20" "$MOCK_STATE"; then
  ok "--storage/--rootfs werden durchgereicht"
else
  bad "--storage/--rootfs nicht durchgereicht: $(grep -o -- '--rootfs [^ ]*' "$MOCK_STATE" | head -1)"
fi

: > "$MOCK_STATE"
OUT_S="$(bash "$INSTALLER" --id 252 --storage gibt-es-nicht 2>&1 || true)"
if echo "$OUT_S" | grep -q "existiert nicht"; then
  ok "Unbekanntes --storage wird sauber abgelehnt"
else
  bad "Unbekanntes --storage nicht erkannt: $OUT_S"
fi

# ── 5. onboot und nextid ─────────────────────────────────────────────────────
echo "== Container-Konfiguration =="
: > "$MOCK_STATE"
bash "$INSTALLER" --id 250 >/dev/null 2>&1 || true
if grep -q -- "--onboot 1" "$MOCK_STATE"; then
  ok "onboot=1 gesetzt"
else
  bad "onboot fehlt"
fi
if grep -q "pvesh get /cluster/nextid" "$MOCK_STATE"; then
  bad "pvesh wird trotz --id 250 abgewertet"
else
  ok "--id 250 überspringt nextid korrekt"
fi

: > "$MOCK_STATE"
bash "$INSTALLER" >/dev/null 2>&1 || true
if grep -q "create 106" "$MOCK_STATE"; then
  ok "ohne --id wird die nächste freie ID (106) verwendet"
else
  bad "nextid wird nicht verwendet"
fi
if grep -q "pvesh get /cluster/nextid" "$MOCK_STATE"; then
  ok "ohne --id wird die nächste freie ID erfragt"
else
  bad "nextid wird nicht abgefragt"
fi

# ── 6. Vorhandener Container: --update/--status finden ihn am Hostnamen ──────
echo "== Update/Status bei vorhandenem Container =="
: > "$MOCK_STATE"
OUT_U="$(MOCK_HAVE_CT=1 bash "$INSTALLER" --update 2>&1 || true)"
if echo "$OUT_U" | grep -q "Update-Modus"; then
  ok "--update erkennt vorhandenen Container am Hostnamen"
else
  bad "--update erkennt Container nicht: $OUT_U"
fi
if grep -q "pct create" "$MOCK_STATE"; then
  bad "--update darf pct create NICHT aufrufen"
else
  ok "--update ruft pct create nicht auf"
fi
if grep -q "pct push" "$MOCK_STATE"; then
  ok "--update pusht die Dateien"
else
  bad "--update pusht keine Dateien"
fi

OUT_ST="$(MOCK_HAVE_CT=1 bash "$INSTALLER" --status 2>&1 || true)"
if echo "$OUT_ST" | grep -q "Container: 250"; then
  ok "--status meldet Container 250"
else
  bad "--status findet Container nicht: $OUT_ST"
fi

OUT_NO="$(bash "$INSTALLER" --status 2>&1 || true)"
if echo "$OUT_NO" | grep -q "Kein Container"; then
  ok "--status ohne Container meldet sauber"
else
  bad "--status ohne Container: $OUT_NO"
fi

# ── 6b. Gestoppter Container: --update startet ihn (Regression pipefail) ────
echo "== Update bei gestopptem Container =="
: > "$MOCK_STATE"
: > "$MOCK_STATE.stopped"
OUT_SU="$(MOCK_HAVE_CT=1 bash "$INSTALLER" --update 2>&1 || true)"
if echo "$OUT_SU" | grep -q "starte ihn"; then
  ok "--update startet gestoppten Container automatisch"
else
  bad "--update startet gestoppten Container nicht: $OUT_SU"
fi
if grep -q "pct start 250" "$MOCK_STATE"; then
  ok "pct start 250 wurde aufgerufen"
else
  bad "pct start 250 fehlt im Zustandslog"
fi
if echo "$OUT_SU" | grep -q "Installationsfehler"; then
  bad "--update mit gestopptem CT bricht mit Installationsfehler ab: $OUT_SU"
else
  ok "--update mit gestopptem CT läuft fehlerfrei durch (kein ERR-Trap)"
fi
rm -f "$MOCK_STATE.stopped"

# ── 6c. Container-Konfiguration fehlt: klare Meldung, kein Startversuch ──────
echo "== Update bei fehlender CT-Konfiguration =="
: > "$MOCK_STATE"
: > "$MOCK_STATE.missing"
OUT_M="$(MOCK_HAVE_CT=1 bash "$INSTALLER" --update 2>&1 || true)"
if echo "$OUT_M" | grep -q "existiert nicht"; then
  ok "--update meldet fehlende Container-Konfiguration klar"
else
  bad "--update erkennt fehlende Config nicht: $OUT_M"
fi
if echo "$OUT_M" | grep -q "starte ihn"; then
  bad "--update versucht bei fehlender Config trotzdem zu starten: $OUT_M"
else
  ok "--update startet bei fehlender Config nicht"
fi
rm -f "$MOCK_STATE.missing"

# ── 6d. Frisch-Install: Payload als Tarball pushen + im Container entpacken ───
echo "== Payload-Transfer per Tarball (Frischinstall) =="
: > "$MOCK_STATE"
bash "$INSTALLER" --id 250 >/dev/null 2>&1 || true
if grep -q "mkdir -p /root/domainradar-src" "$MOCK_STATE"; then
  ok "mkdir -p /root/domainradar-src wird im Container ausgeführt"
else
  bad "mkdir -p /root/domainradar-src fehlt im Zustandslog"
fi
LN_MKDIR="$(grep -n 'mkdir -p /root/domainradar-src' "$MOCK_STATE" | head -1 | cut -d: -f1)"
LN_PUSH="$(grep -n 'pct push' "$MOCK_STATE" | head -1 | cut -d: -f1)"
if [ -n "$LN_MKDIR" ] && [ -n "$LN_PUSH" ] && [ "$LN_MKDIR" -lt "$LN_PUSH" ]; then
  ok "mkdir steht vor dem ersten pct push"
else
  bad "mkdir steht nicht vor dem ersten pct push (mkdir=$LN_MKDIR push=$LN_PUSH)"
fi
if grep -q "domainradar-payload.tar.gz" "$MOCK_STATE" && grep -q "tar -xzf /root/domainradar-payload.tar.gz" "$MOCK_STATE"; then
  ok "Payload wird als Tarball gepusht und im Container entpackt (kein Verzeichnis-pct-push)"
else
  bad "Tarball-Payload/-Entpacken fehlt im Zustandslog"
fi
if grep -qE "pct push 250 .* /root/domainradar-src/(src|web|systemd)$" "$MOCK_STATE"; then
  bad "Verzeichnis-Push auf /root/domainradar-src/<dir> ist noch aktiv"
else
  ok "kein pct push von Verzeichnissen mehr"
fi

echo
echo "-----------------------------------------"
echo " bestanden: $PASS   fehlgeschlagen: $FAIL"
[ "$FAIL" -eq 0 ] && { echo " Alle Mock-Prüfungen grün."; exit 0; } || exit 1
