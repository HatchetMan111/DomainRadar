"""Tests für die Klassifikations-Kaskade.

Im Kern geht es um die Frage: Was passiert, wenn Stufe 1 ausfällt, wenn sie
schlechte Konfidenz liefert, wenn sie kein JSON liefert?
"""

from __future__ import annotations

import json

import pytest

from domainradar.classifier import (
    SYSTEM_PROMPT,
    Provider,
    build_chain,
    classify_batch,
    classify_deterministic,
    normalize_result,
    parse_llm_response,
    run_once,
)


class FakeProvider(Provider):
    """Provider-Attrappe, die eine feste Antwort liefert oder wirft."""

    def __init__(self, name="fake", response=None, error=None):
        super().__init__(name=name, base_url="http://fake/v1", model="fake-model")
        self.response = response
        self.error = error
        self.calls = 0

    def chat(self, system, user, timeout=60):
        self.calls += 1
        if self.error:
            raise RuntimeError(self.error)
        return self.response or ""


def _wrap(results):
    return json.dumps({"results": results})


def _item(domain, confidence=0.9, category="software", sells=True, segment="b2b"):
    return {
        "domain": domain,
        "category": category,
        "segment": segment,
        "business_type": "SaaS",
        "sells_own_product": sells,
        "confidence": confidence,
        "summary_de": "Test",
    }


class TestParseLLMResponse:
    def test_plain_json(self):
        out = parse_llm_response(_wrap([_item("a.de")]))
        assert len(out["results"]) == 1
        assert out["results"][0]["domain"] == "a.de"

    def test_markdown_fence(self):
        raw = "```json\n" + _wrap([_item("a.de")]) + "\n```"
        out = parse_llm_response(raw)
        assert out["results"][0]["domain"] == "a.de"

    def test_text_before_json(self):
        raw = "Hier ist das Ergebnis:\n" + _wrap([_item("a.de")]) + "\nDanke."
        out = parse_llm_response(raw)
        assert out["results"][0]["domain"] == "a.de"

    def test_bare_object(self):
        raw = json.dumps(_item("a.de"))
        out = parse_llm_response(raw)
        assert out["results"][0]["domain"] == "a.de"

    def test_invalid_raises(self):
        with pytest.raises(json.JSONDecodeError):
            parse_llm_response("garbage")


class TestNormalizeResult:
    def test_fills_missing_fields(self):
        r = normalize_result({"domain": "a.de"}, "a.de")
        assert r["category"] == "other"
        assert r["segment"] == "b2c"
        assert r["sells_own_product"] is False
        assert r["confidence"] == 0.0

    def test_clamps_confidence(self):
        assert normalize_result({"confidence": 5}, "a.de")["confidence"] == 1.0
        assert normalize_result({"confidence": -5}, "a.de")["confidence"] == 0.0

    def test_unknown_category_falls_back(self):
        assert normalize_result({"category": "raumfahrt"}, "a.de")["category"] == "other"

    def test_string_boolean(self):
        r = normalize_result({"sells_own_product": "true"}, "a.de")
        assert r["sells_own_product"] is True

    def test_case_insensitive_category(self):
        assert normalize_result({"category": "ENERGY"}, "a.de")["category"] == "energy"


class TestDeterministicFallback:
    def test_keyword_match(self):
        r = classify_deterministic("Photovoltaik Rechner", "Solar Energie", 2000)
        assert r["category"] == "energy"
        assert r["sells_own_product"] is True

    def test_short_page_no_offer(self):
        r = classify_deterministic("Software", "app", 100)
        assert r["sells_own_product"] is False

    def test_confidence_below_threshold(self):
        # Der Fallback muss unter der Eskalationsschwelle bleiben, sonst würde
        # er nie an Stufe 2 weitergegeben.
        r = classify_deterministic("Software", "app", 2000)
        assert r["confidence"] < 0.75


class TestClassifyBatch:
    def test_no_chain_uses_deterministic(self):
        items = [{"domain": "a.de", "title": "Solar", "description": "PV", "text": ""}]
        results, provider, _ = classify_batch(items, [])
        assert provider == "deterministic"
        assert results["a.de"]["category"] == "energy"

    def test_stage1_success(self):
        stage1 = FakeProvider(response=_wrap([_item("a.de", 0.95)]))
        items = [{"domain": "a.de", "title": "x", "description": "y", "text": ""}]
        results, provider, _ = classify_batch(items, [stage1])
        assert stage1.calls == 1
        assert provider == "fake"
        assert results["a.de"]["confidence"] == 0.95

    def test_low_confidence_escalates(self):
        stage1 = FakeProvider(response=_wrap([_item("a.de", 0.4)]))
        stage2 = FakeProvider(name="openrouter", response=_wrap([_item("a.de", 0.9)]))
        items = [{"domain": "a.de", "title": "x", "description": "y", "text": ""}]
        results, provider, _ = classify_batch(items, [stage1, stage2])
        assert stage1.calls == 1
        assert stage2.calls == 1
        assert provider == "openrouter"
        assert results["a.de"]["confidence"] == 0.9

    def test_high_confidence_does_not_escalate(self):
        stage1 = FakeProvider(response=_wrap([_item("a.de", 0.95)]))
        stage2 = FakeProvider(name="openrouter", response=_wrap([_item("a.de", 0.5)]))
        items = [{"domain": "a.de", "title": "x", "description": "y", "text": ""}]
        results, _, _ = classify_batch(items, [stage1, stage2])
        assert stage2.calls == 0
        assert results["a.de"]["confidence"] == 0.95

    def test_stage1_unreachable_falls_back_to_stage2(self):
        stage1 = FakeProvider(error="nicht erreichbar")
        stage2 = FakeProvider(name="openrouter", response=_wrap([_item("a.de", 0.9)]))
        items = [{"domain": "a.de", "title": "x", "description": "y", "text": ""}]
        results, provider, _ = classify_batch(items, [stage1, stage2])
        assert stage2.calls == 1
        assert provider == "openrouter"

    def test_both_stages_fail_uses_deterministic(self):
        stage1 = FakeProvider(error="down")
        stage2 = FakeProvider(name="openrouter", error="auch down")
        items = [{"domain": "a.de", "title": "Solar Energie", "description": "PV", "text": ""}]
        results, _, _ = classify_batch(items, [stage1, stage2])
        assert results["a.de"]["category"] == "energy"
        assert results["a.de"]["confidence"] < 0.75

    def test_bad_json_in_stage1_still_works(self):
        stage1 = FakeProvider(response="kein json hier")
        stage2 = FakeProvider(name="openrouter", response=_wrap([_item("a.de", 0.8)]))
        items = [{"domain": "a.de", "title": "x", "description": "y", "text": ""}]
        results, _, _ = classify_batch(items, [stage1, stage2])
        assert results["a.de"]["confidence"] == 0.8

    def test_empty_items(self):
        assert classify_batch([], [FakeProvider()]) == ({}, "", "")


class TestBuildChain:
    def test_default_chain_has_ollama(self):
        chain = build_chain({})
        assert any(p.name == "ollama" for p in chain)

    def test_openrouter_without_key_skipped(self):
        chain = build_chain({"LLM_CHAIN": "openrouter"})
        assert chain == []

    def test_openrouter_with_key_included(self):
        chain = build_chain(
            {"LLM_CHAIN": "openrouter", "OPENROUTER_API_KEY": "sk-or-test"}
        )
        assert len(chain) == 1
        assert chain[0].name == "openrouter"

    def test_ollama_only(self):
        chain = build_chain({"LLM_CHAIN": "ollama"})
        assert [p.name for p in chain] == ["ollama"]

    def test_unknown_provider_ignored(self):
        chain = build_chain({"LLM_CHAIN": "ollama,nonexistent"})
        assert [p.name for p in chain] == ["ollama"]

    def test_custom_urls(self):
        chain = build_chain(
            {"LLM_CHAIN": "ollama", "OLLAMA_BASE_URL": "http://host:1/v1"}
        )
        assert chain[0].base_url == "http://host:1/v1"


class TestRunOnce:
    def test_classifies_open_domains(self, tmp_path):
        from domainradar.db import Database

        db = Database(tmp_path / "t.db")
        db.insert_domains([{"domain": "a.de"}, {"domain": "b.de"}])
        db.update_fetch("a.de", title="Solar Energie", text_len=2000, page_hash="x")
        db.update_fetch("b.de", title="Software App", text_len=2000, page_hash="y")

        fake = FakeProvider(response=_wrap([_item("a.de"), _item("b.de")]))
        stats = run_once(db, chain=[fake], batch_size=10)

        assert stats["classified"] == 2
        assert stats["provider"] == "fake"
        row = db.get_domain("a.de")
        assert row["classified_at"] is not None
        db.close()

class TestWordBoundaries:
    """Der Fallbackfilter muss ganze Wörter treffen, keine Wortteile.

    Realer Fehlfall aus dem Testlauf: cittasana.de ("KI Automatisierung")
    wurde als `mobility` erkannt, weil "auto" in "Automatisierung" steckte.
    """

    def test_auto_in_automation_is_not_mobility(self):
        r = classify_deterministic(
            "Cittasana — KI Automatisierung & Burnout-Prävention",
            "KI Automatisierung und systemische Beratung",
            2000,
        )
        assert r["category"] != "mobility"

    def test_burnout_is_health(self):
        r = classify_deterministic("Burnout-Prävention", "Beratung für Unternehmer", 2000)
        assert r["category"] == "health"

    def test_reisen_is_mobility(self):
        r = classify_deterministic("Reisen", "Gruppenreisen und Tagesausflüge", 2000)
        assert r["category"] == "mobility"

    def test_shop_is_retail(self):
        r = classify_deterministic("Shop für Kostüme", "Karnevalskostüme", 2000)
        assert r["category"] == "retail"

    def test_pv_is_energy(self):
        r = classify_deterministic("Photovoltaik Rechner", "Solar Energie", 2000)
        assert r["category"] == "energy"

    def test_software_is_software(self):
        r = classify_deterministic("Software-Plattform", "SaaS und Cloud", 2000)
        assert r["category"] == "software"

    def test_auto_still_matches_as_word(self):
        r = classify_deterministic("Auto Werkstatt", "KFZ Reparatur", 2000)
        assert r["category"] == "mobility"


class TestRealWorldCases:
    """Fälle aus dem E2E-Lauf mit echten frisch registrierten Domains."""

    def test_webagentur_is_services(self):
        r = classify_deterministic(
            "Webagentur Stuttgart: Websites, Shops & Automatisierung", "", 16000
        )
        assert r["category"] == "services"

    def test_auto_in_automatisierung_does_not_win(self):
        # "Automatisierung" enthält "auto" – darf nicht mobility auslösen
        r = classify_deterministic(
            "Webagentur: Websites, Shops & Automatisierung", "", 16000
        )
        assert r["category"] != "mobility"


class TestParkingDetection:
    """Parking-Erkennung mit echten Markern aus dem Testbetrieb.

    Die Reihenfolge in `detect_parking` ist Absicht: erst die Textmengen-Prüfung,
    dann die Marker. Eine echte Parkseite ist fast immer kurz – die Textlänge
    fängt sie also oft schon vorher. Die Marker sind die zweite Verteidigungslinie
    für geparkte Seiten, die genug Text haben, um durch die Längenprüfung zu fallen.
    """

    def test_domainpark_short_text_is_caught(self):
        from domainradar.enricher import detect_parking

        text = (
            "Domainpark - Bitte den Rasen nicht betreten. Vielen Dank! "
            "Diese Domain wurde geparkt."
        )
        # Die reale Seite hat 229 Zeichen und fällt durch die Längenprüfung
        assert detect_parking(text.lower(), text, len(text)) != ""

    def test_domainpark_marker_with_enough_text(self):
        from domainradar.enricher import detect_parking

        filler = "Diese Domain wurde geparkt. " * 30  # > 200 Zeichen
        text = "Domainpark " + filler
        assert detect_parking(text.lower(), text, len(text)) == "domainpark"

    def test_nginx_default_page_marker(self):
        from domainradar.enricher import detect_parking

        # HTML-Marker: wird gegen das rohe HTML geprüft
        html = "<html><body>Welcome to nginx welcome page</body></html>"
        text = "nginx " * 100
        assert detect_parking(html.lower(), text, len(text)) == "nginx welcome page"

    def test_real_page_is_not_parked(self):
        from domainradar.enricher import detect_parking

        text = "Webagentur Stuttgart " * 100  # > 200 Zeichen echter Inhalt
        assert detect_parking(text.lower(), text, len(text)) == ""

    def test_undef_in_css_class_does_not_trigger(self):
        # Regression: tlz.de hatte class="… undefined" in der Navigation
        from domainradar.enricher import detect_parking

        html = '<li class="shrink-0 lg:snap-align-none undefined">'
        text = "Thüringer Allgemeine Nachrichten " * 20
        assert detect_parking(html.lower(), text, len(text)) == ""

    def test_short_page_is_flagged(self):
        from domainradar.enricher import detect_parking

        assert detect_parking("", "winzig", 5) == "leer"
        assert detect_parking("", "x" * 100, 100) == "sehr wenig Text"


class TestSettingsDriveClassifier:
    """Änderungen aus der Einstellungsseite müssen die Kaskade steuern."""

    def test_chain_from_settings_ollama_only(self):
        from domainradar.classifier import chain_from_settings

        chain = chain_from_settings(
            {
                "llm_chain": "ollama",
                "ollama_base_url": "http://localhost:11434/v1",
                "ollama_model": "llama3.1:8b",
            }
        )
        assert [p.name for p in chain] == ["ollama"]
        assert chain[0].model == "llama3.1:8b"
        assert chain[0].base_url == "http://localhost:11434/v1"

    def test_chain_from_settings_with_openrouter(self):
        from domainradar.classifier import chain_from_settings

        chain = chain_from_settings(
            {
                "llm_chain": "ollama,openrouter",
                "ollama_base_url": "http://x/v1",
                "ollama_model": "m",
                "openrouter_api_key": "sk-or-test",
                "openrouter_model": "deepseek/deepseek-chat",
            }
        )
        assert [p.name for p in chain] == ["ollama", "openrouter"]

    def test_chain_from_settings_without_key_skips_openrouter(self):
        from domainradar.classifier import chain_from_settings

        chain = chain_from_settings(
            {
                "llm_chain": "ollama,openrouter",
                "ollama_base_url": "http://x/v1",
                "ollama_model": "m",
                "openrouter_api_key": "",
            }
        )
        assert [p.name for p in chain] == ["ollama"]

    def test_escalation_threshold_is_configurable(self):
        # Schwelle 0.95: Stufe 1 muss auch bei guter Konfidenz eskalieren
        stage1 = FakeProvider(response=_wrap([_item("a.de", 0.9)]))
        stage2 = FakeProvider(name="openrouter", response=_wrap([_item("a.de", 0.99)]))
        items = [{"domain": "a.de", "title": "x", "description": "y", "text": ""}]
        classify_batch(items, [stage1, stage2], escalate_below=0.95)
        assert stage2.calls == 1, "bei Schwelle 0,95 muss eskaliert werden"

    def test_escalation_threshold_high_skips(self):
        stage1 = FakeProvider(response=_wrap([_item("a.de", 0.99)]))
        stage2 = FakeProvider(name="openrouter", response=_wrap([_item("a.de", 0.5)]))
        items = [{"domain": "a.de", "title": "x", "description": "y", "text": ""}]
        classify_batch(items, [stage1, stage2], escalate_below=0.5)
        assert stage2.calls == 0, "bei Schwelle 0,5 darf nicht eskaliert werden"
