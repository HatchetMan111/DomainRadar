#!/usr/bin/env python3
"""Phase 0 – Stichprobe über Certificate-Transparency-Logs.

Misst, wie viele Unique-Namen und `.de`-Basis-Domains pro Zertifikat anfallen,
und verteilt die Stichprobe über mehrere weit auseinanderliegende Zeitpunkte.

Warum verteilt? CT-Logs sind zeitlich sortiert. Ein einzelner Block kann
ausschliesslich Let's-Encrypt-Testzertifikate enthalten (Quote 0,00 %) oder
echte Registrierungen (Quote 1,07 %). Nur eine verteilte Stichprobe liefert
eine belastbare Zahl.

Aufruf:
    python3 -m domainradar.spike --certs 2000 --blocks 5

Ausgabe ist bewusst knapp: eine Tabelle, keine JSON-Hoch whom.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request

from . import ctlog
from .ctlog import CTLog, STH, der_certificates, sans_from_cert
from .domains import DomainFilter

USER_AGENT = "DomainRadar/0.1 (+phase0 spike)"


def _get_json(url: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def sample_log(
    name: str,
    base_url: str,
    blocks: int,
    per_block: int,
    domain_filter: DomainFilter,
) -> dict:
    """Mehrere Blöcke über den Log verteilt lesen."""
    client = CTLog(name, base_url, timeout=45)
    sth = client.get_sth()
    end = sth.tree_size - 1
    if end <= 0:
        return {"log": name, "error": "Log ist leer"}

    certs = 0
    names: set[str] = set()
    de_bases: set[str] = set()
    certs_with_sans = 0
    errors: list[str] = []

    for i in range(blocks):
        # Gleichmäßig über den Log verteilen, nicht am Anfang sammeln
        frac = (i + 1) / (blocks + 1)
        block_end = int(end * frac)
        block_start = max(0, block_end - per_block + 1)
        try:
            entries = client.get_entries(block_start, block_end)
        except ctlog.CTLogError as exc:
            errors.append(f"Block {i}: {exc}")
            continue

        for entry in entries:
            certs += 1
            cert_names: list[str] = []
            for cert in der_certificates(entry.get("extra_data", "")):
                cert_names.extend(sans_from_cert(cert))
            if cert_names:
                certs_with_sans += 1
                names.update(cert_names)
                de_bases.update(domain_filter.accept_many(cert_names))
        time.sleep(0.2)

    de_names = [n for n in names if n.endswith(domain_filter.allowed_tlds[0])]
    return {
        "log": name,
        "certs": certs,
        "certs_with_sans": certs_with_sans,
        "unique_names": len(names),
        "de_names": len(de_names),
        "de_bases": len(de_bases),
        "pct_de": round(100 * len(de_names) / max(1, len(names)), 2),
        "sample_bases": sorted(de_bases)[:10],
        "errors": errors,
        "tree_size": sth.tree_size,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="domainradar.spike",
        description="Phase 0: verteilte Stichprobe über CT-Logs, misst .de-Quote",
    )
    ap.add_argument("--certs", type=int, default=400, help="Zertifikate pro Block")
    ap.add_argument("--blocks", type=int, default=5, help="Anzahl Blöcke pro Log")
    ap.add_argument(
        "--logs",
        default="digicert_wyvern2026h2,sectigo_elephant2026h2",
        help="Komma-getrennte Log-Namen (Kurzform erlaubt)",
    )
    ap.add_argument("--all", action="store_true", help="Alle bekannten Logs messen")
    args = ap.parse_args(argv)

    known = dict(ctlog.DEFAULT_LOGS)
    if args.all:
        selected = ctlog.discover_logs()[:4]
    else:
        wanted = [w.strip() for w in args.logs.split(",") if w.strip()]
        selected = []
        for w in wanted:
            match = next(
                ((n, u) for n, u in known.items() if n.startswith(w)), None
            )
            if match is None:
                print(f"Unbekanntes Log: {w}", file=sys.stderr)
                continue
            selected.append(match)
    if not selected:
        print("Keine Logs ausgewählt.", file=sys.stderr)
        return 1

    domain_filter = DomainFilter((".de",))
    print(
        f"Phase-0-Stichprobe: {args.blocks} Blöcke à {args.certs} Cert "
        f"pro Log, {len(selected)} Logs\n"
    )

    rows = []
    for name, url in selected:
        print(f"  messe {name} …", flush=True)
        res = sample_log(name, url, args.blocks, args.certs, domain_filter)
        rows.append(res)
        if "error" in res:
            print(f"    FEHLER: {res['error']}")
            continue
        print(
            f"    {res['certs']:,} Cert · {res['unique_names']:,} Namen · "
            f"{res['de_names']} .de ({res['pct_de']} %)"
        )

    print("\nErgebnis")
    print("-" * 62)
    total_certs = sum(r.get("certs", 0) for r in rows)
    total_names = sum(r.get("unique_names", 0) for r in rows)
    total_de = sum(r.get("de_names", 0) for r in rows)
    total_bases = sum(r.get("de_bases", 0) for r in rows)

    for r in rows:
        if "error" in r:
            continue
        print(
            f"{r['log']:<28} {r['certs']:>7,} Cert  {r['unique_names']:>7,} Namen  "
            f"{r['de_names']:>4} .de  {r['pct_de']:>5} %  {r['de_bases']:>4} Basen"
        )
    print("-" * 62)
    pct = round(100 * total_de / max(1, total_names), 2)
    print(
        f"{'SUMME':<28} {total_certs:>7,} Cert  {total_names:>7,} Namen  "
        f"{total_de:>4} .de  {pct:>5} %  {total_bases:>4} Basen"
    )
    print()
    if pct:
        # Hochrechnung bewusst vorsichtig: pro Log ~2,1 Mio. Cert/Tag, und
        # Subdomains fallen zu einer Basis zusammen. Die Zahl ist eine
        # Obergrenze, keine Prognose.
        per_log = 2_100_000 * (pct / 100)
        print(
            f"→ Obergrenze: ~{per_log:,.0f} neue .de-Namen/Tag/Log "
            f"(vor Subdomain-Zusammenfassung)"
        )
        print(
            f"  realistisch als Basis-Domains: ~{per_log * len(rows) / 3:,.0f}/Tag "
            f"über {len(rows)} Logs (grobe Hausnummer)"
        )
    print("Beispielfund: siehe 'sample_bases' in der JSON-Ausgabe (--json)")

    if "--json" in (argv or sys.argv[1:]):
        print(json.dumps(rows, indent=2, ensure_ascii=False))

    return 0


if __name__ == "__main__":
    sys.exit(main())