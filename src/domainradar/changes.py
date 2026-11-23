"""DomainRadar – wöchentlicher Änderungsreport.

Für jede bekannte Domain die Seite erneut lesen und den Text-Hash vergleichen.
Ändert er sich, wird eine Änderung protokolliert und – wenn Schlagwörter
auftauchen – die Art der Änderung grob eingeordnet.

Bewusst einfach: kein semantischer Diff, sondern Hash-Vergleich plus
Schlagwortsuche. Das reicht, um "hier ist etwas passiert" zu erkennen, und
kostet keine LLM-Calls.
"""

from __future__ import annotations

import logging
import re

from .db import Database
from .enricher import fetch_domain

log = logging.getLogger("domainradar.changes")

# Schlagwörter → Änderungsart. Reihenfolge = Priorität.
SIGNALS: list[tuple[str, re.Pattern[str]]] = [
    ("funding", re.compile(r"(funding|investition|finanzierungsrunde|series [a-e]\b|raised)", re.I)),
    ("launch", re.compile(r"(launch|neu seit|jetzt verfügbar|neu:)", re.I)),
    ("pricing", re.compile(r"(ab \d+ ?(€|eur)|preis|pricing|monat|mtl\.)", re.I)),
    ("hiring", re.compile(r"(wir suchen|stellenangebot|jobs|karriere|hiring)", re.I)),
    ("customer", re.compile(r"(kunde|referenz|case study|kundenstimmen)", re.I)),
]


def classify_change(text: str) -> str | None:
    """Schlagwortsuche im neuen Seitentext."""
    for kind, pattern in SIGNALS:
        if pattern.search(text):
            return kind
    return "page"


def run_once(db: Database, *, limit: int = 300, verbose: bool = False) -> dict:
    """Alle Domains mit Hash einmal neu lesen und vergleichen."""
    rows = db.conn.execute(
        """SELECT domain, page_hash FROM domains
            WHERE page_hash IS NOT NULL AND is_parked = 0
            ORDER BY last_change_at IS NOT NULL, first_seen
            LIMIT ?""",
        (limit,),
    ).fetchall()

    stats = {"checked": 0, "changed": 0, "failed": 0, "kinds": {}}

    for row in rows:
        domain = row["domain"]
        stats["checked"] += 1
        res = fetch_domain(domain)
        if not res.ok:
            stats["failed"] += 1
            continue
        if res.page_hash == row["page_hash"]:
            continue

        kind = classify_change(res.title + " " + res.meta_description)
        db.add_change(domain, kind, f"page_hash {row['page_hash'][:8]} → {res.page_hash[:8]}")
        db.update_fetch(
            domain,
            page_hash=res.page_hash,
            title=res.title,
            meta_description=res.meta_description,
            text_len=res.text_len,
            http_status=res.http_status,
            final_url=res.final_url,
        )
        stats["changed"] += 1
        stats["kinds"][kind] = stats["kinds"].get(kind, 0) + 1
        if verbose:
            log.info("%s: %s", domain, kind)

    return stats

def main(argv: list[str] | None = None) -> int:
    import argparse
    import os

    ap = argparse.ArgumentParser(
        prog="domainradar.changes",
        description="Wochenreport: Seiten erneut lesen und Änderungen protokollieren",
    )
    ap.add_argument(
        "--db",
        default=os.environ.get("DOMAINRADAR_DB", "/opt/domainradar/data/domainradar.db"),
    )
    ap.add_argument("--limit", type=int, default=300)
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
        f"{stats['checked']} geprüft · {stats['changed']} geändert · "
        f"{stats['failed']} fehlerhaft · Arten: {stats['kinds']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
