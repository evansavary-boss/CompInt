import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import enrich
from src.config import load_settings, load_yaml
from src.entities import load_reference
from src.models import Item
from src.stories import StoryBook

ACFG = load_yaml("config/analysis.yaml")
NOW = datetime(2026, 10, 26, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))


@pytest.fixture(scope="module")
def ref():
    return load_reference(load_settings())


def item(iid, title, days_ago=0, sources=1):
    return Item(id=iid, title=title, url=f"https://ex.com/{iid}", source=f"Media {iid}", section="economie",
                published=(NOW - timedelta(days=days_ago)).isoformat(), summary="Extrait de l'article.")


def answer(sid, **kw):
    base = {"id": sid, "relevant": True, "duplicate_of": "", "title_fr": "Titre", "summary": "Résumé.",
            "why": "Parce que.", "tabs": ["marche"], "themes": ["appels_offres"], "industries": ["defense"],
            "countries": ["FR"], "impact": "moyen"}
    base.update(kw)
    return base


class FakeAnalyzer(enrich.StoryAnalyzer):
    name = "fake"

    def __init__(self, acfg, entries=None, error=None):
        super().__init__(acfg)
        self.entries, self.error, self.calls = entries, error, []

    def analyse(self, stories, recent):
        self.calls.append((stories, recent))
        if self.error:
            raise enrich.AnalysisError(self.error)
        return enrich.BatchResult(self.entries or [answer(s["id"]) for s in stories], cost_usd=0.01)


@pytest.fixture
def fake(monkeypatch):
    holder = {}

    def factory(entries=None, error=None):
        analyzer = FakeAnalyzer(ACFG, entries, error)
        monkeypatch.setattr(enrich, "get_analyzer", lambda acfg: analyzer)
        holder["a"] = analyzer
        return analyzer
    return factory


def book_with(items):
    book = StoryBook()
    book.assign(items, ACFG["stories"])
    return book


def test_schema_and_prompt_follow_config():
    schema = enrich.build_schema(ACFG)
    props = schema["properties"]["stories"]["items"]["properties"]
    assert props["impact"]["enum"] == list(ACFG["impact"])
    assert props["industries"]["items"]["enum"] == list(ACFG["industries"])
    prompt = enrich.build_system_prompt(ACFG)
    assert "ogme" in prompt and "Impact fort" not in prompt or "fort" in prompt
    json.dumps(schema)   # serialisable for --json-schema


def test_validate_drops_unknown_values():
    clean = enrich.validate(answer("s1", tabs=["marche", "bogus"], industries=["defense", "x"],
                                   impact="énorme", duplicate_of="s1", countries=["FR", "fr", "FRA"]),
                            ACFG, {"s1"})
    assert clean["tabs"] == ["marche"] and clean["industries"] == ["defense"]
    assert clean["impact"] == "faible" and clean["duplicate_of"] == "" and clean["countries"] == ["FR"]
    assert enrich.validate(answer("unknown"), ACFG, {"s1"}) is None


def test_analyse_stories_applies_results(fake, ref):
    items = [item("a", "SatCen tender for imagery"), item("b", "Unrelated football news")]
    book = book_with(items)
    fake([answer("sa", impact="fort"), answer("sb", relevant=False, title_fr="")])
    report = enrich.analyse_stories(book, items, ACFG, ref, NOW)
    assert report["analysed"] == 2 and report["cost_usd"] == 0.01
    assert book.stories["sa"].analysis["impact"] == "fort"
    assert book.stories["sb"].analysis["relevant"] is False
    assert book.stories["sa"].analysed_size == 1


def test_duplicates_found_by_claude_are_merged(fake, ref):
    items = [item("a", "Ukraine signs satcom deal with ICEYE"), item("b", "L'Ukraine signe avec ICEYE")]
    book = book_with(items)
    assert len(book.stories) == 2                      # different languages: rules miss it
    fake([answer("sa"), answer("sb", duplicate_of="sa")])
    report = enrich.analyse_stories(book, items, ACFG, ref, NOW)
    assert report["merged"] == 1
    assert list(book.stories) == ["sa"] and book.stories["sa"].item_ids == ["a", "b"]


def test_failure_keeps_rules_only(fake, ref):
    items = [item("a", "Something")]
    book = book_with(items)
    fake(error="usage limit reached")
    report = enrich.analyse_stories(book, items, ACFG, ref, NOW)
    assert "usage limit" in report["error"] and report["analysed"] == 0
    assert book.stories["sa"].analysis == {}


def test_selection_cap_age_and_priority(fake, ref):
    items = [item("old", "Old story", days_ago=30), item("a", "Small story"),
             item("b1", "Big story about Planet"), item("b2", "Big story about Planet!", days_ago=0)]
    book = book_with(items)
    analyzer = fake()
    enrich.analyse_stories(book, items, ACFG, ref, NOW, max_stories=1)
    sent = [s["id"] for s in analyzer.calls[0][0]]
    assert sent == ["sb1"]                              # most sources first, cap respected
    assert book.stories["sold"].analysis == {}          # too old, never analysed


def test_unavailable_cli_is_reported(ref):
    acfg = json.loads(json.dumps(ACFG))
    acfg["analysis"]["command"] = "definitely-not-a-real-binary-xyz"
    analyzer = enrich.get_analyzer(acfg)
    ok, why = analyzer.available()
    assert not ok and "introuvable" in why


def test_parse_cli_output():
    ok = json.dumps({"is_error": False, "structured_output": {"stories": [answer("s1")]}, "total_cost_usd": 0.2})
    res = enrich.parse_cli_output(ok, 0)
    assert res.entries[0]["id"] == "s1" and res.cost_usd == 0.2
    with pytest.raises(enrich.AnalysisError, match="limit"):
        enrich.parse_cli_output(json.dumps({"is_error": True, "result": "Claude usage limit reached"}), 1)
    with pytest.raises(enrich.AnalysisError):
        enrich.parse_cli_output("not json", 1, "boom")
