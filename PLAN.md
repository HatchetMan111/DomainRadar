# DomainRadar — Projektplan

Ein lokaler Radar für neue `.de`-Domains, betrieben als LXC auf Proxmox.
Baut die Methode von NovumSignals nach: **neu ausgestellte TLS-Zertifikate beobachten,
Webseiten lesen, einordnen** — aber vollständig eigenständig und auf eigener Hardware.

Stand: 2026-11-20 · Ziel: `/home/phil/DomainRadar-Proxmox`

---

## 1. Ausgangslage (gemessen, nicht geschätzt)

| Frage | Messung | Folge |
|---|---|---|
| Gibt es eine `.de`-Zone? | **Nein.** DENIC gibt sie "auch Dritten nicht" heraus (Datenschutz-Argument). CZDS deckt nur gTLDs, AXFR wird verweigert. | Kein Zonenfile-Weg. |
| Reicht crt.sh? | **Nein.** `crt.sh/?q=%25.de&output=json` liefert 3.226 Zeilen von **2008–2013**, nicht die Gegenwart. `deduplicate=Y` → 502/Timeout. | crt.sh nur für Einzelabfragen, nicht als Quelle. |
| Geht CT direkt? | **Ja.** RFC-6962-Endpunkte antworten ohne Key: Cloudflare Nimbus2026, DigiCert Wyvern2026h2, Sectigo Elephant2026h2. `get-sth` + `get-entries`. | Das ist die Datenquelle. |
| Volumen? | ~2,1 Mio. Zertifikate/Tag pro Log, einstellig viele Logs relevant | ~212 Cert/s auf 1 Kern → 1–2 Kerne reichen |
| `.de`-Quote? | **1,07 %** der Unique-Namen (9 von 845, 5 verteilte Stichproben) | ~5.000–8.000 neue `.de`-Bases/Tag |
| Wahrheit? | DENIC: 18.371.249 Domains, +~100k/Monat ≈ 3.300/Tag netto | Größenordnung bestätigt |

**Phase-0-Messung (2026-11-20/23, echte CT-Daten aus DigiCert Wyvern):**

| Beobachtung | Wert | Bedeutung |
|---|---|---|
| Cert mit SAN | ~34–52 % | Rest sind Pre-Certs / Testzertifikate ohne SAN |
| Unique-Namen | 845 (5 Blöcke) bzw. 10.586 (16k-Block) | stark zeitabhängig |
| `.de`-Namen | **1,07 %** (9/845) | verteilte Stichprobe, belastbar |
| `.de`-Basis-Domains | 9 | echt, u. a. uni-regensburg.de, klenanzeigen.de |
| Dubletten über Logs | 0 | Sectigo brachte 0 neue – Logs überlappen kaum |
| Durchsatz | ~180 Cert/s auf einem Kern | 1 Kern für mehrere Logs genug |

**Wichtige Erkenntnis über die Methode:** CT-Logs sind zeitlich sortiert. Ein
einzelner Block kann eine Quote von 0,00 % oder 1,07 % liefern — je nachdem, ob
er Let's-Encrypt-Testzertifikate (`*.woodpecker.testing.letsencrypt.org`) oder
echte Registrierungen enthält. **Deshalb ist die Quote nur über mehrere
verteilte Stichproben messbar**, und genau so ist Phase 0 aufgebaut.

Die 9 Treffer sind echt und zeigen die Qualität des Filters: keine Dubletten,
keine Subdomain-Reste, keine Freitext-Treffer.

**Zentrale Einschränkung, die ins UI gehört:** CT sieht nur, *wo ein Zertifikat ausgestellt wurde*.
Geparkte oder rein per DNS erreichbare Domains fehlen. Wir nennen das Feld deshalb
**"Zuerst gesehen"**, nicht "Registriert" — genau wie NovumSignals.

---

## 2. Architektur

```
Proxmox-Host
└── LXC "domainradar" (Debian 13, unprivileged, 4 Kern / 8 GB / 24 GB)
    ├── collect.py      CT-Logs folgen, SANs filtern, .de-Bases erkennen
    ├── enrich.py       Homepage + /impressum holen, parsen, Parken erkennen
    ├── classify.py     Provider-Kaskade: Ollama lokal → OpenRouter bei Bedarf
    ├── changes.py      wöchentlicher Diff gegen Vorwoche
    ├── api.py          FastAPI, read-only JSON
    ├── web/            statisches Frontend (kein Build nötig)
    └── ollama          optionales lokales Modell, CPU-Inferenz
```

**Warum nativ (kein Docker):** Die App ist ein einzelnes Python-Paket mit SQLite.
Docker-in-LXC würde `nesting=1` + Volume-Mounts + eine weitere Fehlerquelle bedeuten,
ohne Gegenleistung. Lighter: venv + systemd + cron. (Docker wäre möglich, ist aber
nicht der Default.)

**Warum SQLite:** 1–2 Mio. Zeilen/Jahr, alles Single-Writer (cron), WAL-Mode.
Kein Serverdienst, Backup = eine Datei. Postgres wäre Overkill.

**Warum 8 GB RAM:** Ollama hält ein Modell dauerhaft im Speicher. `llama3.2:3b` (Q4)
braucht ~2,5 GB, `llama3.1:8b` ~6 GB. 8 GB erlaubt den Default *und* das größere Modell
zum Umschalten. Wird Ollama ausgelagert (`OLLAMA_BASE_URL` zeigt woanders hin),
reichen 2 GB — dann greift der Spec-Default (2 Kern / 2 GB / 8 GB).

### 2.1 Frontend: React + Vite

Build-Zeit-Compiler, kein Dev-Server im Betrieb:

```
web/ (React + Vite, Quelle)
   │  npm ci && npm run build        ← einmalig im Installer
   ▼
web/dist/  →  /opt/domainradar/web/  (statisch, von FastAPI ausgeliefert)
```

- Node **nur zur Installationszeit** im LXC nötig (`apt install nodejs npm`),
  danach liegt nur statisches `dist/` da. Der Betrieb braucht kein Node.
- FastAPI liefert `dist/` als `StaticFiles` aus und bedient zusätzlich
  `/api/*`. Der Service heißt weiter `domainradar-api`.
- Kein Node im Betrieb heißt: weniger Angriffsfläche, kein zweiter Prozess,
  Neustart des Containers berührt nur Python.

### 2.2 systemd + cron

Beides, mit klarer Trennung:

| Einheit | Zweck | Restart |
|---|---|---|
| `domainradar-api.service` | FastAPI + statisches Web | `Restart=always`, `After=network-online.target` |
| `domainradar-collect.timer` | CT-Logs pollen (alle 5 min) | `Persistent=true` |
| `domainradar-enrich.timer` | Seiten lesen (alle 10 min) | `Persistent=true` |
| `domainradar-classify.timer` | Klassifizieren (nachts 03:00) | `Persistent=true` |
| `domainradar-changes.timer` | Wöchentlicher Diff (Mo 05:00) | `Persistent=true` |

`Persistent=true` holt versäumte Läufe nach dem Boot nach. `OnFailure=`-Handler
schreibt Fehler ins Journal. Cron ist zusätzlich möglich (`--cron` im Installer),
weil du explizit danach gefragt hast — Default ist aber systemd, weil Timers
Logs, Abhängigkeiten und Nachhol-Läufe sauberer lösen.

---

## 3. Datenmodell

```sql
-- CT-Logs, die wir folgen; tree_size ist der High-Water-Mark
CREATE TABLE logs (
  id INTEGER PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  url TEXT NOT NULL,
  mmd INTEGER,
  tree_size INTEGER DEFAULT 0,
  updated_at TEXT
);

-- ein Roh-Zertifikat (dedupliziert über log_id + cert_index)
CREATE TABLE certs (
  id INTEGER PRIMARY KEY,
  log_id INTEGER NOT NULL,
  cert_index INTEGER NOT NULL,
  not_before TEXT,
  not_after TEXT,
  sans TEXT,               -- JSON-Array aller SANs
  fingerprint TEXT,        -- SHA256 über das DER
  seen_at TEXT,
  UNIQUE(log_id, cert_index)
);

-- eine Basis-Domain (e.g. "epsi-app.de"), über Logs hinweg dedupliziert
CREATE TABLE domains (
  domain TEXT PRIMARY KEY,
  first_seen TEXT,          -- "zuerst gesehen", nicht "registriert"
  first_cert TEXT,
  country TEXT,
  -- Abrieb
  is_parked INTEGER,
  http_status INTEGER,
  final_url TEXT,
  title TEXT,
  meta_description TEXT,
  lang TEXT,
  page_hash TEXT,
  text_len INTEGER,
  -- Klassifikation
  category TEXT,
  segment TEXT,             -- b2c / b2b
  business_type TEXT,
  sells_own_product INTEGER,
  confidence REAL,
  summary_de TEXT,
  -- Betreiber (bewusst minimal, siehe DSGVO)
  company_name TEXT,
  legal_form TEXT,
  classified_at TEXT,
  last_checked TEXT
);

CREATE TABLE changes (
  id INTEGER PRIMARY KEY,
  domain TEXT NOT NULL,
  kind TEXT NOT NULL,       -- page / pricing / launch / funding / hiring
  detected_at TEXT,
  detail TEXT
);

CREATE TABLE page_cache (
  domain TEXT PRIMARY KEY,
  html TEXT,
  fetched_at TEXT
);
```

SQLite im WAL-Modus, `synchronous=NORMAL`, tägliches `VACUUM` sonntags.

---

## 4. Pipeline im Detail

### 4.1 collect.py — CT folgen

1. `GET {log}/ct/v1/get-sth` → `tree_size`, `timestamp`
2. Wenn `tree_size > gespeicherter tree_size`: `get-entries?start=N&end=N+999` in Blöcken
3. Pro Eintrag: `extra_data` dekodieren (Cloudflare hat 3 Byte Vorspann, Zertifikat ab Offset 3 —
   deshalb **Offset-Scan** statt festem Offset), DER laden, SANs extrahieren
4. Filtern: Regex `^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9-]+)*\.de$`, Wildcards `*.` strippen,
   IPs/UUIDs/Whitespace verwerfen (CT enthält ~25 % "de"-Müll wie *Estado de Mexico*)
5. Auf **Basis-Domain** reduzieren (`www.`→`epsi-app.de`), `INSERT OR IGNORE` in `domains`
   mit `first_seen = now()`
6. `tree_size` hochschreiben — erst nach erfolgreichem Commit

**Parallelität:** ein Worker pro Log (Prozess-Pool), damit ein langsamer Log keinen anderen blockiert.
Backoff 1-2-4-8 s, nach 5 Fehlern Log auf `stale` setzen und überspringen.

### 4.2 enrich.py — lesen

Nur für `domains` ohne `http_status`, in FIFO-Reihenfolge, **max. 4 parallel, 1 s Pause**:

- `GET http://→https://<domain>`, 10 s Timeout, max. 2 MB, Redirects verfolgen
- Extraktion: `<title>`, `meta description`, `<html lang>`, `<h1>`, erste 5.000 Zeichen Text
- `/impressum` + `/datenschutz` (falls verlinkt) nachziehen
- **Parking-Erkennung** *vor* dem LLM: Seiten < 500 Zeichen Text, bekannte Parking-Marker,
  `window.location`-Redirects auf Domain-Parking → `is_parked=1`, **kein LLM-Call**
- `page_hash` (SHA256 des Textes) speichern → Grundlage für `changes.py`

**Höflichkeit:** `robots.txt` beachten, eigene Kennung im User-Agent, 429/503 respektieren.

### 4.3 classify.py — zweistufige Provider-Kaskade

**Grundidee:** Der Bulk läuft kostenlos über Ollama. Nur wenn ein lokales Modell
unsicher ist, wird nach OpenRouter eskaliert. Das ist billiger *und* genauer als ein
pauschaler Anbieterwechsel — die teuren Calls treffen nur die Grenzfälle.

```
domain.text  ──►  Stufe 1: Ollama (lokal, gratis)
                        │
                        ├─ confidence >= 0,75  ──►  fertig
                        │
                        └─ confidence <  0,75
                                    │
                                    ▼
                          Stufe 2: OpenRouter (kostenpflichtig)
                                    │
                                    ├─ Erfolg  ──►  Ergebnis + provider="openrouter"
                                    └─ Fehler  ──►  Stufe-1-Ergebnis behalten
```

**Beide Stufen sprechen dasselbe Protokoll** (OpenAI-kompatibel, `chat_completion` nach
dem Muster aus CodeWaechter). Unterschied ist nur `base_url` + `model`:

| | Stufe 1 | Stufe 2 |
|---|---|---|
| URL | `http://127.0.0.1:11434/v1` | `https://openrouter.ai/api/v1` |
| Modell | `llama3.1:8b` (Default, überschreibbar) | `deepseek/deepseek-chat` o. ä. |
| Key | keiner | `OPENROUTER_API_KEY` |
| Kosten | 0 | ~0,0002 €/Domain, nur bei Grenzfällen |

**Schwellwerte** (in `.env`, nicht im Code):

| Variable | Default | Bedeutung |
|---|---|---|
| `LLM_CHAIN` | `ollama,openrouter` | Reihenfolge; `ollama` allein = rein lokal |
| `LLM_ESCALATE_BELOW` | `0.75` | Konfidenz, ab der eskaliert wird |
| `LLM_OPENROUTER_MODEL` | `deepseek/deepseek-chat` | Modell für Stufe 2 |
| `LLM_OPENROUTER_MAX_PER_DAY` | `200` | Kostenbremse |

**Antwortschema** (beide Stufen, 10 Domains pro Call):

```json
{
  "results": [
    {
      "domain": "…",
      "brand": "…",
      "category": "software",
      "segment": "b2c|b2b",
      "business_type": "SaaS",
      "sells_own_product": true,
      "confidence": 0.0,
      "summary_de": "…"
    }
  ]
}
```

**Ausfallverhalten:**

- Kein Ollama erreichbar, kein OpenRouter-Key → **deterministische Keyword-Klassifikation**
  (`db.py` setzt `category` aus Titel/Meta, `segment` aus `/pricing`, `/login` o. ä.).
  Die App läuft weiter, nur ohne Feintext. Kein Abbruch.
- OpenRouter-Limit erreicht → Stufe-1-Ergebnis bleibt, `classified_at` wird nicht gesetzt,
  sodass der Lauf am nächsten Tag erneut versucht.
- Antwort-JSON ungültig → ein Retry ohne `response_format`, dann Keyword-Fallback für diese Charge.
- Ergebnisse werden nach `page_hash` gecacht: identischer Seiteninhalt ⇒ kein zweiter Call,
  über alle Anbieter hinweg.

### 4.4 changes.py — was hat sich getan

Wöchentlich: `page_hash` neu berechnen, bei Abweichung Inhalt diffen und Keywords erkennen
(`Preis`, `Funded`, `Wir suchen`, `Launch`, `Stellenangebote`) → `changes`-Zeilen.

### 4.5 api.py + web

- FastAPI, read-only, `/api/startups?country=de&limit=…&sort=newest`
- Frontend: statisch, kein Node-Build (Plain HTML + Fetch) — hält den Container schlank
- Seiten: `/` (neueste), `/domain/<d>`, `/stats`, `/changes`, `/countries/de`
- RSS unter `/feed.xml`, `llms.txt` für Maschinen

### 4.6 Zwei Sichten auf denselben Bestand

**Eine Datenbank, zwei Filtermodi.** Kein zweiter Crawler, keine Doppelpflege —
`view=market` und `view=leads` schränken dieselben Zeilen unterschiedlich ein.

| | `view=market` (Default) | `view=leads` |
|---|---|---|
| Ziel | Was passiert im Markt? | Wen kann ich ansprechen? |
| `sells_own_product` | egal | **= 1** |
| `segment` | egal | **b2b** (umschaltbar auf b2c) |
| `is_parked` | mit anzeigen | ausblenden |
| Zusatzfelder | Kategorie, Trend, Änderungen | `company_name`, `legal_form`, `contact_url` |
| Sortierung | `first_seen` / `confidence` | `score_lead` (Produkt + Klarheit + Aktivität) |
| Darstellung | Karten, nach Kategorie geclustert | Tabelle mit Betreiber, Rechtsform, Score |

`score_lead` ist eine erklärbare Regelrechnung, keine Blackbox:

```
score = 0,35 * sells_own_product
      + 0,25 * confidence
      + 0,20 * hat_impressum
      + 0,10 * page_hash_changed_in_30d      ( Seite lebt )
      + 0,10 * category_in_target_☞(einstellbar)
```

Die Zielkategorien (`TARGET_CATEGORIES`) sind `.env`-Werte, damit sich der Lead-Modus
ohne Codeänderung auf die eigene Branche drehen lässt. Bei dir als LichtValley wäre
`software,energy,smart-home,climate` ein naheliegender Startwert.

Im UI ein Umschalter oben rechts, in der API ein `view=`-Parameter. Beide Sichten
stehen nebeneinander, keine Umschaltung verliert Daten.

**Export für Leads:** CSV/JSON unter `/export/leads.csv`, weil ein Lead-Generator ohne
Export in ein CRM nichts taugt. DSGVO-Hinweis steht neben dem Export: Impressum ist
öffentlich, aber zweckgebunden — keine Weitergabe an Dritte, keine Werbung ohne
Einwilligung. Das ist keine Formsache, sondern der Unterschied zwischen
Marktbeobachtung und Spam.

---

## 5. systemd-Timer (Default) und cron (optional)

Beide sind dünne Wrapper um dieselben Module, damit man von Hand testen kann
(`python -m domainradar.collect --once`).

**Default: systemd-Timer.** Grund: `Persistent=true` holt einen verpassten Lauf nach
dem Boot nach, Journald liefert Logs, und Abhängigkeiten sind ausdrückbar. Das ist bei
einem Cron-Ersatz der bessere Default.

`/etc/systemd/system/domainradar-collect.service`:

```ini
[Unit]
Description=DomainRadar – CT-Logs pollen
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=domainradar
Group=domainradar
WorkingDirectory=/opt/domainradar
ExecStart=/opt/domainradar/venv/bin/python -m domainradar.collect
Nice=10
```

`/etc/systemd/system/domainradar-collect.timer`:

```ini
[Unit]
Description=DomainRadar – CT-Logs pollen (alle 5 min)

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
AccuracySec=10s
Persistent=true

[Install]
WantedBy=timers.target
```

| Timer | Intervall | Zweck |
|---|---|---|
| `domainradar-collect.timer` | `OnUnitActiveSec=5min` | CT-Logs pollen |
| `domainradar-enrich.timer` | `OnUnitActiveSec=10min` | Seiten lesen |
| `domainradar-classify.timer` | `OnCalendar=*-*-* 03:00:00` | Klassifizieren |
| `domainradar-changes.timer` | `OnCalendar=Mon *-*-* 05:00:00` | Wöchentlicher Diff |

**Optional `--cron`:** erzeugt dieselben fünf Stufen unter `/etc/cron.d/domainradar`,
jede mit `flock -n` gegen Überlappung. Wenn du cron willst, nimmst du `--cron` — die
Module sind identisch, nur der Auslöser unterscheidet sich.

**Warum getrennte Stufen:** collect ist CPU-lastig und muss oft laufen, enrich ist
netzwerklastig und muss gedrosselt sein, classify kostet Geld und läuft nachts.
Getrennte Einheiten lassen sich einzeln abschalten und einzeln debuggen.

---

## 6. Projektstruktur

```
DomainRadar-Proxmox/
├── install/
│   ├── domainradar.sh          # outer: pct create, push, verify (install-signage-Muster)
│   └── domainradar-inner.sh    # läuft in CT: apt, node, venv, build, systemd
├── src/domainradar/
│   ├── __init__.py
│   ├── ctlog.py                # RFC 6962 Client (get-sth, get-entries, DER-Parsing)
│   ├── collector.py
│   ├── enricher.py
│   ├── classifier.py
│   ├── changes.py
│   ├── db.py                   # Schema + Migrationen
│   ├── report.py
│   └── api.py                  # FastAPI, liefert auch web/dist aus
├── web/                        # React + Vite
│   ├── package.json
│   ├── vite.config.js          # base: './', build.outDir: dist
│   ├── index.html
│   └── src/
│       ├── main.jsx
│       ├── App.jsx             # Markt-/Lead-Sicht, Filter, Suche
│       ├── api.js              # fetch-Wrapper
│       ├── DomainCard.jsx
│       ├── DomainTable.jsx
│       └── styles.css
├── systemd/
│   ├── domainradar-api.service
│   ├── domainradar-collect.service + .timer
│   ├── domainradar-enrich.service + .timer
│   ├── domainradar-classify.service + .timer
│   └── domainradar-changes.service + .timer
├── test/
│   ├── test_ctlog.py           # Fixture-Zertifikate, kein Netz
│   ├── test_collector.py
│   ├── test_enricher.py
│   ├── test_classifier.py
│   ├── test_db.py
│   └── test_api.py             # FastAPI TestClient
├── harness/run.sh              # Mock-E2E: fake pct/pveam/apt (Proxmox-Scripts-Muster)
├── PLAN.md
├── README.md
└── LICENSE
```

---

## 7. Installer

`install/domainradar.sh` — nach deinem kanonischen `install-signage.sh`-Muster:

| Schritt | Detail |
|---|---|
| Modus | `--ctid` / `--update` / `--status` / `--uninstall [--yes]` / `--debug` / `--cron` / `--help` |
| CT | `unprivileged=1`, `nesting=1,keyctl=1`, `onboot=1`, DHCP, Debian 13 |
| Hostname | `domainradar` |
| Ressourcen | Default **2 Kern · 2048 MB RAM · 8 GB Disk**; mit `OLLAMA=1` → 4 Kern / 8 GB / 24 GB |
| Storage | `local-lvm` automatisch, `STORAGE=` überschreibbar |
| ID | `pvesh get /cluster/nextid`, Fallback `pct list max + 1` |
| Update | `--update` erkennt CT am Hostnamen → idempotent |
| Verifikation | `verify_ct_started()` explizit (`pct status`), dann HTTP-200 auf Port 8477 |
| Beschreibung | HTML-CT-Description mit Link auf die Web-UI |

**Innere Schritte:**

1. `apt install python3-venv sqlite3 curl ca-certificates nodejs npm`
   (Node für den Vite-Build, `ollama` nur mit `OLLAMA=1`)
2. `npm ci && npm run build` in `/opt/domainradar/web` → `web/dist/`
3. venv + `pip install -r requirements.txt`
4. Nutzer `domainradar` anlegen, `chown -R`
5. `.env` mit `chmod 600`, `DATABASE=/opt/domainradar/data/domainradar.db`
6. systemd-Units kopieren, `systemctl daemon-reload`
7. `systemctl enable --now domainradar-api domainradar-collect.timer …`
8. einmalig `collect --once`, damit die UI beim ersten Aufruf Daten hat
9. nginx als Reverse Proxy auf `0.0.0.0:8477` (oder direkt uvicorn auf `0.0.0.0`)

`OLLAMA_BASE_URL`, `OPENROUTER_API_KEY` und `LLM_CHAIN` sind reine `.env`-Werte.
`OLLAMA=1` installiert Ollama + `llama3.2:3b` im Container.
`--no-openrouter` setzt die Kaskade auf `ollama` allein.

---

## 8. Phasen

| Phase | Inhalt | Abnahme |
|---|---|---|
| **0** | Spike: CT-Logs folgen, 100k Cert parsen, `.de`-Quote + CT-Latenz messen | echte Tag-Quote statt Hochrechnung |
| **1** | `ctlog.py` + `db.py` + `collector.py` + Tests | 24 h Dauerlauf ohne Datenverlust, Catch-up nach 24 h Pause |
| **2** | `enricher.py` | 100 Domains: Titel/Impressum/Parking-Flag korrekt |
| **3** | `classifier.py` + Provider-Kaskade | 20 Domains: Ollama vs. OpenRouter verglichen, Eskalation greift |
| **4** | `api.py` + `web/` + beide Sichten | UI zeigt `view=market` und `view=leads`, RSS gültig |
| **5** | `changes.py` | Diff erkennt eine absichtliche Änderung |
| **6** | Installer + `harness/run.sh` + README | Einzeiler auf Proxmox-Host, 100 % Assertions grün |

---

## 9. Risiken

| Risiko | Bewertung | Gegenmaßnahme |
|---|---|---|
| **CT-Logs blocken/drosseln** | mittel | Backoff, mehrere Logs, `stale`-Markierung; Phase 0 misst real |
| **`.de`-Quote weicht ab** | mittel | Phase 0 misst 24 h, Dimensionierung danach |
| **DSGVO: Impressum = Personendaten** | **hoch** | Default: nur `company_name` + `legal_form`. Adresse/Telefon **opt-in, default aus**. Keine Weitergabe, Löschkonzept, alles lokal. |
| **Kosten LLM** | niedrig | Parking-Filter + Hash-Cache; ohne Endpoint läuft es deterministisch |
| **Last auf Zielseiten** | niedrig | 4 parallel, 1 s Pause, robots.txt, 429 respektieren |
| **crt.sh wird doch gebraucht** | — | nur für Einzel-Lookups, nie als Quelle |

**Rechtlich:** Wir bauen eine eigene Datenbank aus öffentlichen Zertifikats-Logs und
öffentlich erreichbaren Webseiten. Kein Scraping von NovumSignals (deren Terms verbieten
Bulk-Kopie ausdrücklich). Werbetexte fremder Seiten werden nur für die eigene Auswertung
gecacht, nicht weiterveröffentlicht.

---

## 10. Was ich von dir brauche

1. **OpenRouter-Key** — `~/.secrets/omnirouter-key` (35 Zeichen) ist *kein* OpenRouter-Key
   (die beginnen mit `sk-or-`). Ohne Key läuft Stufe 2 nicht; die App fällt auf die
   deterministische Klassifikation zurück. Kannst du später in die `.env` eintragen.
2. **Ollama-Modell** — Default `llama3.2:3b` (~2,5 GB, schnell, für Kategorisierung
   meist ausreichend). Wenn Qualität auf 4 GB mehr liegen soll: `llama3.1:8b` (~6 GB).
   Umschalten ist ein `.env`-Wert, kein Umbau.
3. **Host + CT-ID** — ist `192.168.178.130` richtig? Dann würde ich die nächste freie ID
   über `pvesh get /cluster/nextid` nehmen, nicht raten.

## 11. Offene Punkte aus der Messung

- **`.de`-Quote ist aus einem Log hochgerechnet** (0,40 % im DigiCert-Log, 13.359 Cert /
  11 `.de`-Namen). Über mehrere Logs und 24 h kann die Quote anders liegen.
  **Phase 0 misst das** — bevor Ressourcen und LLM-Kosten festgeschrieben werden.
- **CT-Latenz** ist ungeklärt: Wie lange nach der Registrierung erscheint das erste
  Zertifikat? Bei Let's Encrypt mit Auto-SSL Minuten, bei manueller Einrichtung nie.
  Phase 0 erfasst `not_before` und kann die Verteilung zeigen.

## 12. Erkenntnisse aus dem E2E-Betrieb (2026-11-23)

Diese Punkte standen nicht im Plan und sind erst durch den Lauf gegen echte,
frisch registrierte Domains aufgefallen:

| Beobachtung | Ursache | Behebung |
|---|---|---|
| `tlz.de`, `harzkurier.de` als geparkt erkannt | Marker `undef` traf `class="… undefined"` in der Navigation | Marker nur noch gegen sichtbaren Text, `PARKING_MARKERS_TEXTONLY` |
| `luckysrv.de`, `supersrv.de` nicht als geparkt erkannt | domaindiscount24-Muster („Domainpark") unbekannt | Marker ergänzt, Testfall dokumentiert |
| `cittasana.de` („KI Automatisierung") → `mobility` | Keyword `auto` matchte in „Automatisierung" | Wort­grenzen via Regex, `auto` nur als ganzes Wort |
| `napawebagentur.de` → `other` | `webagentur` fehlte in der Keyword-Liste | `services` ergänzt |
| HTTP 401 als „geparkt" gemeldet | 401 ist eine Zugangssperre, kein Parking | 401/403 als Fehler klassifiziert, nicht als Parking |
| `xn--…`-Domains unleserlich | Punycode in der Anzeige | `display_name()` + Tests inkl. Roundtrip |
| API beim Import nicht testbar | `app = create_app()` beim Modulimport | `asgi.py` als eigener Einstieg, `create_app()` lazy |

**Zur IDN-Darstellung:** `xn--fassnachtskostme-wzb.de` sieht aus, als wäre ein
Suffix verloren — tatsächlich ist `-wzb` Teil der Punycode-Kodierung und
dekodiert korrekt zu `fassnachtskostüme.de`. Das ist durch Roundtrip-Tests
belegt, nicht geraten.

**Zum Harness:** Eine Variablenkollision (`N` als Farbe *und* als Zähler) führte
zu `✓3` statt `✓`. Das ist kein Kosmetikfehler, sondern der Hinweis, dass der
Harness selbst getestet werden muss — deshalb sind die Farben jetzt `C_G/C_R/C_N`
und der Zähler `NDOM`.

## 13. Was offen ist

| Punkt | Grund |
|---|---|
| **Echter Ollama-Lauf** | Auf dem Host läuft kein Ollama. Die Kaskade ist mit Attrappen und echten Provider-Antworten getestet, aber nicht gegen ein echtes Modell. `OLLAMA=1` beim Installer bringt es mit. |
| **OpenRouter-Key** | `~/.secrets/omnirouter-key` ist kein OpenRouter-Key (Beginn nicht `sk-or-`). Ohne Key bleibt Stufe 2 inaktiv. |
| **Installation auf Proxmox** | Der Installer ist syntaktisch geprüft und im Harness gemockt, aber noch nicht auf einem echten Host gelaufen. Der nächste Schritt wäre genau das. |
| **CT-Latenz** | Wie schnell erscheint ein Zertifikat nach der Registrierung? Die `spike.py`-Messung erfasst das, ist aber noch nicht über 24 h gelaufen. |
## 14. Einstellungsseite (nachgerüstet 2026-11-24)

Die Konfiguration lag zunächst nur in der `.env` — für einen Container mit
Weboberfläche zu umständlich. Jetzt gibt es eine Seite dafür.

**Rangfolge:** Datenbank (Oberfläche) → Umgebung (`.env`) → Default.
Einmal in der Oberfläche gespeicherte Werte überschreiben also die Datei. Das
ist der Punkt, an dem man sich sonst stundenlang sucht, warum eine `.env`-Änderung
nichts bewirkt.

**Warum die Oberfläche Vorrang hat:** die `.env` ist im Container versteckt und
braucht einen `pct exec` + Neustart. Die Oberfläche ist der bequeme Weg, also
darf sie auch gewinnen. Die Datei bleibt als Notausgang für alles, was man
vor der ersten Anmeldung braucht.

**14 Felder:**

| Gruppe | Felder |
|---|---|
| LLM | Kaskade, Eskalationsschwelle, Ollama-URL, Ollama-Modell, OpenRouter-URL, OpenRouter-Modell, API-Key |
| Erfassung | Max. Zertifikate/Lauf, Domains/Durchgang, HTTP-Timeout, robots.txt, Domains pro LLM-Aufruf |
| Auswertung | Zielkategorien, Domains pro Änderungslauf |

**Maskierung:** der API-Key wird als `sk**************t999` ausgeliefert. Ein
leer gelassenes Feld beim Speichern lässt den alten Wert stehen — sonst würde
jedes Speichern den Key löschen.

**Verbindungstest:** ein echter Minimal-Call, kein Port-Check. Ein offener Port
sagt nichts darüber, ob Modell und Endpunkt zusammenpassen. Vier Fälle:

| Antwort | Bedeutung |
|---|---|
| „Verbindung steht – Antwort: OK" | Endpunkt + Modell passen |
| „HTTP 404 (Modellname prüfen)" | Endpunkt richtig, Modell nicht vorhanden |
| „HTTP 401 (API-Key falsch …)" | Key falsch oder abgelaufen |
| „Nicht erreichbar (URLError …)" | Endpunkt läuft nicht |

**Zwei Fallen, die dabei auftauchten:**

1. `save()` meldete beim Speichern „13 Werte geändert", obwohl nur eines editiert
   war — die Oberfläche schickt alle Felder mit. Jetzt vergleicht `save()` mit
   dem alten Wert und meldet nur echte Änderungen.
2. `chain_from_settings()` übergab leere Strings an `build_chain()`, das mit
   `if env.get(...)` auf „nicht konfiguriert" prüfte. Folge: ein Provider wurde
   stillschweigend übersprungen, obwohl er konfiguriert war. Jetzt werden leere
   Werte durch die Defaults ersetzt.
