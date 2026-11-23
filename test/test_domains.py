"""Tests für den Domain-Filter – der wichtigste Baustein gegen CT-Müll."""

from __future__ import annotations

import pytest

from domainradar.domains import DomainFilter


@pytest.fixture
def f():
    return DomainFilter((".de",))


class TestNormalize:
    def test_plain_domain(self, f):
        assert f.normalize("example.de") == "example.de"

    def test_uppercase_and_whitespace(self, f):
        assert f.normalize("  Example.DE  ") == "example.de"

    def test_leading_dot(self, f):
        assert f.normalize("www.example.de") == "www.example.de"

    def test_wildcard_stripped(self, f):
        assert f.normalize("*.example.de") == "example.de"

    def test_reject_wrong_tld(self, f):
        assert f.normalize("example.com") is None

    def test_reject_tld_substring(self, f):
        # "example.de" als Endung muss nicht mitten im Namen treffen
        assert f.normalize("example.de.museum") is None

    def test_reject_embedded_tld(self, f):
        # Der Klassiker aus CT: Freitext, kein Domainname
        assert f.normalize("estado de mexico") is None

    def test_reject_address_like_string(self, f):
        assert f.normalize("19 rue des capucines") is None

    def test_reject_comma_separated(self, f):
        assert f.normalize("foo.de,bar.de") is None

    def test_reject_semicolon(self, f):
        assert f.normalize("foo.de;bar.de") is None

    def test_reject_non_ascii(self, f):
        assert f.normalize("bücher.de") is None

    def test_reject_empty(self, f):
        assert f.normalize("") is None
        assert f.normalize("   ") is None

    def test_reject_no_dot(self, f):
        assert f.normalize("localhost") is None

    def test_reject_single_label_with_tld_only(self, f):
        # ".de" allein ist kein Domainname
        assert f.normalize(".de") is None

    def test_reject_leading_hyphen_label(self, f):
        assert f.normalize("-foo.de") is None

    def test_reject_trailing_hyphen_label(self, f):
        assert f.normalize("foo-.de") is None

    def test_reject_too_long(self, f):
        long_label = "a" * 64
        assert f.normalize(f"{long_label}.de") is None

    def test_accept_punycode(self, f):
        assert f.normalize("xn--bcher-kva.de") == "xn--bcher-kva.de"

    def test_accept_digits(self, f):
        assert f.normalize("123.de") == "123.de"

    def test_accept_underscore_rejected(self, f):
        assert f.normalize("foo_bar.de") is None


class TestBaseDomain:
    def test_subdomain_reduced(self, f):
        assert f.base_domain("www.example.de") == "example.de"

    def test_deep_subdomain_reduced(self, f):
        assert f.base_domain("a.b.c.example.de") == "example.de"

    def test_already_base(self, f):
        assert f.base_domain("example.de") == "example.de"

    def test_accept_returns_base(self, f):
        assert f.accept("https://www.example.de/path") is None  # kein SAN-Format
        assert f.accept("www.example.de") == "example.de"


class TestAcceptMany:
    def test_dedupes_and_filters(self, f):
        raws = [
            "www.example.de",
            "example.de",
            "*.example.de",
            "other.com",
            "estado de mexico",
            "shop.example.de",
        ]
        assert f.accept_many(raws) == {"example.de"}

    def test_empty(self, f):
        assert f.accept_many([]) == set()

    def test_multiple_domains(self, f):
        raws = ["a.de", "b.de", "a.de"]
        assert f.accept_many(raws) == {"a.de", "b.de"}


class TestMultipleTlds:
    def test_de_only_by_default(self):
        f = DomainFilter((".de",))
        assert f.accept("a.de") == "a.de"
        assert f.accept("a.at") is None

    def test_custom_tlds(self):
        f = DomainFilter((".de", ".at"))
        assert f.accept("a.at") == "a.at"
        assert f.accept("a.de") == "a.de"
        assert f.accept("a.ch") is None

class TestDisplayName:
    """IDN-Darstellung für die Oberfläche.

    Hintergrund: `xn--fassnachtskostme-wzb.de` sieht aus, als wäre ein Suffix
    verloren, wenn man es dekodiert – tatsächlich ist `-wzb` Teil der
    Punycode-Kodierung und dekodiert sauber zu `fassnachtskostüme`.
    """

    def test_plain_domain_unchanged(self):
        from domainradar.domains import display_name

        assert display_name("example.de") == "example.de"

    def test_punycode_decoded(self):
        from domainradar.domains import display_name

        assert display_name("xn--bcher-kva.de") == "bücher.de"

    def test_punycode_with_suffix_is_correct(self):
        from domainradar.domains import display_name

        # Sieht nach Datenverlust aus, ist aber korrektes IDNA:
        # "-wzb" gehört zur Kodierung.
        assert display_name("xn--fassnachtskostme-wzb.de") == "fassnachtskostüme.de"

    def test_mixed_labels(self):
        from domainradar.domains import display_name

        out = display_name("xn--bcher-kva.example.de")
        assert out == "bücher.example.de"

    def test_broken_punycode_stays(self):
        from domainradar.domains import display_name

        # Defekter Punycode soll unverändert bleiben, nicht crashen
        assert display_name("xn--invalid-.de") == "xn--invalid-.de"

    def test_roundtrip(self):
        from domainradar.domains import display_name

        for name in ("bücher.de", "fassnachtskostüme.de", "müller-baut.de"):
            puny = name.encode("idna").decode("ascii")
            assert display_name(puny) == name
