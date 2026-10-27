import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src import collect
from src.build import on_page
from src.classify import Classifier, geo_theme_index
from src.config import load_settings, load_yaml
from src.entities import load_reference
from src.filter import KeywordMatcher
from src.models import Item
from src.worldmap import build_paths, equal_earth

GCFG = load_yaml("config/geopolitics.yaml")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))


@pytest.fixture(scope="module")
def ref():
    return load_reference(load_settings())


def item(title, via="", summary="", section="geopolitique"):
    return Item(id=title, title=title, url="u", source="s", section=section,
                published=NOW.isoformat(), summary=summary, via=via)


def gate(items, **src):
    return collect.geo_gate(items, src, geo_theme_index(GCFG), collect.geo_exclusions(GCFG),
                            KeywordMatcher(load_yaml("config/keywords.yaml")))


def test_geo_gate_keeps_matching_families():
    kept = gate([item("Houthi missiles hit a tanker in the Red Sea"),
                 item("Local bakery wins award"),
                 item("Sanctions on Russian oil exports tightened")])
    assert [i.title for i in kept] == ["Houthi missiles hit a tanker in the Red Sea",
                                       "Sanctions on Russian oil exports tightened"]
    assert set(kept[0].geo_themes) >= {"conflits", "ressources"}
    assert set(kept[1].geo_themes) >= {"sanctions", "ressources"}


def test_geo_gate_exclusions_and_title_only_for_searches():
    assert gate([item("Champions League: war of nerves before the final")]) == []
    # Searches are judged on the title only: GDELT and Google News match article bodies.
    assert gate([item("Mayor opens new library", via="gdelt", summary="war")]) == []


def test_specialised_feed_keeps_its_family_and_title_filter():
    gdacs = dict(theme="crises", title_include_regex="^(Orange|Red) ")
    kept = gate([item("Red alert for tropical system in Philippines"),
                 item("Green earthquake alert (Magnitude 4.5M) in Indonesia")], **gdacs)
    assert [i.title for i in kept] == ["Red alert for tropical system in Philippines"]
    assert kept[0].geo_themes == ["crises"]


def test_geo_sources_built_from_config(ref):
    settings = load_settings()
    sources = collect.geo_sources(GCFG, settings)
    vias = {s["via"] for s in sources}
    assert vias == {"", "gnews", "gdelt"}
    assert all(s["section"] == "geopolitique" and s["gate"] == "geo" for s in sources)
    assert collect.geo_sources(GCFG, settings, use_searches=False) == [s for s in sources if s["via"] == ""]


def test_gdelt_parsing(monkeypatch):
    payload = {"articles": [{"url": "https://ex.com/a", "title": "Sanctions on Iran tightened",
                             "seendate": "20261026T071500Z", "domain": "ex.com"}]}

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return payload

    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: Resp())
    settings = load_settings()
    items, status = collect.collect_gdelt({"name": "GDELT · sanctions", "url": "u", "section": "geopolitique"},
                                          settings, NOW.tzinfo, NOW)
    assert status.ok and items[0].source == "ex.com" and items[0].via == "gdelt"
    assert items[0].published == "2026-10-26T08:15:00+01:00"


def test_gdelt_rate_limit_text_is_an_error(monkeypatch):
    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): raise ValueError("not json")

    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: Resp())
    items, status = collect.collect_gdelt({"name": "g", "url": "u", "section": "geopolitique"},
                                          load_settings(), NOW.tzinfo, NOW)
    assert items == [] and not status.ok and "GDELT" in status.error


def test_gdelt_retries_once_after_429(monkeypatch):
    calls = []

    class Resp:
        def __init__(self, code): self.status_code = code
        def raise_for_status(self): pass
        def json(self): return {"articles": []}

    def fake_get(*a, **k):
        calls.append(1)
        return Resp(429 if len(calls) == 1 else 200)

    monkeypatch.setattr(collect.requests, "get", fake_get)
    monkeypatch.setattr(collect.time, "sleep", lambda s: None)
    items, status = collect.collect_gdelt({"name": "g", "url": "u", "section": "geopolitique", "retry_after": 0},
                                          load_settings(), NOW.tzinfo, NOW)
    assert len(calls) == 2 and status.ok


def test_title_only_feeds_ignore_the_excerpt():
    assert gate([item("Key findings of the assessment", summary="humanitarian crisis")], title_only=True) == []
    assert gate([item("Key findings of the assessment", summary="humanitarian crisis")]) != []


def test_classifier_geo_tab_and_countries(ref):
    clf = Classifier(ref, GCFG)
    it = clf.classify(item("Sudan war: RSF advance near Khartoum"))
    assert "geopolitique" in it.sections and "SD" in it.countries and "conflits" in it.geo_themes


def test_on_page_uses_claude_tabs_then_rules():
    row = {"analysis_tabs": ["geopolitique"], "sections": ["economie"]}
    assert on_page(row, "geopolitique") and not on_page(row, "economie")
    row = {"analysis_tabs": [], "sections": ["geopolitique", "politique"]}
    assert on_page(row, "geopolitique") and on_page(row, "economie")


def test_projection_and_map_paths(ref):
    assert equal_earth(0, 0) == (0.0, 0.0)
    x, _ = equal_earth(180, 0)
    assert abs(x - 2.7064) < 0.001
    world = build_paths(Path("config/geodata/countries-110m.json"), ref["countries"])
    assert {"FR", "US", "YE", "UA", "SD"} <= set(world["shapes"])
    assert len(world["shapes"]) > 150
    assert all(d.startswith("M") for d in world["shapes"].values())
