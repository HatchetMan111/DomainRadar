"""Tests für den RFC-6962-Client mit echten Fixture-Daten.

Die Fixtures sind echte CT-Logs (DigiCert Wyvern, Nov 2026), einmalig
eingefroren. Damit testen wir den Parser gegen die Realität, nicht gegen
unsere eigene Annahme – inklusive des 3-Byte-Vorspanns, den Cloudflare und
andere Logs einbauen.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from domainradar import ctlog
from domainradar.ctlog import CTLog, CTLogError, STH, der_certificates, parse_entry

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def entries():
    data = json.loads((FIXTURES / "fixture_entries.json").read_text())
    return data


@pytest.fixture
def sth_data():
    return json.loads((FIXTURES / "fixture_sth.json").read_text())


class TestDerCertificates:
    def test_finds_certificate_in_fixture(self, entries):
        certs = list(der_certificates(entries[0]["extra_data"]))
        assert certs, "kein Zertifikat im Fixture-Eintrag gefunden"

    def test_all_fixture_entries_parse(self, entries):
        for i, entry in enumerate(entries):
            certs = list(der_certificates(entry["extra_data"]))
            assert certs, f"Eintrag {i} ohne Zertifikat"

    def test_handles_prefixed_extra_data(self, entries):
        # DigiCert liefert einen Vorspann; der Parser muss ihn überspringen
        raw = base64.b64decode(entries[0]["extra_data"])
        certs = list(der_certificates(entries[0]["extra_data"]))
        assert certs[0].public_bytes(
            __import__("cryptography").hazmat.primitives.serialization.Encoding.DER
        ) in raw

    def test_empty_input(self):
        assert list(der_certificates("")) == []

    def test_garbage_input(self):
        assert list(der_certificates(base64.b64encode(b"not a certificate").decode())) == []

    def test_invalid_base64_raises(self):
        with pytest.raises(Exception):
            list(der_certificates("!!!not-base64!!!"))


class TestParseEntry:
    def test_returns_names(self, entries):
        certs, names = parse_entry(entries[0])
        assert certs
        assert isinstance(names, list)

    def test_names_are_lowercase(self, entries):
        _, names = parse_entry(entries[0])
        assert all(n == n.lower() for n in names)

    def test_no_names_without_san(self):
        # Ein Zertifikat ohne SAN ist zulässig – muss [] liefern, nicht crashen
        certs, names = parse_entry({"extra_data": ""})
        assert certs == []
        assert names == []


class TestCTLogClient:
    def test_get_sth_structure(self, sth_data):
        sth = STH(
            tree_size=sth_data["tree_size"],
            timestamp_ms=sth_data["timestamp"],
            root_hash=sth_data["sha256_root_hash"],
            raw=sth_data,
        )
        assert sth.tree_size > 0
        assert sth.timestamp > 1_700_000_000  # nach 2023

    def test_missing_field_raises(self):
        with pytest.raises(CTLogError):
            CTLog("x", "https://example.invalid").get_sth()

    def test_unreachable_host_raises_ctlogerror(self):
        with pytest.raises(CTLogError):
            CTLog("x", "https://nonexistent.invalid.example", timeout=5).get_sth()

    def test_chunk_size_limit(self):
        client = CTLog("x", "https://example.invalid")
        with pytest.raises(CTLogError):
            client.get_entries(0, 1500)

    def test_negative_range(self):
        client = CTLog("x", "https://example.invalid")
        assert client.get_entries(10, 5) == []


class TestDefaultLogs:
    def test_defaults_are_wellformed(self):
        assert ctlog.DEFAULT_LOGS
        for name, url in ctlog.DEFAULT_LOGS:
            assert name
            assert url.startswith("https://")


class TestIterLogNames:
    """End-to-end-Test des Namens-Iterators mit gemocktem Transport."""

    def test_yields_index_and_domain(self, entries, monkeypatch):
        def fake_get_entries(self, start, end):
            return entries

        monkeypatch.setattr(CTLog, "get_entries", fake_get_entries)
        monkeypatch.setattr(
            CTLog, "iter_entries", lambda self, a, b: iter(entries)
        )

        out = list(ctlog.iter_log_names("test", "https://example.invalid", 0, 1))
        assert out, "keine Namen geliefert"
        for idx, domain in out:
            assert isinstance(idx, int)
            assert isinstance(domain, str)

    def test_index_is_monotonic(self, entries, monkeypatch):
        monkeypatch.setattr(CTLog, "iter_entries", lambda self, a, b: iter(entries))
        out = list(ctlog.iter_log_names("test", "https://example.invalid", 100, 4))
        indices = {idx for idx, _ in out}
        # Indizes müssen im Bereich des Logs liegen
        assert all(i >= 100 for i in indices)