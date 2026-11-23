"""Tests für den Collector.

Das Netz wird durch eine Attrappe ersetzt. Geprüft wird das, was wirklich
schiefgehen kann: High-Water-Mark, Catch-up nach Pause, partielle Abbrüche,
fehlerhafte Logs.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from domainradar import ctlog
from domainradar.collector import collect_log, run_once
from domainradar.ctlog import CTLog, CTLogError
from domainradar.db import Database
from domainradar.domains import DomainFilter

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "test.db")
    yield d
    d.close()


@pytest.fixture
def entries():
    return json.loads((FIXTURES / "fixture_entries.json").read_text())


class FakeLog(CTLog):
    """CTLog-Attrappe, die einen festen Satz Einträge über mehrere Calls verteilt."""

    def __init__(self, name, base_url, entries, fail_after=None):
        super().__init__(name, base_url)
        self.entries = entries
        self.fail_after = fail_after
        self.served = 0

    def get_sth(self):
        return ctlog.STH(
            tree_size=len(self.entries),
            timestamp_ms=1_800_000_000_000,
            root_hash="fake",
        )

    def iter_entries(self, from_index, to_size):
        for i in range(from_index, min(to_size + 1, len(self.entries))):
            # Vor jedem Yield prüfen: so bricht der Lauf mitten im Chunk ab
            if self.fail_after is not None and self.served >= self.fail_after:
                raise CTLogError("simulierter Abbruch")
            self.served += 1
            yield self.entries[i]


class TestCollectLog:
    def test_first_run_stores_everything(self, db, entries, monkeypatch):
        monkeypatch.setattr(
            ctlog, "CTLog", lambda n, u: FakeLog(n, u, entries)
        )
        read, new_domains = collect_log(
            db, "fake", "https://fake.invalid", DomainFilter((".de",))
        )
        assert read == len(entries)
        assert db.get_log_progress("fake")[0] == len(entries)

    def test_second_run_is_noop(self, db, entries, monkeypatch):
        monkeypatch.setattr(ctlog, "CTLog", lambda n, u: FakeLog(n, u, entries))
        collect_log(db, "fake", "https://fake.invalid", DomainFilter((".de",)))
        # Zweiter Lauf, gleiche Einträge: nichts Neues
        read, _ = collect_log(db, "fake", "https://fake.invalid", DomainFilter((".de",)))
        assert read == 0

    def test_new_entries_are_appended(self, db, entries, monkeypatch):
        monkeypatch.setattr(ctlog, "CTLog", lambda n, u: FakeLog(n, u, entries))
        collect_log(db, "fake", "https://fake.invalid", DomainFilter((".de",)))

        # Log ist gewachsen: alter Stand + 2 neue Einträge
        grown = entries + entries[:2]
        monkeypatch.setattr(ctlog, "CTLog", lambda n, u: FakeLog(n, u, grown))
        read, _ = collect_log(db, "fake", "https://fake.invalid", DomainFilter((".de",)))
        assert read == 2
        assert db.get_log_progress("fake")[0] == len(grown)

    def test_failure_marks_error_and_keeps_progress(self, db, entries, monkeypatch):
        monkeypatch.setattr(
            ctlog,
            "CTLog",
            lambda n, u: FakeLog(n, u, entries, fail_after=2),
        )
        with pytest.raises(CTLogError):
            collect_log(db, "fake", "https://fake.invalid", DomainFilter((".de",)))

        # Fortschritt darf nicht auf 0 zurückspringen
        tree_size, state = db.get_log_progress("fake")
        assert state == "error"
        assert tree_size < len(entries)

    def test_resume_after_failure(self, db, entries, monkeypatch):
        # Erst Abbruch nach 2 Einträgen
        monkeypatch.setattr(
            ctlog, "CTLog", lambda n, u: FakeLog(n, u, entries, fail_after=2)
        )
        with pytest.raises(CTLogError):
            collect_log(db, "fake", "https://fake.invalid", DomainFilter((".de",)))

        partial = db.get_log_progress("fake")[0]
        assert partial > 0

        # Dann Erfolg ohne Abbruch
        monkeypatch.setattr(ctlog, "CTLog", lambda n, u: FakeLog(n, u, entries))
        read, _ = collect_log(db, "fake", "https://fake.invalid", DomainFilter((".de",)))
        assert partial + read == len(entries)
        assert db.get_log_progress("fake")[0] == len(entries)


class TestDomainDedupe:
    def test_same_domain_from_two_logs_counts_once(self, db, entries, monkeypatch):
        monkeypatch.setattr(ctlog, "CTLog", lambda n, u: FakeLog(n, u, entries))

        _, first = collect_log(db, "logA", "https://a.invalid", DomainFilter((".de",)))
        # Gleiche Einträge, anderer Log: neue Zertifikate, aber gleiche Domains
        _, second = collect_log(db, "logB", "https://b.invalid", DomainFilter((".de",)))

        assert second == 0, "Domains dürfen nicht doppelt gezählt werden"

    def test_subdomain_and_base_count_as_one(self, db, entries, monkeypatch):
        # Die Fixtures enthalten Subdomains – der Filter muss sie zusammenführen
        monkeypatch.setattr(ctlog, "CTLog", lambda n, u: FakeLog(n, u, entries))
        _, new_domains = collect_log(
            db, "fake", "https://fake.invalid", DomainFilter((".de",))
        )
        rows = db.conn.execute("SELECT domain FROM domains").fetchall()
        bases = [r["domain"] for r in rows]
        assert len(bases) == len(set(bases)), "Dubletten in domains"
        assert all(b.count(".") == 1 for b in bases), f"keine Basis-Domain: {bases}"


class TestRunOnce:
    def test_continues_after_single_log_failure(self, db, monkeypatch):
        def fake_discover():
            return [("bad", "https://bad.invalid"), ("good", "https://good.invalid")]

        def fake_ctlog(name, url, *a, **kw):
            if name == "bad":
                raise CTLogError("boom")

            class Good(FakeLog):
                def __init__(self, n, u):
                    super().__init__(n, u, [])

                def get_sth(self):
                    return ctlog.STH(0, 1_800_000_000_000, "x")

            return Good(name, url)

        monkeypatch.setattr(ctlog, "discover_logs", fake_discover)
        monkeypatch.setattr(ctlog, "CTLog", fake_ctlog)

        result = run_once(db)
        assert result.logs_seen == 1
        assert result.logs_failed == 1
        assert result.errors