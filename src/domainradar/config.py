"""DomainRadar – Konfiguration aus Datenbank und Umgebung.

Zwei Quellen, eine Reihenfolge:

    1. Was in der Oberfläche gespeichert wurde  (`settings`-Tabelle)  ← gewinnt
    2. Was in der `.env` steht                       (Umgebung)
    3. Der eingebaute Default

Das ist Absicht: die Oberfläche ist der bequeme Weg, die `.env` der Notausgang.
Einmal in der Oberfläche gespeicherte Werte überstimmen also die Datei – sonst
würde eine alte `.env` jede Änderung im UI ignorieren.

Wichtig für den Betrieb: die Zeitpläne sind `oneshot`-Einheiten, starten also für
jeden Lauf einen neuen Prozess. Änderungen greifen damit beim nächsten Lauf, ohne
Neustart der API. Nur die API selbst liest pro Anfrage neu.
"""

from __future__ import annotations

import logging
import os
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger("domainradar.config")

# ---------------------------------------------------------------- Defaults

DEFAULTS: dict[str, Any] = {
    # LLM-Kaskade
    "llm_chain": "ollama,openrouter",
    "llm_escalate_below": 0.75,
    "ollama_base_url": "http://127.0.0.1:11434/v1",
    "ollama_model": "llama3.2:3b",
    "openrouter_base_url": "https://openrouter.ai/api/v1",
    "openrouter_model": "deepseek/deepseek-chat",
    "openrouter_api_key": "",
    # Erfassung
    "collect_max_entries": 0,       # 0 = unbegrenzt (nur für Tests sinnvoll)
    "enrich_batch": 100,
    "enrich_timeout": 10,
    "respect_robots": 1,
    "classify_batch": 10,
    # Darstellung / Auswertung
    "target_categories": "software,energy,smart-home,climate",
    "changes_limit": 300,
}

# Werte, die niemand zurückgeschrieben bekommt. Der API-Key wird maskiert
# ausgegeben, damit er nicht in Browser-Caches oder Screenshots landet.
SECRET_KEYS = frozenset({"openrouter_api_key"})

# Felder mit anderem Datentyp als String
INT_KEYS = frozenset(
    {"collect_max_entries", "enrich_batch", "enrich_timeout", "classify_batch",
     "changes_limit"}
)
FLOAT_KEYS = frozenset({"llm_escalate_below"})
BOOL_KEYS = frozenset({"respect_robots"})

# Beschreibungen für die Oberfläche. Ein Feld ohne Eintrag wird nicht angezeigt.
FIELD_META: dict[str, dict[str, str]] = {
    "llm_chain": {
        "label": "Provider-Kaskade",
        "hint": "Reihenfolge, Komma-getrennt. 'ollama' allein = rein lokal, keine Cloud.",
        "group": "LLM",
    },
    "llm_escalate_below": {
        "label": "Eskalationsschwelle",
        "hint": "Unter dieser Konfidenz wird an den nächsten Provider eskaliert (0–1).",
        "group": "LLM",
    },
    "ollama_base_url": {
        "label": "Ollama-URL",
        "hint": "OpenAI-kompatibler Endpunkt, meist http://127.0.0.1:11434/v1",
        "group": "LLM",
    },
    "ollama_model": {
        "label": "Ollama-Modell",
        "hint": "Modellname, z. B. llama3.2:3b oder llama3.1:8b",
        "group": "LLM",
    },
    "openrouter_base_url": {
        "label": "OpenRouter-URL",
        "hint": "https://openrouter.ai/api/v1",
        "group": "LLM",
    },
    "openrouter_model": {
        "label": "OpenRouter-Modell",
        "hint": "Modell für Stufe 2, z. B. deepseek/deepseek-chat",
        "group": "LLM",
    },
    "openrouter_api_key": {
        "label": "OpenRouter-API-Key",
        "hint": "Beginnt mit sk-or-. Wird maskiert angezeigt und nie zurückgegeben.",
        "group": "LLM",
    },
    "collect_max_entries": {
        "label": "Max. Zertifikate pro Lauf",
        "hint": "0 = unbegrenzt. Nur für Tests sinnvoll begrenzen.",
        "group": "Erfassung",
    },
    "enrich_batch": {
        "label": "Domains pro Durchgang",
        "hint": "Wie viele neue Domains pro Lauf abgerufen werden.",
        "group": "Erfassung",
    },
    "enrich_timeout": {
        "label": "HTTP-Timeout (Sekunden)",
        "hint": "Gilt pro Abruf einer Webseite.",
        "group": "Erfassung",
    },
    "respect_robots": {
        "label": "robots.txt beachten",
        "hint": "1 = ja. Aus bei 0 – nur wenn die Ziele das ausdrücklich erlauben.",
        "group": "Erfassung",
    },
    "classify_batch": {
        "label": "Domains pro LLM-Aufruf",
        "hint": "Größere Bündel sparen Calls, erhöhen aber die Fehlerquote.",
        "group": "Erfassung",
    },
    "target_categories": {
        "label": "Zielkategorien für Leads",
        "hint": "Komma-getrennt. Erhöht den Lead-Score in diesen Kategorien.",
        "group": "Auswertung",
    },
    "changes_limit": {
        "label": "Domains pro Änderungslauf",
        "hint": "Obergrenze für den Wochenreport.",
        "group": "Auswertung",
    },
}

GROUP_ORDER = ["LLM", "Erfassung", "Auswertung"]

# ---------------------------------------------------------------- Lesen

_TRUE = ("1", "true", "ja", "yes", "on")
_FALSE = ("0", "false", "nein", "no", "off")


def _coerce(key: str, raw: Any) -> Any:
    """Rohwert in den deklarierten Typ zwingen, mit Fallback auf den Default."""
    default = DEFAULTS.get(key)
    if isinstance(raw, bool):
        return raw
    if key in BOOL_KEYS:
        if isinstance(raw, str):
            low = raw.strip().lower()
            if low in _TRUE:
                return 1
            if low in _FALSE:
                return 0
        try:
            return 1 if int(raw) else 0
        except (TypeError, ValueError):
            return default
    if key in INT_KEYS:
        try:
            return int(float(raw))
        except (TypeError, ValueError):
            return default
    if key in FLOAT_KEYS:
        try:
            return max(0.0, min(1.0, float(raw)))
        except (TypeError, ValueError):
            return default
    return "" if raw is None else str(raw)


# Zuordnung Umgebung → Schlüssel. Bewusst wenige, nur was auch in der .env
# dokumentiert ist.
ENV_MAP = {
    "LLM_CHAIN": "llm_chain",
    "OLLAMA_BASE_URL": "ollama_base_url",
    "OLLAMA_MODEL": "ollama_model",
    "OPENROUTER_BASE_URL": "openrouter_base_url",
    "OPENROUTER_MODEL": "openrouter_model",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "TARGET_CATEGORIES": "target_categories",
    "DOMAINRADAR_ENRICH_BATCH": "enrich_batch",
    "DOMAINRADAR_CLASSIFY_BATCH": "classify_batch",
    "DOMAINRADAR_ENRICH_TIMEOUT": "enrich_timeout",
    "DOMAINRADAR_RESPECT_ROBOTS": "respect_robots",
    "DOMAINRADAR_COLLECT_MAX": "collect_max_entries",
    "DOMAINRADAR_CHANGES_LIMIT": "changes_limit",
}


def load(db=None, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Wirksame Konfiguration liefern.

    `db` darf None sein – dann gibt es nur Umgebung und Defaults. Das ist für
    Tests und für die Zeit vor der ersten Schema-Erzeugung nötig.
    """
    env = env if env is not None else dict(os.environ)
    result: dict[str, Any] = dict(DEFAULTS)

    # 1. Umgebung
    for env_name, key in ENV_MAP.items():
        if env.get(env_name):
            result[key] = _coerce(key, env[env_name])

    # 2. Datenbank überschreibt Umgebung
    if db is not None:
        try:
            rows = db.conn.execute("SELECT key, value FROM settings").fetchall()
        except Exception as exc:  # noqa: BLE001 – Tabelle kann vor v1 fehlen
            log.debug("settings nicht lesbar: %s", exc)
            rows = []
        for row in rows:
            key = row["key"]
            if key in DEFAULTS:
                result[key] = _coerce(key, row["value"])

    return result


def public_view(settings: dict[str, Any]) -> dict[str, Any]:
    """Konfiguration für die Oberfläche – Geheimnisse maskiert.

    Der Key wird nie im Klartext ausgeliefert. Stattdessen sieht die Oberfläche,
    ob einer gesetzt ist und wie die letzten vier Zeichen lauten. Zum Ändern
    muss der vollständige Wert neu eingegeben werden.
    """
    out: dict[str, Any] = {}
    for key, value in settings.items():
        if key in SECRET_KEYS:
            secret = str(value or "")
            out[key] = {
                "is_set": bool(secret),
                "preview": _mask(secret),
                "length": len(secret),
            }
        else:
            out[key] = value
    return out


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:2] + "*" * (len(secret) - 6) + secret[-4:]


# ---------------------------------------------------------------- Schreiben

def save(db, values: dict[str, Any]) -> list[str]:
    """Werte speichern. Rückgabe: Liste der *tatsächlich veränderten* Schlüssel.

    Unbekannte Schlüssel werden stillschweigend ignoriert, damit eine ältere
    Oberfläche eine neuere Installation nicht beschädigt. Leere Strings bei
    Feldern mit Default werden verworfen – sonst löscht ein leeres Formular
    versehentlich eine URL.

    Der Vergleich mit dem alten Wert ist Absicht: die Oberfläche schickt beim
    Speichern alle Felder mit. Ohne Abgleich würde sie "13 Werte geändert"
    melden, obwohl nur eines editiert wurde.
    """
    changed: list[str] = []
    with db.transaction() as con:
        for key, raw in values.items():
            if key not in DEFAULTS:
                continue
            # Bei Geheimnissen: leer lassen = unverändert lassen
            if key in SECRET_KEYS and (raw is None or str(raw) == ""):
                continue
            if raw is None:
                continue
            text = str(raw)
            if key not in SECRET_KEYS and text.strip() == "" and DEFAULTS[key] not in ("", 0, 0.0, 1):
                continue
            old = con.execute(
                "SELECT value FROM settings WHERE key=?", (key,)
            ).fetchone()
            old_value = old["value"] if old is not None else None
            if old_value is not None and str(old_value) == text:
                continue  # unverändert, nicht melden
            con.execute(
                """INSERT INTO settings(key, value, updated_at) VALUES(?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                     value=excluded.value, updated_at=excluded.updated_at""",
                (key, text, _now()),
            )
            changed.append(key)
    return changed


def _now() -> str:
    from .db import now_iso

    return now_iso()


# ---------------------------------------------------------------- Test

def test_connection(provider: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Einen Provider mit einem Minimal-Call prüfen.

    Bewusst ein echter Aufruf statt eines URL-Checks: ein erreichbarer Port
    sagt nichts darüber, ob Modell und Endpunkt zusammenpassen.
    """
    if provider == "ollama":
        base = str(settings.get("ollama_base_url", ""))
        model = str(settings.get("ollama_model", ""))
        key = ""
    elif provider == "openrouter":
        base = str(settings.get("openrouter_base_url", ""))
        model = str(settings.get("openrouter_model", ""))
        key = str(settings.get("openrouter_api_key", ""))
        if not key:
            return {
                "ok": False,
                "provider": provider,
                "message": "Kein API-Key gesetzt. Bitte oben eintragen.",
            }
    else:
        return {
            "ok": False,
            "provider": provider,
            "message": f"Unbekannter Provider: {provider}",
        }

    if not base:
        return {"ok": False, "provider": provider, "message": "Keine URL gesetzt."}
    if not model:
        return {"ok": False, "provider": provider, "message": "Kein Modell gesetzt."}

    import json

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": "Antworte nur mit dem Wort OK."},
            {"role": "user", "content": "Test"},
        ],
        "temperature": 0,
        "max_tokens": 5,
    }
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"

    req = urllib.request.Request(
        base.rstrip("/") + "/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(body)
        answer = parsed["choices"][0]["message"]["content"].strip()[:80]
        return {
            "ok": True,
            "provider": provider,
            "message": f"Verbindung steht – Antwort: {answer}",
        }
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        hint = ""
        if exc.code == 401:
            hint = " (API-Key falsch oder abgelaufen)"
        elif exc.code == 404:
            hint = " (Modellname prüfen)"
        elif exc.code == 429:
            hint = " (Rate-Limit oder Guthaben leer)"
        return {
            "ok": False,
            "provider": provider,
            "message": f"HTTP {exc.code} {exc.reason}{hint} – {detail[:200]}",
        }
    except (urllib.error.URLError, OSError) as exc:
        return {
            "ok": False,
            "provider": provider,
            "message": f"Nicht erreichbar ({type(exc).__name__}: {exc})",
        }
    except (ValueError, KeyError, IndexError) as exc:
        return {
            "ok": False,
            "provider": provider,
            "message": f"Unerwartete Antwort ({exc})",
        }