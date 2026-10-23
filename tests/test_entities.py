import copy
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import entities
from src.config import load_settings

PROPS = load_settings()["wikidata"]["properties"]


@pytest.fixture(scope="module")
def ref():
    return entities.load_reference(load_settings())


def test_reference_config_is_valid(ref):
    kinds = [e["kind"] for e in ref["entities"].values()]
    assert kinds.count("company") >= 50
    assert kinds.count("institution") >= 20
    assert "NO" in ref["countries"]          # quoted key, not the YAML boolean
    assert all(e.get("wikidata") for e in ref["entities"].values())


def test_validation_catches_common_mistakes(ref):
    bad = copy.deepcopy(ref)
    bad["countries"][False] = {"name": "Norvège", "region": "europe", "aliases": ["Norway"]}
    bad["entities"]["iceye"]["countries"] = ["XX"]
    bad["entities"]["planet"]["segments"] = ["unknown"]
    bad["entities"]["umbra"]["wikidata"] = ref["entities"]["iceye"]["wikidata"]
    bad["entities"]["esa"]["facts"] = {"budget": "7 Md€"}
    bad["events"]["launch"]["regex"] = ["("]
    errors = "\n".join(entities.validate_reference(bad, PROPS))
    assert "code pays invalide False" in errors
    assert "pays inconnu 'XX'" in errors
    assert "segment inconnu 'unknown'" in errors
    assert "déjà utilisé" in errors
    assert "fait inconnu 'budget'" in errors
    assert "regex invalide" in errors


def _claim(value, rank="normal", qualifiers=None):
    return {"rank": rank, "qualifiers": qualifiers or {},
            "mainsnak": {"datavalue": {"value": value}}}


ENTITY = {
    "claims": {
        "P571": [_claim({"time": "+2012-00-00T00:00:00Z", "precision": 9})],
        "P159": [_claim({"id": "Q1"})],
        "P169": [
            _claim({"id": "Q2"}, qualifiers={"P582": [{"datavalue": {"value": {"time": "+2020-01-01T00:00:00Z"}}}]}),
            _claim({"id": "Q3"}),
            _claim({"id": "Q4"}, rank="deprecated"),
        ],
        "P1128": [
            _claim({"amount": "+500"}, qualifiers={"P585": [{"datavalue": {"value": {"time": "+2021-01-01T00:00:00Z"}}}]}),
            _claim({"amount": "+800"}, qualifiers={"P585": [{"datavalue": {"value": {"time": "+2024-01-01T00:00:00Z"}}}]}),
        ],
        "P856": [_claim("https://example.com"), _claim("https://old.example.com", rank="deprecated")],
        "P154": [_claim("Example logo.svg")],
    },
    "sitelinks": {"enwiki": {"title": "Example Corp"}, "frwiki": {"title": "Example (entreprise)"}},
}


def test_parse_claims_picks_current_values():
    raw = entities.parse_claims(ENTITY, PROPS)
    assert raw["founded"] == 2012
    assert raw["headquarters"] == "Q1"
    assert raw["ceo"] == "Q3"                         # ended and deprecated claims skipped
    assert raw["employees"] == {"value": 800, "year": 2024}
    assert raw["website"] == "https://example.com"
    assert raw["wikipedia"] == "https://fr.wikipedia.org/wiki/Example_%28entreprise%29"


def test_finalize_facts_resolves_labels():
    raw = entities.parse_claims(ENTITY, PROPS)
    facts = entities.finalize_facts(raw, {"Q1": "Espoo"})
    assert facts["headquarters"] == "Espoo"
    assert "ceo" not in facts                          # unknown label: dropped, never a bare QID
    assert facts["logo"].startswith("https://commons.wikimedia.org/wiki/Special:FilePath/Example_logo.svg")


def _settings(tmp_path):
    s = load_settings()
    s = copy.deepcopy(s)
    s["paths"]["entities"] = str(tmp_path / "entities.json")
    return s


class FailingClient:
    def get_entities(self, *args, **kwargs):
        raise RuntimeError("network down")


def test_refresh_keeps_cache_when_wikidata_fails(tmp_path, ref):
    settings = _settings(tmp_path)
    old = (datetime.now(ZoneInfo("Europe/Paris")) - timedelta(days=30)).isoformat()
    cache = {"entities": {"iceye": {"qid": ref["entities"]["iceye"]["wikidata"], "fetched_at": old,
                                    "facts": {"headquarters": "Espoo"}}}}
    (tmp_path / "entities.json").write_text(json.dumps(cache), encoding="utf-8")

    result = entities.refresh(settings, ref, client=FailingClient())
    assert result["error"] == "network down"
    assert result["entities"]["iceye"]["facts"]["headquarters"] == "Espoo"


def test_stale_detection(ref):
    now = datetime(2026, 10, 23, tzinfo=ZoneInfo("Europe/Paris"))
    qid = ref["entities"]["iceye"]["wikidata"]
    fresh = {"entities": {"iceye": {"qid": qid, "fetched_at": (now - timedelta(days=1)).isoformat()}}}
    stale = entities.stale_entities(ref, fresh, now, refresh_days=7)
    assert "iceye" not in stale and "planet" in stale
    assert "iceye" in entities.stale_entities(ref, fresh, now, refresh_days=7, force=True)


def test_yaml_facts_override_wikidata(ref):
    cache = {"entities": {"arianespace": {"facts": {"ceo": "Ancien dirigeant", "founded": 1980}}}}
    merged = entities.merged_entities(ref, cache)["arianespace"]
    assert merged["facts"]["ceo"] == ref["entities"]["arianespace"]["facts"]["ceo"]
    assert merged["facts"]["founded"] == 1980
    assert merged["facts_overridden"] == ["ceo"]
