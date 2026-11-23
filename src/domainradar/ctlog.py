"""DomainRadar – RFC-6962-Client für Certificate-Transparency-Logs.

Liest Certificate-Transparency-Logs direkt aus, ohne crt.sh und ohne API-Key.
Protokoll: RFC 6962 (`get-sth`, `get-entries`).

Der Client ist bewusst netzwerkarm gehalten: eine STH-Abfrage, dann seitenweise
`get-entries` in 1000er-Blöcken. Alles andere (Filtern, Dedupe, Persistenz)
macht `collector.py`.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Iterable, Iterator

log = logging.getLogger(__name__)

USER_AGENT = "DomainRadar/0.1 (+local; RFC6962 client)"
DEFAULT_TIMEOUT = 25
PAGE_SIZE = 1000


class CTLogError(RuntimeError):
    """Ein CT-Log hat nicht spezifikationskonform geantwortet."""


@dataclass
class STH:
    """Signed Tree Head eines CT-Logs."""

    tree_size: int
    timestamp_ms: int
    root_hash: str
    raw: dict = field(default_factory=dict)

    @property
    def timestamp(self) -> float:
        """Zeitstempel in Sekunden (UTC)."""
        return self.timestamp_ms / 1000.0


def _http_get_json(url: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raise CTLogError(f"HTTP {resp.status} für {url}")
            body = resp.read()
    except urllib.error.HTTPError as exc:
        raise CTLogError(f"HTTP {exc.code} für {url}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise CTLogError(f"Netzwerkfehler für {url}: {exc.reason}") from exc
    try:
        return json.loads(body)
    except ValueError as exc:
        raise CTLogError(f"Ungültiges JSON von {url}: {exc}") from exc


class CTLog:
    """Ein einzelnes Certificate-Transparency-Log."""

    def __init__(self, name: str, base_url: str, timeout: int = DEFAULT_TIMEOUT):
        self.name = name
        self.base_url = base_url if base_url.endswith("/") else base_url + "/"
        self.timeout = timeout

    def _url(self, method: str) -> str:
        return f"{self.base_url}ct/v1/{method}"

    def get_sth(self) -> STH:
        """Aktuellen Signed Tree Head holen."""
        data = _http_get_json(self._url("get-sth"), self.timeout)
        try:
            return STH(
                tree_size=int(data["tree_size"]),
                timestamp_ms=int(data["timestamp"]),
                root_hash=str(data.get("sha256_root_hash", "")),
                raw=data,
            )
        except KeyError as exc:
            raise CTLogError(
                f"{self.name}: STH ohne Feld {exc} – Antwort: {str(data)[:200]}"
            ) from exc

    def get_entries(self, start: int, end: int) -> list[dict]:
        """Einträge im Bereich [start, end] inclusive holen (max. 1000)."""
        if end < start:
            return []
        if end - start + 1 > PAGE_SIZE:
            raise CTLogError(
                f"Chunk zu groß: {end - start + 1} Einträge (max {PAGE_SIZE})"
            )
        data = _http_get_json(
            f"{self._url('get-entries')}?start={start}&end={end}", self.timeout
        )
        return data.get("entries", [])

    def iter_entries(self, from_index: int, to_size: int) -> Iterator[dict]:
        """Alle Einträge von `from_index` bis `to_size` (exklusiv) seitenweise."""
        idx = from_index
        last = to_size - 1
        while idx <= last:
            end = min(idx + PAGE_SIZE - 1, last)
            entries = self.get_entries(idx, end)
            if not entries:
                log.warning(
                    "%s: leerer Chunk %d-%d – breche ab", self.name, idx, end
                )
                return
            yield from entries
            idx = end + 1


def der_certificates(extra_data_b64: str) -> Iterator[bytes]:
    """Rohe DER-Zertifikate aus dem `extra_data`-Feld eines CT-Eintrags.

    RFC 6962 spezifiziert `extra_data` als Folge von
    `opaque type<3> || uint24 length || bytes` – bei `precert_entry` zusätzlich
    Issuer-Key-Hash und TBSCertificate. In der Praxis liefern Logs (u. a.
    Cloudflare) einen Vorspann von 3 Byte, andere ab Offset 0.

    Deshalb scannen wir nach der DER-Sequenz (`0x30`) und lassen den
    `cryptography`-Parser entscheiden, was ein gültiges Zertifikat ist. Das ist
    robuster als ein fixer Offset und kostet bei ~1 KB pro Eintrag nichts.
    """
    from cryptography import x509

    raw = base64.b64decode(extra_data_b64)
    i = 0
    n = len(raw)
    while i < n - 4:
        if raw[i] != 0x30:
            i += 1
            continue
        first_len = raw[i + 1]
        if first_len & 0x80:
            k = first_len & 0x7F
            if k > 2 or i + 2 + k > n:
                i += 1
                continue
            length = int.from_bytes(raw[i + 2 : i + 2 + k], "big")
            header = 2 + k
        else:
            length = first_len
            header = 2
        if i + header + length <= n:
            try:
                yield x509.load_der_x509_certificate(raw[i : i + header + length])
                i += header + length
                continue
            except Exception:  # noqa: BLE001 – kein Zertifikat an dieser Stelle
                pass
        i += 1


def sans_from_cert(cert) -> list[str]:
    """Alle DNS-Namen aus den Subject-Alternative-Names eines Zertifikats."""
    from cryptography import x509

    try:
        ext = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    return [n.lower() for n in ext.value.get_values_for_type(x509.DNSName)]


def parse_entry(entry: dict) -> tuple[list[bytes], list[str]]:
    """Ein `get-entries`-Eintrag → (DER-Zertifikate, SAN-Namen)."""
    certs = list(der_certificates(entry.get("extra_data", "")))
    names: list[str] = []
    for cert in certs:
        names.extend(sans_from_cert(cert))
    return certs, names


def iter_log_names(
    name: str, base_url: str, from_index: int, to_size: int, timeout: int = DEFAULT_TIMEOUT
) -> Iterator[tuple[int, str]]:
    """Liest einen Log-Bereich und liefert (cert_index, domain) für jede SAN.

    Der Index ist der Eintrag im Log, nicht die Position im Chunk – damit kann
    der Collector exakt dort weiterlesen, wo er aufgehört hat.
    """
    log_client = CTLog(name, base_url, timeout)
    for offset, entry in enumerate(log_client.iter_entries(from_index, to_size)):
        _, names = parse_entry(entry)
        for domain in names:
            yield from_index + offset, domain


DEFAULT_LOGS: list[tuple[str, str]] = [
    # (Name, Basis-URL) – Stand 2026-11, Stand der Messung siehe PLAN.md
    ("cloudflare_nimbus2026", "https://ct.cloudflare.com/logs/nimbus2026"),
    ("digicert_wyvern2026h2", "https://wyvern.ct.digicert.com/2026h2"),
    ("sectigo_elephant2026h2", "https://elephant2026h2.ct.sectigo.com"),
    ("sectigo_tiger2026h2", "https://tiger2026h2.ct.sectigo.com"),
    ("google_argon2026h2", "https://ct.googleapis.com/logs/us1/argon2026h2"),
    ("letsencrypt_oak2026h2", "https://oak.ct.letsencrypt.org/2026h2"),
]


def discover_logs() -> list[tuple[str, str]]:
    """Aktive CT-Logs aus der offiziellen Google-Liste laden.

    Fallback auf die eingebaute Liste, wenn das erreichbar ist oder nicht.
    """
    try:
        data = _http_get_json(
            "https://www.gstatic.com/ct/log_list/v3/log_list.json", 20
        )
        found: list[tuple[str, str]] = []
        for operator in data.get("operators", []):
            for entry in operator.get("logs", []):
                if "readonly" in entry.get("state", {}):
                    continue
                url = entry.get("url", "")
                if url:
                    found.append((entry["name"], url.rstrip("/")))
        if found:
            return found
    except (CTLogError, ValueError) as exc:
        log.warning("Log-Liste nicht erreichbar (%s) – nutze eingebaute Liste", exc)
    return DEFAULT_LOGS