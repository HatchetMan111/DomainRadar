"""Basis-Domain-Erkennung und Filterung.

Zwei Aufgaben, die zusammengehören:

1. Aus einer beliebigen SAN (`www.foo.de`, `*.bar.de`, `xn--…`) die
   Basis-Domain bestimmen.
2. CT-Müll aussortieren. Certificate-Transparency-Logs enthalten in den
   `name_value`-Feldern mehr als nur Domains: Freitext, Adressen, Non-ASCII.
   Ein naiver `LIKE '%.de'`-Filter fängt dann Zeilen wie
   "Estado de Mexico" oder "19 rue des capucines" – gemessen ~25 %.

Deshalb ist der Filter hier absichtlich streng: nur ASCII, kein Whitespace,
kein Komma, gültige Labels, saubere Länge.
"""

from __future__ import annotations

import re
from functools import lru_cache

# Ein DNS-Label: beginnt und endet alphanumerisch, innen auch Bindestrich.
_LABEL = re.compile(r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$")

# Alles, was nach Whitespace, Komma, Semikolon oder Steuerzeichen riecht.
_FORBIDDEN = re.compile(r"[\s,;:<>\"'\\|()\[\]{}]")

# Non-ASCII (Umlaute, Akzente) – Domainnamen sind zu diesem Zeitpunkt punycode.
_NON_ASCII = re.compile(r"[^\x00-\x7f]")

MAX_DOMAIN_LEN = 253


class DomainFilter:
    """Normalisiert und filtert Kandidaten aus CT-Logs.

    Die Klasse ist absichtlich zustandslos – sie hält nur die konfigurierte
    Liste der erlaubten Endungen. So bleibt sie testbar und nebenläufig
    einsetzbar.
    """

    def __init__(self, allowed_tlds: tuple[str, ...] = (".de",)):
        self.allowed_tlds = tuple(t.lower() for t in allowed_tlds)

    def normalize(self, raw: str) -> str | None:
        """Roher SAN-Eintrag → normalisierte Domain oder None."""
        if not raw:
            return None

        name = raw.strip().lower()

        # Wildcards und IDN-Prefixe entfernen
        if name.startswith("*."):
            name = name[2:]
        if name.startswith("xn--"):
            pass  # bleibt, ist bereits ASCII

        if not name or len(name) > MAX_DOMAIN_LEN:
            return None
        if _FORBIDDEN.search(name) or _NON_ASCII.search(name):
            return None
        if not name.endswith(self.allowed_tlds):
            return None

        labels = name.split(".")
        if len(labels) < 2:
            return None
        if not all(_LABEL.match(label) for label in labels):
            return None

        return name

    def base_domain(self, name: str) -> str:
        """Basis-Domain bestimmen (registrierbarer Teil).

        Für `.de` ist das einfach die letzten zwei Labels. Die Logik ist
        bewusst so kurz – eine vollständige Public-Suffix-Liste wäre für
        `.de` unnötig, und Sonderfälle wie `co.uk` betreffen uns hier nicht.
        """
        parts = name.split(".")
        if len(parts) <= 2:
            return name
        return ".".join(parts[-2:])

    def accept(self, raw: str) -> str | None:
        """Komplettprüfung: roher SAN → Basis-Domain oder None."""
        name = self.normalize(raw)
        if name is None:
            return None
        return self.base_domain(name)

    def accept_many(self, raws: list[str]) -> set[str]:
        """Mehrere SANs filtern und auf Basis-Domains reduzieren."""
        out: set[str] = set()
        for raw in raws:
            base = self.accept(raw)
            if base:
                out.add(base)
        return out


def display_name(domain: str) -> str:
    """Punycode für die Anzeige in Unicode zurückverwandeln.

    `xn--fassnachtskostme-wzb.de` wird zu `faßnachtskostüme-wzb.de`.
    Schlägt die Dekodierung fehl, bleibt der Punycode stehen – lieber
    unverändert als kaputt.
    """
    if not domain or "xn--" not in domain:
        return domain
    parts = []
    for label in domain.split("."):
        if label.startswith("xn--"):
            try:
                parts.append(label.encode("ascii").decode("idna"))
                continue
            except (UnicodeDecodeError, UnicodeError):
                pass
        parts.append(label)
    return ".".join(parts)


@lru_cache(maxsize=1)
def default_filter() -> DomainFilter:
    return DomainFilter((".de",))