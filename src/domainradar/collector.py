"""DomainRadar – Collector.

Folgt Certificate-Transparency-Logs inkrementell:

    get-sth  →  tree_size
      └─ wenn tree_size > gespeichert:  get-entries(start=gespeichert, end=tree_size-1)
            └─ SANs filtern, auf Basis-Domains reduzieren, einfügen
                  └─ tree_size NACH erfolgreichem Commit fortschreiben

Der High-Water-Mark wird erst nach dem Commit geschrieben. Bricht ein Lauf
mitten im Chunk ab, liest der nächste Lauf genau dort weiter – kein
Datenverlust, keine Dublette (Unique-Index auf `certs(log_id, cert_index)`).

Catch-up: Der Collector ist nicht auf "höchstens N Einträge" beschränkt. Wer
eine Woche weg war, holt die Woche nach. Das kostet Zeit, aber keine Daten.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field

from . import ctlog
from .db import Database, now_iso
from .domains import DomainFilter

log = logging.getLogger("domainradar.collector")

# Pro-Lauf-Zähler. Bewusst ein Modul-Dict statt Rückgabewert-Kette: so kann
# `collect_log` einzeln getestet werden und die Statistik trotzdem stimmen.
collector_stats = {"certs_read": 0, "certs_new": 0, "certs_with_sans": 0}


@dataclass
class CollectResult:
    """Ergebnis eines Sammellaufs – für Logs und Tests."""

    logs_seen: int = 0
    logs_failed: int = 0
    certs_read: int = 0
    certs_new: int = 0
    certs_with_sans: int = 0
    domains_new: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"{self.logs_seen} Logs · {self.certs_read:,} Cert gelesen "
            f"({self.certs_new:,} neu, {self.certs_with_sans:,} mit SAN) · "
            f"{self.domains_new:,} neue Domains"
        )


def _cert_not_before(cert) -> str | None:
    """not_before als ISO-String (UTC)."""
    from cryptography import x509

    try:
        nb = cert.not_valid_before_utc
    except AttributeError:  # cryptography < 42
        nb = cert.not_valid_before
    return nb.replace(microsecond=0).isoformat()


def _cert_not_after(cert) -> str | None:
    from cryptography import x509

    try:
        na = cert.not_valid_after_utc
    except AttributeError:  # cryptography < 42
        na = cert.not_valid_after
    return na.replace(microsecond=0).isoformat()


def _fingerprint(cert) -> str:
    """SHA256 über das DER – Dedupe über Logs hinweg."""
    import hashlib

    return hashlib.sha256(cert.public_bytes(__import__("cryptography").hazmat.primitives.serialization.Encoding.DER)).hexdigest()


def collect_log(
    db: Database,
    log_name: str,
    base_url: str,
    domain_filter: DomainFilter,
    *,
    max_entries: int | None = None,
    sleep_between_pages: float = 0.0,
) -> tuple[int, int]:
    """Einen einzelnen Log bis zum aktuellen Head lesen.

    Rückgabe: (gelesene Zertifikate, neue Domains).
    """
    client = ctlog.CTLog(log_name, base_url)
    log_id = db.upsert_log(log_name, base_url)
    stored_size, state = db.get_log_progress(log_name)

    if state == "stale":
        log.warning("%s ist als stale markiert – überspringe", log_name)
        return 0, 0

    sth = client.get_sth()
    tree_size = sth.tree_size

    if tree_size <= stored_size:
        log.debug("%s: keine neuen Einträge (%d)", log_name, tree_size)
        return 0, 0

    start = stored_size
    log.info(
        "%s: %d → %d (%d neu)",
        log_name,
        stored_size,
        tree_size,
        tree_size - stored_size,
    )

    read = 0
    with_sans = 0
    new_domains = 0
    result_certs_new = 0
    stamp = now_iso()

    try:
        for entry in client.iter_entries(start, tree_size):
            certs = list(ctlog.der_certificates(entry.get("extra_data", "")))
            if not certs:
                read += 1  # zählt als gelesener Eintrag, auch ohne Zertifikat
                if max_entries and read >= max_entries:
                    break
                continue

            cert_index = start + read
            read += 1

            names: list[str] = []
            not_before = None
            not_after = None
            for cert in certs:
                names.extend(ctlog.sans_from_cert(cert))
                if not_before is None:
                    not_before = _cert_not_before(cert)
                    not_after = _cert_not_after(cert)

            if names:
                with_sans += 1

            sans_json = json.dumps(names)
            inserted = db.record_certs(
                log_id,
                [(log_id, cert_index, not_before, not_after, sans_json, stamp)],
            )
            result_certs_new += inserted

            bases = domain_filter.accept_many(names)
            if bases:
                rows = [
                    {"domain": b, "cert_index": cert_index, "log_name": log_name}
                    for b in sorted(bases)
                ]
                new_domains += db.insert_domains(rows)

            if max_entries and read >= max_entries:
                log.info("%s: Limit von %d Einträgen erreicht", log_name, max_entries)
                break
            if sleep_between_pages and read % ctlog.PAGE_SIZE == 0:
                time.sleep(sleep_between_pages)

    except ctlog.CTLogError as exc:
        db.set_log_progress(log_name, stored_size + read, error=str(exc))
        raise

    # Commit des Fortschritts – bewusst nach dem Einfügen.
    db.set_log_progress(log_name, start + read)
    collector_stats["certs_read"] += read
    collector_stats["certs_new"] += result_certs_new
    collector_stats["certs_with_sans"] += with_sans
    return read, new_domains


def run_once(
    db: Database,
    *,
    only: str | None = None,
    max_entries: int | None = None,
    domain_filter: DomainFilter | None = None,
) -> CollectResult:
    """Ein vollständiger Sammellauf über alle bekannten Logs."""
    domain_filter = domain_filter or DomainFilter((".de",))
    result = CollectResult()
    collector_stats.update(certs_read=0, certs_new=0, certs_with_sans=0)

    logs = ctlog.discover_logs()
    if only:
        wanted = {w.strip() for w in only.split(",") if w.strip()}
        logs = [(n, u) for n, u in logs if n in wanted]
        if not logs:
            raise SystemExit(f"Kein Log gefunden für Filter: {only}")

    for name, url in logs:
        try:
            _, domains_new = collect_log(
                db, name, url, domain_filter, max_entries=max_entries
            )
            result.logs_seen += 1
            result.domains_new += domains_new
            log.info("%s: %d neue Domains", name, domains_new)
        except ctlog.CTLogError as exc:
            result.logs_failed += 1
            result.errors.append(f"{name}: {exc}")
            log.error("%s fehlgeschlagen: %s", name, exc)
        except Exception as exc:  # noqa: BLE001 – ein Log darf den Lauf nicht killen
            result.logs_failed += 1
            result.errors.append(f"{name}: {type(exc).__name__}: {exc}")
            log.exception("%s: unerwarteter Fehler", name)

    result.certs_read = collector_stats["certs_read"]
    result.certs_new = collector_stats["certs_new"]
    result.certs_with_sans = collector_stats["certs_with_sans"]
    return result


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="domainradar.collect",
        description="Certificate-Transparency-Logs lesen und neue .de-Domains sammeln",
    )
    ap.add_argument(
        "--db",
        default=os.environ.get("DOMAINRADAR_DB", "/opt/domainradar/data/domainradar.db"),
        help="Pfad zur SQLite-Datenbank",
    )
    ap.add_argument("--only", help="Nur bestimmte Logs (Komma-getrennt)")
    ap.add_argument(
        "--max-entries",
        type=int,
        default=int(os.environ.get("DOMAINRADAR_COLLECT_MAX", "0")) or None,
        help="Maximal so viele Zertifikate pro Log (für Tests)",
    )
    ap.add_argument("--verbose", "-v", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    db = Database(args.db)
    try:
        result = run_once(
            db,
            only=args.only,
            max_entries=args.max_entries,
        )
    finally:
        db.close()

    print(result.summary())
    for err in result.errors:
        print(f"FEHLER: {err}", file=sys.stderr)
    return 1 if result.logs_failed and result.logs_seen == 0 else 0


if __name__ == "__main__":
    sys.exit(main())