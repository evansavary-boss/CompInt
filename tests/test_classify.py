import pytest

from src.classify import Classifier
from src.collect import gnews_sources, split_gnews_title
from src.config import load_settings
from src.entities import load_reference
from src.models import Item
from src.textmatch import TermIndex, fold


@pytest.fixture(scope="module")
def ref():
    return load_reference(load_settings())


@pytest.fixture(scope="module")
def clf(ref):
    return Classifier(ref)


def item(title, summary="", section="economie", query_entities=()):
    return Item(id=title, title=title, url="u", source="s", section=section,
                published="2026-10-22T08:00:00+02:00", summary=summary,
                query_entities=list(query_entities))


# ------------------------------------------------------------ text matching

def test_fold_keeps_offsets():
    text = "Défense spatiale – IRIS² à Kourou"
    assert len(fold(text)) == len(text)
    assert fold(text).startswith("defense spatiale")


def test_longest_match_wins():
    idx = TermIndex()
    idx.add("Thales", "thales", case_sensitive=True)
    idx.add("Thales Alenia Space", "tas")
    assert idx.targets("Thales Alenia Space wins a contract") == ["tas"]
    assert idx.targets("Thales and Thales Alenia Space") == ["thales", "tas"]


def test_plurals_and_hyphens():
    idx = TermIndex(plurals=True)
    idx.add("lanceur", "launch")
    idx.add("Vega C", "avio")
    assert idx.targets("Les lanceurs européens") == ["launch"]
    assert idx.targets("Vega-C returns to flight") == ["avio"]


# ------------------------------------------------------------ entities

def test_entities_aliases_and_case(clf):
    assert clf.find_entities("Thales Alenia Space and Leonardo sign for IRIS²") == ["thales-alenia-space", "leonardo"]
    assert "ses" in clf.find_entities("SES orders O3b satellites")
    assert "ses" not in clf.find_entities("Ses résultats sont en hausse")
    assert clf.find_entities("Starlink outage hits Ukraine") == ["spacex"]
    assert clf.find_entities("Safran.AI wins NATO deal")[:1] == ["safran-ai"]


# ------------------------------------------------------------ geography

def test_countries_from_text_and_entities(clf):
    it = clf.classify(item("ICEYE signs deal with the Portuguese navy and Japan"))
    assert it.countries[:2] == ["JP", "FI"] or set(it.countries) >= {"JP", "FI"}
    assert "europe" in it.regions and "asia_pacific" in it.regions


def test_supranational_institution_region(clf):
    it = clf.classify(item("ESA Council approves new directors", section="politique"))
    assert it.entities == ["esa"]
    assert it.regions == ["europe"]
    assert it.countries == []


def test_acronym_country(clf):
    it = clf.classify(item("US and UK agree on space situational awareness"))
    assert set(it.countries) >= {"US", "GB"}
    it = clf.classify(item("Nous avons reçu une commande"))   # "us" inside French words
    assert "US" not in it.countries


# ------------------------------------------------------------ events and tabs

def test_contract_won(clf):
    it = clf.classify(item("Space Development Agency awards Rocket Lab $500M contract"))
    assert "contract_won" in it.events
    assert "economie" in it.sections


def test_contract_lost_overrides_won(clf):
    it = clf.classify(item("Air Force cancels contract awarded to Firefly"))
    assert "contract_lost" in it.events
    assert "contract_won" not in it.events


def test_funding_and_french_patterns(clf):
    assert "funding" in clf.classify(item("Exotrail lève 50 millions d'euros")).events
    assert "funding" in clf.classify(item("Pixxel raises $60M in Series C")).events
    assert "mna" in clf.classify(item("Safran rachète une start-up")).events


def test_politics_tab(clf):
    # "unveils" (new product) needs a company: here it is a government strategy.
    it = clf.classify(item("France unveils its new national space strategy", section="economie"))
    assert it.events == ["strategy"]
    assert it.sections == ["politique"]


def test_business_events_need_a_company(clf):
    assert "mna" not in clf.classify(item("NGA improves data acquisition for maps")).events
    assert "partnership" not in clf.classify(item("NATO deepens partnership with Ukraine")).events
    assert "partnership" in clf.classify(item("ICEYE announces partnership with Rheinmetall")).events


def test_tabs_without_event(clf):
    assert clf.classify(item("A day at CNES", section="economie")).sections == ["politique", "economie"]
    assert clf.classify(item("Interview with the ICEYE founder", section="politique")).sections == ["politique", "economie"]
    assert clf.classify(item("Nothing to see here", section="politique")).sections == ["politique"]


def test_query_entity_added(clf):
    it = clf.classify(item("Les satellites de la start-up rennaise", query_entities=["unseenlabs"]))
    assert "unseenlabs" in it.entities and "FR" in it.countries


# ------------------------------------------------------------ Google News

def test_split_gnews_title():
    assert split_gnews_title("ICEYE wins deal - SpaceNews", "SpaceNews") == "ICEYE wins deal"
    assert split_gnews_title("Title - with dash", "Other") == "Title - with dash"


def test_gnews_sources(ref):
    settings = load_settings()
    sources = gnews_sources(ref, settings)
    by_entity = {}
    for s in sources:
        by_entity.setdefault(s["entity"], []).append(s)
    assert len(by_entity) == len(ref["entities"])
    assert len(by_entity["unseenlabs"]) == 2                 # French entity: en + fr editions
    assert by_entity["thales"][0]["require_keywords"] is True   # diversified defence group
    assert by_entity["iceye"][0]["require_keywords"] is False
    assert by_entity["cnes"][0]["section"] == "politique"
    assert "when%3A3d" in by_entity["iceye"][0]["url"]


def test_contract_phrasings(clf):
    for title in ("BlackSky wins expanded contract with defense partner",
                  "L3Harris Receives $33.1 Million Space Force Contract Modification",
                  "Synspective signs 20-launch agreement with Rocket Lab",
                  "Thales décroche un contrat de 300 millions"):
        assert "contract_won" in clf.classify(item(title)).events, title


def test_contract_amount_is_not_funding(clf):
    events = clf.classify(item("HawkEye 360 secures $36M in Q3 international contracts")).events
    assert "contract_won" in events and "funding" not in events
    assert "funding" in clf.classify(item("Xona Space Systems Secures $250M Series D")).events


def test_appointment_follows_entity_kind(clf):
    assert clf.classify(item("Rocket Factory Augsburg appoints new CEO", section="politique")).sections == ["economie"]
    assert clf.classify(item("ESA Council names new directors", section="economie")).sections == ["politique"]


def test_stock_noise_is_excluded(ref):
    from src.config import load_yaml
    from src.filter import KeywordMatcher
    m = KeywordMatcher(load_yaml("config/keywords.yaml"))
    assert m.is_excluded("Firefly Aerospace (NasdaqGM:FLY) Stock Forecast & Analyst Predictions")
    assert m.is_excluded("Could Investing $5,000 in Redwire Stock Make You a Millionaire?")
    assert not m.is_excluded("Rocket Lab Q3 revenue beats guidance")
