"""DomainRadar – Webseiten lesen und einordnen.

Was hier passiert, ist bewusst schmal gehalten:

1. `GET http→https://<domain>`, Redirects verfolgen, max. 2 MB
2. Titel, Meta-Description, Sprache, Textmenge, Links auf Impressum
3. **Parking-Erkennung** – bevor ein LLM Geld kostet
4. Ergebnis in `domains` schreiben

Die Parking-Erkennung ist der wichtigste Kostenhebel: ein großer Teil frisch
registrierter Domains ist geparkt oder leer. Ohne diesen Filter würde die
Klassifikation für leere Seiten bezahlen.
"""

from __future__ import annotations

import logging
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .db import Database

log = logging.getLogger("domainradar.enricher")

USER_AGENT = (
    "Mozilla/5.0 (compatible; DomainRadar/0.1; +local research bot) "
    "Python-urllib/3"
)
MAX_BYTES = 2_000_000
TIMEOUT = 10

# Marker für geparkte Domains: Domain-Händler, Baukästen, Halterungen.
PARKING_MARKERS = (
    "domain is for sale",
    "domain zum verkauf",
    "domain zu verkaufen",
    "diese domain steht zum verkauf",
    "buy this domain",
    "powered by sedo",
    "powered by afternic",
    "domain parking",
    "parkingcrew",
    "bodis",
    "dan.com",
    "this domain is registered",
    "diese domain ist registriert",
    "default web site page",
    "web server's default page",
    "apache2 debian default page",
    "nginx welcome page",
    "coming soon",
    "demnächst verfügbar",
    "in construction",
    "im aufbau",
    # domaindiscount24 / "Domainpark": im Testbetrieb zweimal aufgetaucht
    "domainpark",
    "bitte den rasen nicht betreten",
    "diese domain wurde geparkt",
    "keinem server oder webhosting zugewiesen",
)

# Zweite Klasse: nur gegen den sichtbaren Text prüfen. "undef" steckt in
# CSS-Klassen (`class="… undefined"`) und würde sonst echte Seiten wie tlz.de
# fälschlich als geparkt markieren.
PARKING_MARKERS_TEXTONLY = (
    "undef",
    "diese domain kaufen",
    "domain registrieren",
    "jetzt sichern",
)

# Impressum/AGB-Verlinkungen – verraten, dass jemand ein Impressum betreibt.
IMPRESSUM_HINTS = re.compile(
    r"(impressum|anbieterkennzeichnung|about\s*us|über\s*uns|kontakt)", re.I
)
PRICING_HINTS = re.compile(r"(preis|pricing|tarif|abo|subscription)", re.I)
LOGIN_HINTS = re.compile(r"(login|anmelden|sign\s*in|konto|account)", re.I)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
_DESC_RE = re.compile(
    r'<meta[^>]+name=["\']description["\'][^>]+content=["\'](.*?)["\']', re.I | re.S
)
_DESC_RE_ALT = re.compile(
    r'<meta[^>]+content=["\'](.*?)["\'][^>]+name=["\']description["\']', re.I | re.S
)
_LANG_RE = re.compile(r'<html[^>]+lang=["\']([a-zA-Z-]+)["\']', re.I)
_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.I | re.S)


@dataclass
class FetchResult:
    domain: str
    ok: bool
    final_url: str = ""
    http_status: int | None = None
    title: str = ""
    meta_description: str = ""
    lang: str = ""
    text_len: int = 0
    page_hash: str = ""
    is_parked: bool = False
    parking_reason: str = ""
    links_impressum: bool = False
    hints: dict[str, bool] = None  # type: ignore[assignment]
    error: str = ""

    def __post_init__(self):
        if self.hints is None:
            self.hints = {}


def _strip_tags(html: str) -> str:
    text = re.sub(r"<script.*?</script>", " ", html, flags=re.I | re.S)
    text = re.sub(r"<style.*?</style>", " ", text, flags=re.I | re.S)
    text = _TAG_RE.sub(" ", text)
    text = _unescape(text)
    return _WS_RE.sub(" ", text).strip()


def _unescape(text: str) -> str:
    import html as html_mod

    return html_mod.unescape(text)


def _first(patterns, html: str) -> str:
    for pat in patterns:
        m = pat.search(html)
        if m:
            return _WS_RE.sub(" ", _strip_tags(m.group(1))).strip()
    return ""


def detect_parking(html_lower: str, text: str, text_len: int) -> str:
    """Leerstring, wenn nicht geparkt – sonst der auslösende Marker.

    `html_lower` ist das rohe HTML (für HTML-Kommentare und Server-Default-
    Seiten), `text` der sichtbare Text (für Marker, die in CSS-Klassen
    als False Positive auftauchen).
    """
    if text_len < 200:
        return "leer" if text_len < 50 else "sehr wenig Text"

    text_lower = text.lower()

    for marker in PARKING_MARKERS:
        if marker in html_lower:
            return marker
    for marker in PARKING_MARKERS_TEXTONLY:
        if marker in text_lower:
            return marker
    return ""


def _fetch_once(url: str) -> tuple[int, str, str]:
    """HTTP holen. Rückgabe: (status, final_url, body)."""
    ctx = ssl.create_default_context()
    # Manche frische Domains haben kein valides Zertifikat – das ist kein Grund,
    # die Seite zu überspringen, solange wir nichts ausgeben müssen.
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "de,en;q=0.5",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ctx) as resp:
            raw = resp.read(MAX_BYTES)
            charset = resp.headers.get_content_charset() or "utf-8"
            body = raw.decode(charset, errors="replace")
            return resp.status, resp.geturl(), body
    except urllib.error.HTTPError as exc:
        # 4xx/5xx sind eine gültige Antwort: Domain existiert, Seite nicht.
        try:
            body = exc.read(MAX_BYTES).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            body = ""
        return exc.code, url, body
    except (urllib.error.URLError, socket.timeout, OSError) as exc:
        raise RuntimeError(f"{type(exc).__name__}: {exc}") from exc


def fetch_domain(domain: str, *, try_https: bool = True) -> FetchResult:
    """Eine Domain lesen und auswerten."""
    result = FetchResult(domain=domain, ok=False)

    scheme = "https" if try_https else "http"
    url = f"{scheme}://{domain}/"
    try:
        status, final_url, body = _fetch_once(url)
    except RuntimeError as exc:
        # Einmal http versuchen, falls https scheitert (oder umgekehrt)
        if try_https:
            return fetch_domain(domain, try_https=False)
        result.error = str(exc)
        return result

    if status in (401, 403):
        # Zugangssperre, kein Parking: die Seite existiert, wir dürfen nur nicht.
        result.error = f"HTTP {status} (Zugang verweigert)"
        result.http_status = status
        result.final_url = final_url
        return result

    if status >= 500:
        result.error = f"HTTP {status}"
        result.http_status = status
        return result

    html_lower = body.lower()
    text = _strip_tags(body)
    text_len = len(text)
    title = _first((_TITLE_RE,), body)

    parking = detect_parking(html_lower, text, text_len)
    if parking:
        result.is_parked = True
        result.parking_reason = parking
        result.http_status = status
        result.final_url = final_url
        result.ok = True
        return result

    import hashlib

    result.ok = True
    result.http_status = status
    result.final_url = final_url
    result.title = title[:300]
    result.meta_description = _first((_DESC_RE, _DESC_RE_ALT), body)[:500]
    lang_match = _LANG_RE.search(body)
    result.lang = (lang_match.group(1)[:20] if lang_match else "").lower()
    result.text_len = text_len
    result.page_hash = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    result.links_impressum = bool(IMPRESSUM_HINTS.search(html_lower))
    result.hints = {
        "impressum": result.links_impressum,
        "pricing": bool(PRICING_HINTS.search(html_lower)),
        "login": bool(LOGIN_HINTS.search(html_lower)),
    }
    return result


def enrich_one(db: Database, domain: str) -> FetchResult:
    """Eine Domain holen und in die DB schreiben."""
    result = fetch_domain(domain)
    if result.ok:
        db.update_fetch(
            domain,
            is_parked=1 if result.is_parked else 0,
            http_status=result.http_status,
            final_url=result.final_url,
            title=result.title,
            meta_description=result.meta_description,
            lang=result.lang,
            page_hash=result.page_hash,
            text_len=result.text_len,
            fetch_error=None,
        )
    else:
        db.update_fetch(domain, fetch_error=result.error, http_status=result.http_status)
    return result


def run_once(db: Database, *, limit: int = 100, verbose: bool = False) -> dict:
    """Alle offenen Domains bis zum Limit lesen."""
    domains = db.domains_without_fetch(limit=limit)
    stats = {"attempted": 0, "ok": 0, "parked": 0, "failed": 0}
    for domain in domains:
        stats["attempted"] += 1
        res = enrich_one(db, domain)
        if res.ok and res.is_parked:
            stats["parked"] += 1
        elif res.ok:
            stats["ok"] += 1
        else:
            stats["failed"] += 1
        if verbose:
            log.info(
                "%s: %s%s",
                domain,
                "geparkt" if res.is_parked else ("ok" if res.ok else "FEHLER"),
                f" ({res.parking_reason})" if res.is_parked else "",
            )
    return stats

def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(
        prog="domainradar.enrich",
        description="Offene Domains lesen und Attribute in die Datenbank schreiben",
    )
    ap.add_argument(
        "--db",
        default=os.environ.get("DOMAINRADAR_DB", "/opt/domainradar/data/domainradar.db"),
    )
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    db = Database(args.db)
    try:
        stats = run_once(db, limit=args.limit, verbose=args.verbose)
    finally:
        db.close()
    print(
        f"{stats['attempted']} versucht · {stats['ok']} ok · "
        f"{stats['parked']} geparkt · {stats['failed']} fehlerhaft"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
