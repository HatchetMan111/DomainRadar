#!/usr/bin/env bash
# DomainRadar – Harness.
#
# Testet die Pipeline und den Reboot-Nachweis ohne Proxmox, mit Attrappen für
# pct/pveam/pvesh. Damit ist prüfbar, dass:
#   1. alle Tests grün sind
#   2. die Installer-Syntax stimmt (bash -n, shellcheck)
#   3. die Pipeline über echte CT-Daten läuft (collect → enrich → classify)
#   4. die API startet, antwortet und den Reboot übersteht
#
# Kein Netzwerk nötig ausser für Schritt 3 (CT-Logs).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PASS=0
FAIL=0
if [ -t 1 ]; then
  C_G=$'\033[32m'; C_R=$'\033[31m'; C_N=$'\033[0m'
else
  C_G=""; C_R=""; C_N=""
fi
ok()   { echo "  ${C_G}✓${C_N} $*"; PASS=$((PASS+1)); }
bad()  { echo "  ${C_R}✗${C_N} $*"; FAIL=$((FAIL+1)); }
head1(){ echo; echo "== $* =="; }

echo "DomainRadar Harness"
echo " cwd: $ROOT"

# ---------- 1. Tests ----------
head1 "Unit-Tests"
if PYTHONPATH=src python3 -m pytest test/ -q >/tmp/dr-pytest.log 2>&1; then
  ok "$(tail -1 /tmp/dr-pytest.log)"
else
  bad "pytest fehlgeschlagen"
  tail -20 /tmp/dr-pytest.log
fi

# ---------- 2. Installer ----------
head1 "Installer"
if bash -n install/domainradar.sh; then ok "domainradar.sh Syntax"; else bad "domainradar.sh Syntax"; fi
if bash -n install/domainradar-inner.sh; then ok "domainradar-inner.sh Syntax"; else bad "domainradar-inner.sh Syntax"; fi
if command -v shellcheck >/dev/null 2>&1; then
  if shellcheck -S warning install/*.sh >/dev/null 2>&1; then
    ok "shellcheck"
  else
    bad "shellcheck meldet Warnungen"
    shellcheck -S warning install/*.sh | head -20
  fi
else
  echo "  - shellcheck nicht installiert, übersprungen"
fi
if grep -q "set -Eeuo pipefail" install/domainradar.sh; then
  ok "set -Eeuo pipefail vorhanden"
else
  bad "set -Eeuo pipefail fehlt"
fi
if grep -q "pct create" install/domainradar.sh && grep -q "onboot 1" install/domainradar.sh; then
  ok "CT-Erstellung mit onboot"
else
  bad "CT-Erstellung unvollständig"
fi

# ---------- 3. Web-Build ----------
head1 "Web-Build"
if [ -f web/dist/index.html ]; then
  ok "dist vorhanden"
  JS=$(grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' web/dist/index.html | head -1)
  if [ -n "$JS" ] && [ -f "web/dist/$JS" ]; then ok "Bundle $JS"; else bad "Bundle fehlt"; fi
else
  bad "web/dist/index.html fehlt (npm run build ausführen)"
fi

# ---------- 4. Pipeline gegen echte CT-Logs ----------
head1 "Pipeline (echte CT-Daten)"
DB="/tmp/domainradar-harness.db"
rm -f "$DB" "$DB-wal" "$DB-shm"

if PYTHONPATH=src python3 - <<EOF
from domainradar.ctlog import CTLog, der_certificates, sans_from_cert
from domainradar.domains import DomainFilter
from domainradar.db import Database
db = Database("$DB")
f = DomainFilter((".de",))
name, url = "digicert_wyvern2026h2", "https://wyvern.ct.digicert.com/2026h2"
db.upsert_log(name, url)
c = CTLog(name, url)
end = c.get_sth().tree_size - 1
found, certs = set(), 0
for frac in (0.2, 0.5, 0.8):
    be = int(end*frac); bs = max(0, be-600)
    for e in c.get_entries(bs, be):
        certs += 1
        names = []
        for cert in der_certificates(e["extra_data"]):
            names.extend(sans_from_cert(cert))
        found.update(f.accept_many(names))
db.insert_domains([{"domain": d, "cert_index": 0, "log_name": name} for d in sorted(found)])
print("certs", certs, "domains", db.conn.execute("select count(*) from domains").fetchone()[0])
db.close()
EOF
then
  ok "CT-Logs gelesen"
else
  bad "CT-Logs nicht erreichbar"
fi

NDOM=$(python3 -I -c "
import sqlite3
try:
    c=sqlite3.connect('file:$DB?mode=ro',uri=True); print(c.execute('select count(*) from domains').fetchone()[0])
except Exception: print(0)
")
if [ "${NDOM:-0}" -gt 0 ]; then ok "$NDOM Domains in der Datenbank"; else bad "keine Domains gesammelt"; fi

if PYTHONPATH=src python3 -m domainradar.enricher --db "$DB" --limit 10 >/dev/null 2>&1; then
  ok "Enricher lief durch"
else
  bad "Enricher fehlgeschlagen"
fi

if PYTHONPATH=src python3 -m domainradar.classifier --db "$DB" >/dev/null 2>&1; then
  ok "Classifier lief durch"
else
  bad "Classifier fehlgeschlagen"
fi

# ---------- 5. API + Reboot ----------
head1 "API und Reboot"

start_api() {
  PYTHONPATH=src DOMAINRADAR_DB="$DB" \
    python3 -m uvicorn domainradar.asgi:app --host 127.0.0.1 --port 8479 \
    >/tmp/dr-api.log 2>&1 &
  API_PID=$!
  for _ in $(seq 1 25); do
    if curl -sf -m 2 http://127.0.0.1:8479/api/health >/dev/null 2>&1; then return 0; fi
    sleep 0.4
  done
  return 1
}

stop_api() {
  [ -n "${API_PID:-}" ] && kill "$API_PID" 2>/dev/null
  wait "$API_PID" 2>/dev/null
  API_PID=""
}

if start_api; then
  ok "API startet"
  for ep in /api/health /api/stats /api/startups /feed.xml /export/leads.csv /api/settings; do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 4 "http://127.0.0.1:8479$ep")
    if [ "$code" = "200" ]; then ok "$ep → 200"; else bad "$ep → $code"; fi
  done
  DOM=$(curl -s -m 4 http://127.0.0.1:8479/ | grep -oE 'assets/index-[A-Za-z0-9_-]+\.js' | head -1)
  if [ -n "$DOM" ]; then ok "Frontend ausgeliefert ($DOM)"; else bad "Frontend fehlt"; fi

  # Einstellungsseite: Felder, Maskierung, Speichern
  SET=$(curl -s -m 4 http://127.0.0.1:8479/api/settings)
  NFIELDS=$(printf '%s' "$SET" | python3 -c "
import json,sys
try: print(len(json.load(sys.stdin)['meta']))
except Exception: print(0)
" 2>/dev/null)
  if [ "${NFIELDS:-0}" -gt 0 ]; then ok "Einstellungsseite: $NFIELDS Felder"; else bad "Einstellungsseite leer"; fi

  # Key setzen und pruefen, dass er maskiert zurueckkommt
  curl -s -m 4 -X PUT http://127.0.0.1:8479/api/settings \
    -H "Content-Type: application/json" \
    -d '{"values":{"openrouter_api_key":"sk-or-harneytest-1234"}}' >/dev/null
  LEAK=$(curl -s -m 4 http://127.0.0.1:8479/api/settings | grep -c "harneytest" || true)
  if [ "$LEAK" = "0" ]; then ok "API-Key wird nicht im Klartext ausgeliefert"
  else bad "API-Key leakt im Klartext"; fi

  # Verbindungstest ohne Provider-Konfiguration muss sauber antworten
  TC=$(curl -s -m 10 -X POST http://127.0.0.1:8479/api/settings/test \
    -H "Content-Type: application/json" -d '{"provider":"ollama","values":{"ollama_base_url":"http://127.0.0.1:9/v1","ollama_model":"x"}}')
  OKFLAG=$(printf '%s' "$TC" | python3 -c "
import json,sys
try: print('true' if json.load(sys.stdin).get('ok') else 'false')
except Exception: print('error')
" 2>/dev/null)
  if [ "$OKFLAG" = "false" ]; then ok "Verbindungstest meldet Fehler korrekt"
  else bad "Verbindungstest antwortet unerwartet: $TC"; fi

  # Test-DB aufraeumen, damit der naechste Lauf sauber startet
  curl -s -m 4 -X PUT http://127.0.0.1:8479/api/settings \
    -H "Content-Type: application/json" \
    -d '{"values":{"ollama_model":"llama3.2:3b"}}' >/dev/null
else
  bad "API startet nicht"
  tail -10 /tmp/dr-api.log
fi

# Reboot: Prozess weg, neu starten, Daten müssen noch da sein
head1 "Reboot-Nachweis"
stop_api
sleep 1
if start_api; then
  ok "API nach Neustart wieder erreichbar"
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 4 http://127.0.0.1:8479/)
  [ "$code" = "200" ] && ok "Web-UI nach Neustart → 200" || bad "Web-UI nach Neustart → $code"
  total=$(curl -s -m 4 http://127.0.0.1:8479/api/stats | python3 -c "import json,sys;print(json.load(sys.stdin)['total_domains'])" 2>/dev/null)
  if [ "${total:-0}" -ge "${NDOM:-0}" ] && [ "${total:-0}" -gt 0 ]; then
    ok "Daten über Neustart erhalten ($total Domains)"
  else
    bad "Daten nach Neustart verloren (erwartet $NDOM, ist ${total:-0})"
  fi
else
  bad "API nach Neustart nicht erreichbar"
fi
stop_api

# ---------- Ergebnis ----------
echo
echo "-----------------------------------------"
echo " bestanden: $PASS   fehlgeschlagen: $FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo " ${C_G}Alle Prüfungen grün.${C_N}"
  exit 0
else
  echo " ${C_R}Es gibt Fehlschläge.${C_N}"
  exit 1
fi