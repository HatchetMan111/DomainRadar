# 📡 DomainRadar — Proxmox-VE-Installer

Lokaler Radar für neu beobachtete `.de`-Domains. Liest Certificate-Transparency-Logs
aus, ruft die neuen Webseiten ab und ordnet ein, was sie anbieten und wer sie betreibt.
**Vollständig lokal** — keine Cloud, keine Weitergabe, eine SQLite-Datei.

> **Was das ist:** eine Beobachtungsdatenbank, kein WHOIS-Ersatz.
> **Was das nicht ist:** ein Verzeichnis aller neuen `.de`-Domains. CT-Logs zeigen
> nur Domains, für die ein TLS-Zertifikat ausgestellt wurde. Geparkte Domains ohne
> HTTPS fehlen. Das Feld heißt deshalb „Zuerst gesehen", nicht „Registriert".

---

## 🚀 Installation (Einzeiler, auf dem Proxmox-Host als root)

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/DomainRadar/main/install/domainradar.sh)"
```

### Was passiert

| Schritt | Detail |
|---|---|
| CT-ID | automatisch die nächste freie (`pvesh get /cluster/nextid`, überschreibbar via `--id`) |
| Hostname | `domainradar` |
| Template | Debian 13, Architektur-gefiltert, bei Bedarf `pveam download` |
| Ressourcen | 2 vCPU · 2048 MB RAM · 512 MB Swap · 8 GB Disk |
| Features | `unprivileged=1`, `nesting=1,keyctl=1`, `onboot=1`, DHCP |
| Dienste | `domainradar-api` (systemd, `Restart=always`) + 4 Timer |
| Web-UI | `http://<CT-IP>:8477` |
| Update | `--update` erkennt den Container am Hostnamen |

Mit **Ollama** (lokales LLM für die Klassifikation) direkt mitinstallieren:

```bash
bash -c "$(wget -qLO - https://raw.githubusercontent.com/HatchetMan111/DomainRadar/main/install/domainradar.sh)" --ollama
```

Das dimensioniert den Container auf 4 vCPU / 8 GB / 24 GB hoch und lädt
`llama3.2:3b` herunter. Ohne `--ollama` läuft die Klassifikation regelbasiert,
die Oberfläche bleibt aber voll nutzbar.

Optionen hinter dem schließenden Anführungszeichen:

```bash
...domainradar.sh --id 250 --memory 8192 --cores 4 --ollama --port 8477
```

---

## 🔧 Verwaltung

```bash
# Status
bash -c "$(wget -qLO - .../install/domainradar.sh)" -- --status

# Aktualisieren
bash -c "$(wget -qLO - .../install/domainradar.sh)" -- --update

# Deinstallieren (fragt nach)
bash -c "$(wget -qLO - .../install/domainradar.sh)" -- --uninstall

# Deinstallieren ohne Rückfrage
bash -c "$(wget -qLO - .../install/domainradar.sh)" -- --uninstall --yes

# Voller Fehlersuche-Modus
bash -c "$(wget -qLO - .../install/domainradar.sh)" -- --debug
```

| Option | Bedeutung |
|---|---|
| `--id <id>` | Container-ID fest vorgeben (Alias: `--ctid`) |
| `--hostname <name>` | Hostname im Container (Default: `domainradar`) |
| `--cores <n>` | vCPU-Kerne (Default: 2) |
| `--memory <mb>` | RAM in MB (Default: 2048) |
| `--swap <mb>` | Swap in MB (Default: 512) |
| `--rootfs <gb>` | Disk in GB (Default: 8) |
| `--storage <name>` | Storage-Pool (Default: `local-lvm`) |
| `--bridge <name>` | Bridge (Default: `vmbr0`) |
| `--ip <cidr>` | Statische IP statt DHCP |
| `--port <n>` | Web-Port (Default: 8477) |
| `--update` | vorhandenen Container aktualisieren |
| `--status` | Status und URL anzeigen |
| `--uninstall [--yes]` | Container löschen |
| `--cron` | cron statt systemd-Timern |
| `--no-ollama` | Ollama nicht installieren (Default) |
| `--ollama` | Ollama installieren, Container hochdimensionieren |
| `--debug` | `bash -x`, vollständige Fehlkette inkl. Container-Logs |

### Umgebungsvariablen

| Variable | Default | Bedeutung |
|---|---|---|
| `CTID` | nächste freie | Container-ID |
| `CORES` / `RAM` / `DISK` | 2 / 2048 / 8 | Ressourcen |
| `STORAGE` | `local-lvm` | Storage |
| `UNPRIVILEGED` | `1` | `0` nur bei UTS-Needed-Setups |
| `TZ` | `Europe/Berlin` | Zeitzone im Container |

---

## ⚙️ Konfiguration

### Einstellungsseite (empfohlen)

In der Oberfläche oben auf **Einstellungen**. 14 Felder in drei Gruppen:

| Gruppe | Inhalt |
|---|---|
| **LLM** | Provider-Kaskade, Eskalationsschwelle, Ollama-URL + Modell, OpenRouter-URL + Modell + API-Key |
| **Erfassung** | Zertifikate pro Lauf, Domains pro Durchgang, HTTP-Timeout, robots.txt beachten, Domains pro LLM-Aufruf |
| **Auswertung** | Zielkategorien für Leads, Domains pro Änderungslauf |

Zwei Knöpfe „**Verbindung testen**" (bei Ollama und OpenRouter) bauen einen echten
Minimal-Aufruf und unterscheiden vier Fälle:

| Antwort | Bedeutung |
|---|---|
| „Verbindung steht – Antwort: OK" | Endpunkt und Modell passen |
| „HTTP 404 Not Found (Modellname prüfen)" | Endpunkt richtig, Modell nicht vorhanden |
| „HTTP 401 Unauthorized (API-Key falsch …)" | Key falsch oder abgelaufen |
| „Nicht erreichbar (URLError …)" | Endpunkt läuft nicht |

Speichern schreibt in die Datenbank. Die Zeitpläne sind `oneshot`-Einheiten und
starten für jeden Lauf neu — **Änderungen greifen ohne Neustart** beim nächsten Lauf.

### Datei-Konfiguration

Alternativ `/opt/domainradar/.env` (Modus 600). **Achtung Rangfolge:** Werte aus
der Oberfläche überschreiben die Datei. Wer die Datei ändert und trotzdem alte
Werte sieht, hat in der Oberfläche gespeichert.

| Variable | Default | Bedeutung |
|---|---|---|
| `DOMAINRADAR_DB` | `/opt/domainradar/data/domainradar.db` | Datenbank |
| `DOMAINRADAR_PORT` | `8477` | Web-Port |
| `LLM_CHAIN` | `ollama,openrouter` | Provider-Reihenfolge |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434/v1` | lokales LLM |
| `OLLAMA_MODEL` | `llama3.2:3b` | Modell für Stufe 1 |
| `OPENROUTER_MODEL` | `deepseek/deepseek-chat` | Modell für Stufe 2 |
| `OPENROUTER_API_KEY` | *(leer)* | Key für Stufe 2 |
| `TARGET_CATEGORIES` | `software,energy,smart-home,climate` | Zielkategorien für Leads |

**LLM-Kaskade:** Erst Ollama (gratis, lokal). Nur wenn die Konfidenz unter
`llm_escalate_below` (Default 0,75) liegt, wird nach OpenRouter eskaliert.
Ohne Key und ohne Ollama läuft eine regelbasierte Klassifikation weiter — die
App bricht nie ab.

---

## 🗂️ Wo liegt was?

| Pfad (im CT) | Inhalt |
|---|---|
| `/opt/domainradar/` | Anwendungscode, venv, `.env` |
| `/opt/domainradar/data/domainradar.db` | Datenbank (Backup = diese Datei) |
| `/opt/domainradar/web/dist/` | gebaute Oberfläche (React) |
| `/etc/systemd/system/domainradar-*.timer` | Zeitpläne |
| `/var/log/domainradar-setup.log` | Installationslog |

### Zeitpläne

| Timer | Intervall | Zweck |
|---|---|---|
| `domainradar-collect.timer` | alle 5 min | CT-Logs lesen |
| `domainradar-enrich.timer` | alle 10 min | Webseiten abrufen |
| `domainradar-classify.timer` | täglich 03:00 | Einordnen |
| `domainradar-changes.timer` | montags 05:00 | Änderungsreport |

Alle mit `Persistent=true`, verpasste Läufe werden nachgeholt.

---

## 🧭 Bedienung

**Radar** — die Beobachtungsdatenbank.

**Einstellungen** — LLM-Provider, API-Keys, Erfassungslimits, Zielkategorien.
Mit Verbindungstest pro Provider.

Innerhalb des Radars zwei Ansichten:

**Markt** — alle beobachteten Domains, nach Kategorie und Zeit filterbar.

**Leads** — nur Domains mit erkennbarem Produkt und Betreiber, inklusive
Firmenname, Rechtsform und einem erklärbaren Score:

```
score = 0,35 · eigenes Produkt
      + 0,25 · Konfidenz
      + 0,20 · Impressum vorhanden
      + 0,10 · Seite in 30 Tagen aktiv
      + 0,10 · Zielkategorie
```

Die Zielkategorien stehen in `TARGET_CATEGORIES` (`.env`), damit sich die
Lead-Sicht ohne Codeänderung auf die eigene Branche drehen lässt.

Export fürs CRM: **`/export/leads.csv`**.

---

## 🔄 Update

```bash
bash -c "$(wget -qLO - .../install/domainradar.sh)" -- --update
```

Der Installer erkennt den Container am Hostnamen, aktualisiert Code und Abhängigkeiten
und lässt die Datenbank unangetastet.

## 💾 Backup

```bash
vzdump <CT-ID> --mode snapshot
```

Oder nur die Datenbank:

```bash
pct exec <CT-ID> -- sqlite3 /opt/domainradar/data/domainradar.db ".backup '/tmp/dr.db'"
pct pull <CT-ID> /tmp/dr.db ./domainradar-backup.db
```

## 🗑️ Deinstallation

```bash
bash -c "$(wget -qLO - .../install/domainradar.sh)" -- --uninstall
```

Löscht den Container vollständig. Die Datenbank liegt dann nicht mehr vor —
vorher sichern, wenn die Beobachtungen gebraucht werden.

---

## 🧪 Testen

```bash
bash harness/run.sh          # 26 Prüfungen: Pipeline, API, Reboot
bash harness/installer-mock.sh  # 11 Prüfungen: Installer ohne Proxmox
PYTHONPATH=src python3 -m pytest test/ -q   # 171 Unit-Tests
```

### Was tatsächlich geprüft wurde

| Prüfung | Ergebnis |
|---|---|
| Unit-Tests | **171 grün** (ctlog, collector, enricher, classifier, config, api) |
| `harness/run.sh` | **26/26** – inkl. Pipeline gegen echte CT-Logs und Reboot-Nachweis |
| `harness/installer-mock.sh` | **11/11** – mit `pct`/`pvesh`/`pveam`-Attrappen |
| `bash -n` + `shellcheck -S warning` | sauber, beide Skripte |
| Frontend-Build aus frischem Tarball | `npm ci` + `npm run build` → 154 KB JS / 50 KB gzip |
| `requirements.txt` in frischem venv | fastapi 0.115.6, uvicorn 0.32.1, cryptography 44.0.0 |
| Wheels für Python 3.13 (Debian 13) | vorhanden für uvloop, httptools, pyyaml, cryptography (abi3) |
| Repo-URLs nach dem Push | `install/domainradar.sh`, `-inner.sh`, Tarball → je HTTP 200 |

**Der Reboot-Nachweis** ist Teil von `harness/run.sh`: API starten, Prozess beenden,
neu starten, danach müssen `/` und `/api/health` wieder 200 liefern und die
Domains noch in der Datenbank stehen.

**Was noch nicht geprüft ist:** der echte Lauf auf einem Proxmox-Host. Der
Installer ist syntaktisch, per Attrappen und gegen die echten GitHub-URLs geprüft —
aber `pct create` auf echter Hardware hat er noch nicht gesehen.

```bash
# Phase-0-Stichprobe: .de-Quote über verteilte CT-Blöcke messen
PYTHONPATH=src python3 -m domainradar.spike --certs 400 --blocks 5
```

---

## 🔒 Datenschutz

Das ist keine Formsache, sondern der Kern der Anwendung:

- **Öffentliche Quellen.** Zertifikats-Logs sind öffentlich, Webseiten sind öffentlich.
  Es werden keine Daten gekauft oder erschlichen.
- **API-Keys bleiben lokal.** Keys werden in der Datenbank gespeichert und maskiert
  an die Oberfläche ausgeliefert (`sk-or-…` → `sk**************t999`). Ein leer
  gelassenes Feld beim Speichern lässt den bestehenden Wert unverändert.
- **Datensparsamkeit.** Von Impressumsdaten wird nur Firmenname und Rechtsform
  gespeichert. Adresse, Telefon und E-Mail werden **nicht** erfasst.
- **Alles lokal.** Keine Weitergabe an Dritte, keine Cloud, keine Telemetrie.
- **Zweckbindung.** Die Lead-Sicht ist für eigene Marktanalyse gedacht. Für
  Werbekontakt braucht es eine Rechtsgrundlage — Impressumsdaten sind dafür
  nicht automatisch eine.
- **Recht auf Löschung.** Jede Zeile lässt sich einzeln entfernen:
  `sqlite3 … "DELETE FROM domains WHERE domain='beispiel.de'"`.

---

## 🐞 Fehlersuche

```bash
# Service-Status
pct exec <CT-ID> -- systemctl status domainradar-api

# Logs der API
pct exec <CT-ID> -- journalctl -u domainradar-api -n 100 --no-pager

# Läufe der Pipeline
pct exec <CT-ID> -- journalctl -u domainradar-collect -n 50 --no-pager

# Manuell eine Stufe starten
pct exec <CT-ID> -- bash -c 'cd /opt/domainradar && set -a && . ./.env && set +a && venv/bin/python -m domainradar.collector -v'
```

Der Installer gibt bei Fehlern immer die **vollständige Fehlkette** aus: Exit-Code,
Zeile, Funktionsaufrufe und die letzten 20 Zeilen des Installationslogs.
Mit `--debug` zusätzlich `bash -x`.

---

## 📜 Lizenz

MIT. Siehe [LICENSE](LICENSE).

Die Anwendung wurde so gebaut, dass sie die Methode eines kommerziellen Anbieters
nachbildet — ohne dessen Daten zu nutzen. Die Terms dieses Anbieters untersagen
Bulk-Kopie ausdrücklich; hier wird nichts kopiert, sondern aus öffentlichen
Zertifikats-Logs selbst erhoben.