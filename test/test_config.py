"""Tests für die Konfiguration.

Drei Dinge müssen stimmen:
1. Die Rangfolge Umgebung → Datenbank → Default.
2. Geheimnisse verlassen nie im Klartext die API.
3. Ein leeres Feld löscht keinen bestehenden Wert.
"""

from __future__ import annotations

import pytest

from domainradar import config
from domainradar.db import Database


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "settings.db")
    yield d
    d.close()


class TestLoad:
    def test_defaults_without_anything(self):
        s = config.load(None, env={})
        assert s["llm_chain"] == "ollama,openrouter"
        assert s["ollama_model"] == "llama3.2:3b"
        assert s["llm_escalate_below"] == 0.75

    def test_env_overrides_default(self):
        s = config.load(None, env={"OLLAMA_MODEL": "llama3.1:8b"})
        assert s["ollama_model"] == "llama3.1:8b"

    def test_db_overrides_env(self, db):
        config.save(db, {"ollama_model": "aus-db"})
        s = config.load(db, env={"OLLAMA_MODEL": "aus-env"})
        assert s["ollama_model"] == "aus-db", "Datenbank muss die Umgebung schlagen"

    def test_env_still_used_when_db_empty(self, db):
        s = config.load(db, env={"OLLAMA_MODEL": "aus-env"})
        assert s["ollama_model"] == "aus-env"

    def test_types_are_coerced(self, db):
        config.save(db, {"enrich_batch": "250", "llm_escalate_below": "0.9"})
        s = config.load(db, env={})
        assert s["enrich_batch"] == 250
        assert isinstance(s["enrich_batch"], int)
        assert s["llm_escalate_below"] == 0.9

    def test_bool_coercion(self, db):
        for raw, expected in (("1", 1), ("ja", 1), ("true", 1), ("0", 0), ("nein", 0)):
            config.save(db, {"respect_robots": raw})
            assert config.load(db, env={})["respect_robots"] == expected

    def test_float_is_clamped(self, db):
        config.save(db, {"llm_escalate_below": "5.0"})
        assert config.load(db, env={})["llm_escalate_below"] == 1.0

    def test_invalid_int_falls_back(self, db):
        config.save(db, {"enrich_batch": "keine-zahl"})
        assert config.load(db, env={})["enrich_batch"] == config.DEFAULTS["enrich_batch"]

    def test_unknown_db_key_ignored(self, db):
        config.save(db, {"gibt_es_nicht": "x"})
        assert "gibt_es_nicht" not in config.load(db, env={})


class TestSave:
    def test_returns_changed_keys(self, db):
        changed = config.save(db, {"ollama_model": "a", "ollama_base_url": "b"})
        assert set(changed) == {"ollama_model", "ollama_base_url"}

    def test_ignores_unknown_keys(self, db):
        assert config.save(db, {"nope": "x"}) == []

    def test_empty_secret_does_not_overwrite(self, db):
        config.save(db, {"openrouter_api_key": "sk-or-secret"})
        changed = config.save(db, {"openrouter_api_key": ""})
        assert "openrouter_api_key" not in changed
        assert config.load(db, env={})["openrouter_api_key"] == "sk-or-secret"

    def test_empty_string_does_not_wipe_default(self, db):
        config.save(db, {"ollama_model": "modell"})
        config.save(db, {"ollama_model": ""})
        assert config.load(db, env={})["ollama_model"] == "modell"

    def test_zero_is_a_valid_value(self, db):
        # 0 ist bei collect_max_entries ein gültiger Wert ("unbegrenzt")
        config.save(db, {"collect_max_entries": "0"})
        assert config.load(db, env={})["collect_max_entries"] == 0

    def test_secret_can_be_replaced(self, db):
        config.save(db, {"openrouter_api_key": "sk-or-alt"})
        config.save(db, {"openrouter_api_key": "sk-or-neu"})
        assert config.load(db, env={})["openrouter_api_key"] == "sk-or-neu"


class TestPublicView:
    def test_secret_is_masked(self):
        view = config.public_view({"openrouter_api_key": "sk-or-1234567890"})
        assert "1234567890" not in str(view["openrouter_api_key"])
        assert view["openrouter_api_key"]["is_set"] is True
        assert view["openrouter_api_key"]["preview"].startswith("sk")
        assert view["openrouter_api_key"]["preview"].endswith("7890")

    def test_empty_secret_reports_unset(self):
        view = config.public_view({"openrouter_api_key": ""})
        assert view["openrouter_api_key"]["is_set"] is False

    def test_short_secret_fully_masked(self):
        view = config.public_view({"openrouter_api_key": "abc"})
        assert view["openrouter_api_key"]["preview"] == "***"

    def test_non_secrets_pass_through(self):
        view = config.public_view({"ollama_model": "llama3.2:3b"})
        assert view["ollama_model"] == "llama3.2:3b"


class TestTestConnection:
    def test_openrouter_without_key(self):
        res = config.test_connection("openrouter", {"openrouter_api_key": ""})
        assert res["ok"] is False
        assert "Key" in res["message"]

    def test_ollama_without_url(self):
        res = config.test_connection("ollama", {"ollama_base_url": ""})
        assert res["ok"] is False
        assert "URL" in res["message"]

    def test_unknown_provider(self):
        res = config.test_connection("gibtsnicht", {})
        assert res["ok"] is False
        assert "Unbekannter" in res["message"]

    def test_unreachable_provider(self, monkeypatch):
        """Ein nicht erreichbarer Provider muss als Fehler erscheinen, nicht crashen.

        Der Test läuft ohne Netz (`pytest-socket` blockiert Sockets), deshalb
        wird der Verbindungsaufbau hier per Attrappe simuliert.
        """
        import urllib.error

        def boom(*args, **kwargs):
            raise urllib.error.URLError("nicht erreichbar")

        monkeypatch.setattr("urllib.request.urlopen", boom)
        res = config.test_connection(
            "ollama",
            {"ollama_base_url": "http://127.0.0.1:1/v1", "ollama_model": "x"},
        )
        assert res["ok"] is False
        assert "erreichbar" in res["message"]


class TestFieldMeta:
    def test_every_field_has_a_label(self):
        for key in config.DEFAULTS:
            assert key in config.FIELD_META, f"{key} ohne Beschreibung"
            assert config.FIELD_META[key].get("label")
            assert config.FIELD_META[key].get("hint")

    def test_every_group_is_known(self):
        for meta in config.FIELD_META.values():
            assert meta["group"] in config.GROUP_ORDER

    def test_secret_keys_are_documented(self):
        for key in config.SECRET_KEYS:
            assert key in config.FIELD_META

class TestSaveIsTruthful:
    """Die Oberfläche schickt alle Felder – nur echte Änderungen dürfen zählen."""

    def test_unchanged_values_are_not_reported(self, db):
        config.save(db, {"ollama_model": "a"})
        changed = config.save(db, {"ollama_model": "a"})
        assert changed == [], "unveränderter Wert darf nicht als Änderung gelten"

    def test_only_changed_field_reported(self, db):
        config.save(db, {"ollama_model": "a", "ollama_base_url": "u"})
        changed = config.save(
            db, {"ollama_model": "a", "ollama_base_url": "neu"}
        )
        assert changed == ["ollama_base_url"]

    def test_first_write_reports_all(self, db):
        # Noch nichts gespeichert → jeder Wert ist eine Änderung
        changed = config.save(db, {"ollama_model": "a", "ollama_base_url": "u"})
        assert set(changed) == {"ollama_model", "ollama_base_url"}

    def test_changed_value_is_persisted(self, db):
        config.save(db, {"enrich_batch": "100"})
        assert config.save(db, {"enrich_batch": "250"}) == ["enrich_batch"]
        assert config.load(db, env={})["enrich_batch"] == 250


class TestMasking:
    """Maskierung darf auch bei Grenzfällen nicht durchschlagen."""

    def test_long_key_keeps_only_edges(self):
        view = config.public_view({"openrouter_api_key": "sk-or-abcdefghijklmnop"})
        preview = view["openrouter_api_key"]["preview"]
        assert preview.startswith("sk")
        assert preview.endswith("mnop")
        assert "cdefghij" not in preview

    def test_preview_length_matches_key(self):
        secret = "sk-or-1234567890"
        view = config.public_view({"openrouter_api_key": secret})
        assert len(view["openrouter_api_key"]["preview"]) == len(secret)
