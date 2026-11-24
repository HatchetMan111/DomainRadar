"""DomainRadar – Klassifikation mit zweistufiger Provider-Kaskade.

    Text → Stufe 1 (Ollama, lokal, gratis)
             ├─ Konfidenz >= SCHWELLE  → fertig
             └─ Konfidenz <  SCHWELLE
                      → Stufe 2 (OpenRouter, kostenpflichtig)
                            ├─ Erfolg → Ergebnis + provider
                            └─ Fehler → Stufe-1-Ergebnis bleibt

Beide Stufen sprechen dasselbe Protokoll (OpenAI-kompatibel), Unterschied sind
nur `base_url`, `model` und API-Key. Damit ist Ollama, llama.cpp, vLLM oder eine
Cloud-API gleichermaßen anschließbar.

Ohne konfigurierten Endpoint läuft eine deterministische Keyword-Klassifikation.
Die App bricht also nie ab, nur die Qualität sinkt.
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from .config import DEFAULTS
from .db import Database

log = logging.getLogger("domainradar.classifier")

HTTP_TIMEOUT = 60

SYSTEM_PROMPT = """Du klassifizierst neu gestartete Webseiten für eine Marktbeobachtung.
Antworte AUSSCHLIESSLICH mit einem JSON-Objekt, ohne Erklärung, ohne Markdown.

Schema:
{
  "results": [
    {
      "domain": "die übergebene Domain",
      "brand": "Markenname",
      "category": "eine Kategorie aus der Liste",
      "segment": "b2b" oder "b2c",
      "business_type": "z.B. SaaS, Shop, Agentur, Verein, Blog",
      "sells_own_product": true oder false,
      "confidence": 0.0 bis 1.0,
      "summary_de": "ein Satz, was angeboten wird"
    }
  ]
}

Kategorien: software, energy, smart-home, climate, health, finance, education,
retail, food, mobility, media, crafts, services, other.

sells_own_product = true, wenn die Seite ein eigenes Produkt oder eine eigene
Dienstleistung verkauft. false bei reiner Landingpage, Blog, Geparktem oder
 reiner Werbung für Dritte.

confidence: wie sicher du bist. Bei leeren, mehrdeutigen oder technisch
fehlerhaften Seiten niedrig (unter 0.5) setzen."""

CATEGORIES = (
    "software",
    "energy",
    "smart-home",
    "climate",
    "health",
    "finance",
    "education",
    "retail",
    "food",
    "mobility",
    "media",
    "crafts",
    "services",
    "other",
)


def categories_from_settings(settings: dict[str, Any]) -> tuple[str, ...]:
    """Zielkategorien aus den Einstellungen (kommagetrennt), Fallback auf CATEGORIES."""
    raw = str(settings.get("target_categories", "") or "").strip()
    if not raw:
        return CATEGORIES
    cats = tuple(
        c.strip().lower().replace(" ", "-")
        for c in raw.split(",")
        if c.strip()
    )
    return cats or CATEGORIES


def system_prompt(categories: tuple[str, ...] = CATEGORIES) -> str:
    """System-Prompt mit der wirksamen Kategorieliste (Default: CATEGORIES)."""
    default_line = "Kategorien: " + ", ".join(CATEGORIES) + "."
    cat_line = "Kategorien: " + ", ".join(categories) + "."
    return SYSTEM_PROMPT.replace(default_line, cat_line)


@dataclass
class Provider:
    """Ein LLM-Endpunkt im OpenAI-kompatiblen Format."""

    name: str
    base_url: str
    model: str
    api_key: str = ""
    enabled: bool = True

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.model)

    def chat(self, system: str, user: str, timeout: int = HTTP_TIMEOUT) -> str:
        """Chat-Completion, gibt den Rohtext zurück."""
        url = self.base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.1,
        }
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read(500).decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass
            raise RuntimeError(
                f"{self.name}: HTTP {exc.code} {exc.reason} – {detail[:300]}"
            ) from exc
        except (urllib.error.URLError, OSError) as exc:
            raise RuntimeError(
                f"{self.name}: nicht erreichbar ({type(exc).__name__}: {exc})"
            ) from exc

        try:
            parsed = json.loads(body)
            return parsed["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError) as exc:
            raise RuntimeError(
                f"{self.name}: unerwartetes Antwortformat ({exc}) – {body[:300]}"
            ) from exc


def build_chain(env: dict[str, str] | None = None) -> list[Provider]:
    """Provider-Kette aus der `.env` aufbauen.

    `LLM_CHAIN=ollama,openrouter` (Default). Ein Eintrag ohne URL wird
    übersprungen, damit eine halb konfigurierte Umgebung nicht crasht.
    """
    env = env if env is not None else dict(os.environ)
    chain_name = env.get("LLM_CHAIN", "ollama,openrouter")

    ollama = Provider(
        name="ollama",
        base_url=env.get("OLLAMA_BASE_URL", "http://127.0.0.1:11434/v1"),
        model=env.get("OLLAMA_MODEL", "llama3.2:3b"),
    )
    openrouter = Provider(
        name="openrouter",
        base_url=env.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        model=env.get("OPENROUTER_MODEL", "deepseek/deepseek-chat"),
        api_key=env.get("OPENROUTER_API_KEY", ""),
    )

    by_name = {"ollama": ollama, "openrouter": openrouter}
    chain: list[Provider] = []
    for name in (n.strip() for n in chain_name.split(",")):
        if name not in by_name:
            log.warning("Unbekannter LLM-Provider '%s' in LLM_CHAIN – übersprungen", name)
            continue
        provider = by_name[name]
        if provider.name == "openrouter" and not provider.api_key:
            log.info("OpenRouter ohne API-Key – Stufe 2 ist inaktiv")
            continue
        if provider.configured:
            chain.append(provider)
    return chain


def chain_from_settings(settings: dict) -> list[Provider]:
    """Provider-Kette aus der wirksamen Konfiguration bauen.

    Gegenüber `build_chain` der Unterschied: die Werte kommen aus Datenbank
    und Umgebung, nicht nur aus der Umgebung. Damit greifen Änderungen aus
    der Einstellungsseite sofort.

    Leere Werte werden zu sinnvollen Defaults aufgefüllt. Ohne das würde
    `build_chain` einen leeren Modellnamen als "nicht konfiguriert" lesen
    und den Provider stillschweigend überspringen.
    """
    return build_chain(
        {
            "LLM_CHAIN": str(settings.get("llm_chain") or "ollama,openrouter"),
            "OLLAMA_BASE_URL": str(
                settings.get("ollama_base_url") or DEFAULTS["ollama_base_url"]
            ),
            "OLLAMA_MODEL": str(settings.get("ollama_model") or DEFAULTS["ollama_model"]),
            "OPENROUTER_BASE_URL": str(
                settings.get("openrouter_base_url") or DEFAULTS["openrouter_base_url"]
            ),
            "OPENROUTER_MODEL": str(
                settings.get("openrouter_model") or DEFAULTS["openrouter_model"]
            ),
            "OPENROUTER_API_KEY": str(settings.get("openrouter_api_key") or ""),
        }
    )


def parse_llm_response(raw: str) -> dict[str, Any]:
    """LLM-Antwort robust parsen – auch mit Markdown-Rahmen oder Vortext."""
    text = raw.strip()

    # ```json … ``` entfernen
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    if fence:
        text = fence.group(1).strip()

    # Erste geschweifte Klammer bis zur letzten
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]

    data = json.loads(text)
    if "results" in data and isinstance(data["results"], list):
        return data
    return {"results": [data]}


def normalize_result(
    item: dict[str, Any],
    domain: str,
    allowed: tuple[str, ...] = CATEGORIES,
) -> dict[str, Any]:
    """Ein LLM-Ergebnis auf das Schema zwingen, fehlende Felder füllen."""
    category = str(item.get("category", "")).strip().lower()
    if category not in allowed:
        category = "other"

    segment = str(item.get("segment", "")).strip().lower()
    if segment not in ("b2b", "b2c"):
        segment = "b2c"

    try:
        confidence = float(item.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    sells = item.get("sells_own_product")
    if isinstance(sells, str):
        sells = sells.strip().lower() in ("true", "ja", "yes", "1")
    elif not isinstance(sells, bool):
        sells = False

    return {
        "domain": str(item.get("domain") or domain),
        "category": category,
        "segment": segment,
        "business_type": str(item.get("business_type", "") or "")[:60],
        "sells_own_product": sells,
        "confidence": round(confidence, 3),
        "summary_de": str(item.get("summary_de", "") or "")[:400],
    }


# ---------- Deterministischer Fallback ----------

KEYWORD_CATEGORY = {
    "software": ("software", "saas", "plattform", "cloud", "programmierung", "entwicklung"),
    "energy": ("energie", "solar", "photovoltaik", "strom", "wärmepumpe", "heizung"),
    "smart-home": ("smart home", "smart-home", "home assistant", "homematic"),
    "climate": ("klima", "co2", "nachhaltig", "umwelt"),
    "health": ("gesundheit", "medizin", "arzt", "pflege", "therapie", "burnout"),
    "finance": ("finanz", "geld", "konto", "versicher", "kredit", "steuern"),
    "education": ("lernen", "kurs", "schule", "bildung", "nachhilfe", "prüfung"),
    "retail": ("shop", "versand", "warenkorb", "kollektion"),
    "food": ("essen", "rezept", "restaurant", "lebensmittel"),
    "mobility": ("auto", "fahrrad", "reisen", "reisebüro", "transport", "ladepunkt"),
    "media": ("nachrichten", "magazin", "podcast", "journal"),
    "crafts": ("handwerk", "werkstatt", "schreiner", "elektrik", "sanierung"),
    "services": (
        "beratung",
        "consulting",
        "agentur",
        "webagentur",
        "dienstleistung",
        "-seo",
        "marketing",
    ),
}


def _word_hits(keywords: tuple[str, ...], haystack: str) -> int:
    """Treffer zählen, aber nur als ganzes Wort.

    Ohne Wortgrenze würde "auto" in "Automatisierung" treffen und eine
    KI-Beratung zur Mobilitätsseite machen. Genau das ist im Testlauf
    passiert (cittasana.de → mobility).
    """
    hits = 0
    for kw in keywords:
        pattern = r"(?<![a-zäöüß0-9])" + re.escape(kw) + r"(?![a-zäöüß0-9])"
        if re.search(pattern, haystack, re.I):
            hits += 1
    return hits


def classify_deterministic(title: str, description: str, text_len: int) -> dict[str, Any]:
    """Regelbasierte Klassifikation, wenn kein LLM erreichbar ist."""
    haystack = f"{title} {description}".lower()

    category = "other"
    best = 0
    for cat, keywords in KEYWORD_CATEGORY.items():
        hits = _word_hits(keywords, haystack)
        if hits > best:
            best = hits
            category = cat

    # Heuristik: Impressum + Kontakt deutet auf ein echtes Angebot, nicht auf
    # eine reine Landingpage. Kein Beweis, aber besser als nichts.
    has_offer = best > 0 and text_len > 800

    return {
        "domain": "",
        "category": category,
        "segment": "b2b" if ("business" in haystack or "b2b" in haystack) else "b2c",
        "business_type": "",
        "sells_own_product": has_offer,
        "confidence": round(min(0.55, 0.2 + 0.1 * best), 3),
        "summary_de": "",
    }


# ---------- Klassifikation ----------

def _build_user_prompt(items: list[dict[str, str]]) -> str:
    lines = []
    for item in items:
        lines.append(
            f"DOMAIN: {item['domain']}\n"
            f"TITEL: {item['title']}\n"
            f"BESCHREIBUNG: {item['description']}\n"
            f"TEXT: {item['text'][:1200]}"
        )
    return "\n\n---\n\n".join(lines)


def classify_batch(
    items: list[dict[str, str]],
    chain: list[Provider],
    escalate_below: float = 0.75,
    categories: tuple[str, ...] = CATEGORIES,
) -> tuple[dict[str, dict], str, str]:
    """Ein Batch (max. 10) klassifizieren.

    Rückgabe: (Ergebnisse pro Domain, verwendeter Provider, Modell).
    Bei leerer Kette wird deterministisch klassifiziert.
    """
    if not items:
        return {}, "", ""

    if not chain:
        out = {}
        for item in items:
            res = classify_deterministic(
                item["title"], item["description"], len(item.get("text", ""))
            )
            res["domain"] = item["domain"]
            out[item["domain"]] = res
        return out, "deterministic", ""

    sys_prompt = system_prompt(categories)
    prompt = _build_user_prompt(items)
    first_provider = chain[0]
    used = first_provider.name
    model = first_provider.model
    stage1_ok = False

    try:
        raw = first_provider.chat(sys_prompt, prompt)
        parsed = parse_llm_response(raw)
        results = {
            str(r.get("domain")): normalize_result(r, str(r.get("domain", "")), categories)
            for r in parsed.get("results", [])
            if isinstance(r, dict)
        }
        stage1_ok = bool(results)
    except RuntimeError as exc:
        log.warning("Stufe 1 (%s) fehlgeschlagen: %s", first_provider.name, exc)
        results = {}
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("Stufe 1 lieferte kein gültiges JSON: %s", exc)
        results = {}

    if not stage1_ok:
        # Kein Stufe-1-Ergebnis: die Ausweisung muss das widerspiegeln, sonst
        # behauptet der Report "klassifiziert über ollama", obwohl der
        # deterministische Fallback geliefert hat.
        used = "deterministic"
        model = ""

    # Fehlende oder unsichere Ergebnisse → Stufe 2
    needs_escalation = [
        item
        for item in items
        if item["domain"] not in results
        or results[item["domain"]]["confidence"] < escalate_below
    ]

    if needs_escalation and len(chain) > 1:
        second = chain[1]
        log.info(
            "%d von %d Domains unter Schwelle – eskaliere an %s",
            len(needs_escalation),
            len(items),
            second.name,
        )
        try:
            raw = second.chat(sys_prompt, _build_user_prompt(needs_escalation))
            parsed = parse_llm_response(raw)
            for r in parsed.get("results", []):
                if not isinstance(r, dict):
                    continue
                domain = str(r.get("domain", ""))
                if domain:
                    normalized = normalize_result(r, domain, categories)
                    # Bessere Konfidenz gewinnt
                    old = results.get(domain)
                    if old is None or normalized["confidence"] >= old["confidence"]:
                        results[domain] = normalized
            used = second.name
            model = second.model
        except RuntimeError as exc:
            log.warning("Stufe 2 (%s) fehlgeschlagen: %s", second.name, exc)
        except (ValueError, json.JSONDecodeError) as exc:
            log.warning("Stufe 2 lieferte kein gültiges JSON: %s", exc)

    # Was übrig bleibt, deterministisch füllen
    for item in items:
        if item["domain"] not in results:
            res = classify_deterministic(
                item["title"], item["description"], len(item.get("text", ""))
            )
            res["domain"] = item["domain"]
            results[item["domain"]] = res

    return results, used, model


def run_once(
    db: Database,
    *,
    batch_size: int | None = None,
    chain: list[Provider] | None = None,
    verbose: bool = False,
) -> dict:
    """Alle offenen Domains klassifizieren.

    Ohne `chain` wird die wirksame Konfiguration aus Datenbank und Umgebung
    gelesen – damit greifen Änderungen aus der Einstellungsseite sofort.
    """
    from . import config

    settings = config.load(db)
    if batch_size is None:
        batch_size = int(settings.get("classify_batch", 10) or 10)
    escalate_below = float(settings.get("llm_escalate_below", 0.75) or 0.75)
    if chain is None:
        chain = chain_from_settings(settings)
    categories = categories_from_settings(settings)

    domains = db.domains_without_classification(limit=batch_size * 10)

    stats = {"attempted": 0, "classified": 0, "provider": "", "model": ""}
    for i in range(0, len(domains), batch_size):
        chunk = domains[i : i + batch_size]
        items = []
        for domain in chunk:
            row = db.get_domain(domain)
            if row is None:
                continue
            items.append(
                {
                    "domain": domain,
                    "title": row["title"] or "",
                    "description": row["meta_description"] or "",
                    "text": (row["meta_description"] or "") + " " + (row["title"] or ""),
                }
            )
        if not items:
            continue

        results, provider_name, model = classify_batch(
            items, chain, escalate_below=escalate_below, categories=categories
        )
        stats["provider"] = provider_name
        stats["model"] = model

        for domain, res in results.items():
            db.update_classification(
                domain,
                category=res["category"],
                segment=res["segment"],
                business_type=res["business_type"],
                sells_own_product=1 if res["sells_own_product"] else 0,
                confidence=res["confidence"],
                summary_de=res["summary_de"],
                classify_provider=provider_name,
                classify_model=model,
            )
            stats["classified"] += 1
            stats["attempted"] += 1
            if verbose:
                log.info(
                    "%s → %s/%s (%.2f, eigenes Produkt: %s)",
                    domain,
                    res["category"],
                    res["segment"],
                    res["confidence"],
                    res["sells_own_product"],
                )

    return stats


def classify_one(db: Database, domain: str) -> dict[str, Any] | None:
    """Eine einzelne Domain klassifizieren (für Direkt-Prüfung via API)."""
    row = db.get_domain(domain)
    if row is None:
        return None
    from . import config

    settings = config.load(db)
    categories = categories_from_settings(settings)
    chain = chain_from_settings(settings)
    escalate_below = float(settings.get("llm_escalate_below", 0.75) or 0.75)
    items = [
        {
            "domain": domain,
            "title": row["title"] or "",
            "description": row["meta_description"] or "",
            "text": (row["meta_description"] or "") + " " + (row["title"] or ""),
        }
    ]
    results, provider_name, model = classify_batch(
        items, chain, escalate_below=escalate_below, categories=categories
    )
    res = results.get(domain)
    if res is None:
        return None
    db.update_classification(
        domain,
        category=res["category"],
        segment=res["segment"],
        business_type=res["business_type"],
        sells_own_product=1 if res["sells_own_product"] else 0,
        confidence=res["confidence"],
        summary_de=res["summary_de"],
        classify_provider=provider_name,
        classify_model=model,
    )
    return res


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(
        prog="domainradar.classify",
        description="Domains über die Provider-Kaskade klassifizieren",
    )
    ap.add_argument(
        "--db",
        default=os.environ.get("DOMAINRADAR_DB", "/opt/domainradar/data/domainradar.db"),
    )
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    db = Database(args.db)
    try:
        stats = run_once(db, batch_size=args.batch_size, verbose=args.verbose)
    finally:
        db.close()
    print(
        f"{stats['classified']} klassifiziert über {stats['provider'] or '–'} "
        f"({stats['model'] or 'deterministisch'})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
