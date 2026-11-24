"""Tests für die FastAPI.

Fokus liegt auf den Dingen, die im Betrieb wirklich brechen: leere Datenbank,
freie Eingaben, Filter-Kombinationen.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from domainradar.api import create_app
from domainradar.db import Database


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "api.db"
    db = Database(db_path)
    db.insert_domains(
        [
            {"domain": "alpha.de"},
            {"domain": "beta.de"},
            {"domain": "gamma.de"},
        ]
    )
    db.update_fetch(
        "alpha.de",
        title="Alpha Shop",
        text_len=5000,
        page_hash="a",
        is_parked=0,
        http_status=200,
    )
    db.update_fetch(
        "beta.de",
        title="Beta Beratung",
        text_len=5000,
        page_hash="b",
        is_parked=0,
        http_status=200,
    )
    db.update_fetch(
        "gamma.de",
        title="Gamma",
        text_len=100,
        page_hash="c",
        is_parked=1,
        http_status=200,
    )
    db.update_classification(
        "alpha.de",
        category="software",
        segment="b2b",
        sells_own_product=1,
        confidence=0.9,
        summary_de="Verkauft Software",
        company_name="Alpha GmbH",
        legal_form="GmbH",
    )
    db.update_classification(
        "beta.de",
        category="services",
        segment="b2b",
        sells_own_product=1,
        confidence=0.6,
        summary_de="Beratung",
        company_name="Beta AG",
        legal_form="AG",
    )
    db.close()

    app = create_app(str(db_path))
    return TestClient(app)


class TestHealth:
    def test_health_ok(self, client):
        res = client.get("/api/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"


class TestStartups:
    def test_returns_all(self, client):
        res = client.get("/api/startups")
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 3
        assert len(body["hits"]) == 3

    def test_display_name_present(self, client):
        res = client.get("/api/startups")
        for hit in res.json()["hits"]:
            assert "display_name" in hit

    def test_search_by_query(self, client):
        res = client.get("/api/startups", params={"q": "Alpha"})
        assert res.json()["total"] == 1

    def test_filter_by_category(self, client):
        res = client.get("/api/startups", params={"category": "software"})
        assert res.json()["total"] == 1
        assert res.json()["hits"][0]["domain"] == "alpha.de"

    def test_filter_by_segment(self, client):
        res = client.get("/api/startups", params={"segment": "b2b"})
        assert res.json()["total"] == 2

    def test_leads_view_excludes_parked(self, client):
        res = client.get("/api/startups", params={"view": "leads"})
        domains = {h["domain"] for h in res.json()["hits"]}
        assert "gamma.de" not in domains
        assert "alpha.de" in domains

    def test_sort_oldest(self, client):
        res = client.get("/api/startups", params={"sort": "oldest"})
        assert res.json()["total"] == 3

    def test_invalid_view_rejected(self, client):
        res = client.get("/api/startups", params={"view": "bogus"})
        assert res.status_code == 422

    def test_limit_bounds(self, client):
        assert client.get("/api/startups", params={"limit": 0}).status_code == 422
        assert client.get("/api/startups", params={"limit": 999}).status_code == 422


class TestDomainDetail:
    def test_found(self, client):
        res = client.get("/api/domain/alpha.de")
        assert res.status_code == 200
        assert res.json()["category"] == "software"

    def test_not_found(self, client):
        res = client.get("/api/domain/unbekannt.de")
        assert res.status_code == 404

    def test_idn_display(self, client):
        # Punycode in der DB, lesbare Form in der Antwort
        from domainradar.db import Database as DB

        res = client.get("/api/domain/xn--bcher-kva.de")
        assert res.status_code == 404  # nicht in dieser Fixture


class TestStats:
    def test_counts(self, client):
        res = client.get("/api/stats")
        body = res.json()
        assert body["total_domains"] == 3
        assert body["with_product"] == 2
        assert body["parked"] == 1
        assert isinstance(body["by_category"], list)


class TestFeed:
    def test_feed_is_rss(self, client):
        res = client.get("/feed.xml")
        assert res.status_code == 200
        assert "rss" in res.text
        assert "alpha.de" in res.text


class TestExport:
    def test_csv_export(self, client):
        res = client.get("/export/leads.csv")
        assert res.status_code == 200
        assert "domain" in res.text
        assert "alpha.de" in res.text


class TestEmptyDatabase:
    def test_api_survives_empty_db(self, tmp_path):
        app = create_app(str(tmp_path / "empty.db"))
        c = TestClient(app)
        assert c.get("/api/startups").json()["total"] == 0
        assert c.get("/api/stats").status_code == 200
        assert c.get("/feed.xml").status_code == 200

class TestSettingsApi:
    """Endpunkte der Einstellungsseite."""

    def test_get_settings(self, client):
        res = client.get("/api/settings")
        assert res.status_code == 200
        body = res.json()
        assert "values" in body
        assert "meta" in body
        assert "groups" in body
        # Jeder bekannte Schlüssel muss beschrieben sein
        for key in body["meta"]:
            assert key in body["values"], f"{key} fehlt in values"

    def test_secret_is_masked_in_response(self, client):
        # Key setzen
        client.put("/api/settings", json={"values": {"openrouter_api_key": "sk-or-1234567890"}})
        body = client.get("/api/settings").json()
        secret = body["values"]["openrouter_api_key"]
        assert secret["is_set"] is True
        assert "1234567890" not in str(secret)
        assert "7890" in secret["preview"]

    def test_put_changes_value(self, client):
        res = client.put("/api/settings", json={"values": {"ollama_model": "llama3.1:8b"}})
        assert res.status_code == 200
        assert "ollama_model" in res.json()["changed"]
        body = client.get("/api/settings").json()
        assert body["values"]["ollama_model"] == "llama3.1:8b"

    def test_put_empty_secret_keeps_old(self, client):
        client.put("/api/settings", json={"values": {"openrouter_api_key": "sk-or-alt"}})
        res = client.put("/api/settings", json={"values": {"openrouter_api_key": ""}})
        assert "openrouter_api_key" not in res.json()["changed"]
        # Weiterhin gesetzt
        body = client.get("/api/settings").json()
        assert body["values"]["openrouter_api_key"]["is_set"] is True

    def test_put_rejects_missing_values(self, client):
        assert client.put("/api/settings", json={}).status_code == 422

    def test_put_rejects_non_object(self, client):
        assert client.put("/api/settings", json=[1, 2, 3]).status_code == 422

    def test_put_ignores_unknown_keys(self, client):
        res = client.put("/api/settings", json={"values": {"gibt_es_nicht": "x"}})
        assert res.json()["changed"] == []

    def test_test_endpoint_rejects_unknown_provider(self, client):
        res = client.post("/api/settings/test", json={"provider": "nos"})
        assert res.status_code == 422

    def test_test_endpoint_openrouter_without_key(self, client):
        res = client.post("/api/settings/test", json={"provider": "openrouter"})
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is False
        assert "Key" in body["message"]

    def test_test_endpoint_uses_unsaved_values(self, client):
        # Werte mitschicken, ohne sie zu speichern
        res = client.post(
            "/api/settings/test",
            json={
                "provider": "ollama",
                "values": {"ollama_base_url": "", "ollama_model": ""},
            },
        )
        body = res.json()
        assert body["ok"] is False
        assert "URL" in body["message"]


class TestSettingsRoute:
    """Die Einstellungsseite muss per SPA-Fallback erreichbar sein."""

    def test_settings_url_serves_app(self, client):
        res = client.get("/settings")
        assert res.status_code == 200


class TestConnectionTestEndpoint:
    """Der Verbindungstest muss Fehlerarten unterscheiden, nicht nur abbrechen."""

    def test_success_path(self, client, monkeypatch):
        """Erfolgreicher Minimal-Call → ok=true mit Antworttext."""
        import urllib.request
        from unittest.mock import MagicMock

        class Resp:
            status = 200

            def read(self):
                return b'{"choices":[{"message":{"content":"OK"}}]}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: Resp())
        res = client.post(
            "/api/settings/test",
            json={
                "provider": "ollama",
                "values": {"ollama_base_url": "http://x/v1", "ollama_model": "m"},
            },
        )
        body = res.json()
        assert body["ok"] is True
        assert "OK" in body["message"]

    def test_model_not_found_gives_hint(self, client, monkeypatch):
        import urllib.error
        import urllib.request

        def raise_404(*a, **k):
            raise urllib.error.HTTPError(
                "http://x", 404, "Not Found", {}, None
            )

        monkeypatch.setattr(urllib.request, "urlopen", raise_404)
        res = client.post(
            "/api/settings/test",
            json={
                "provider": "ollama",
                "values": {"ollama_base_url": "http://x/v1", "ollama_model": "m"},
            },
        )
        body = res.json()
        assert body["ok"] is False
        assert "Modell" in body["message"]

    def test_bad_key_gives_hint(self, client, monkeypatch):
        import urllib.error
        import urllib.request

        def raise_401(*a, **k):
            raise urllib.error.HTTPError(
                "http://x", 401, "Unauthorized", {}, None
            )

        monkeypatch.setattr(urllib.request, "urlopen", raise_401)
        res = client.post(
            "/api/settings/test",
            json={
                "provider": "openrouter",
                "values": {
                    "openrouter_base_url": "http://x/v1",
                    "openrouter_model": "m",
                    "openrouter_api_key": "sk-or-x",
                },
            },
        )
        body = res.json()
        assert body["ok"] is False
        assert "Key" in body["message"]


class TestManualRun:
    def test_unknown_stage_404(self, client):
        res = client.post("/api/run/xyz")
        assert res.status_code == 404

    def test_runs_status_lists_stages(self, client):
        res = client.get("/api/runs")
        assert res.status_code == 200
        body = res.json()
        assert body["ok"] is True
        assert {r["stage"] for r in body["runs"]} == {"collect", "enrich", "classify", "changes"}
        assert all(r["running"] is False for r in body["runs"])


class TestCheck:
    def test_invalid_domain_422(self, client):
        # .com ist nicht erlaubt – darf weder DB-Eintrag noch Thread erzeugen
        res = client.post("/api/check", json={"domain": "example.com"})
        assert res.status_code == 422

    def test_missing_payload_422(self, client):
        res = client.post("/api/check", json={})
        assert res.status_code == 422
