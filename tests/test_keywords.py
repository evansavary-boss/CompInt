import pytest

from src.config import load_yaml
from src.enrich import truncate
from src.filter import KeywordMatcher, tag_items
from src.models import Item

CONFIG = {
    "domains": {
        "geospatial": {
            "include": ["observation de la Terre", "satellite imagery", "géospatial"],
            "acronyms": ["SAR", "GEOINT"],
            "exclude": ["search and rescue"],
        },
        "newspace": {
            "include": ["lanceur", "constellation", "orbite basse"],
            "acronyms": ["LEO"],
            "exclude": [],
        },
    },
    "global_exclude": ["horoscope", "trou noir"],
}


@pytest.fixture
def matcher():
    return KeywordMatcher(CONFIG)


def test_accent_and_case_insensitive(matcher):
    assert matcher.match("Nouveau satellite d'OBSERVATION DE LA TERRE") == ["geospatial"]
    assert matcher.match("Le marché geospatial européen") == ["geospatial"]


def test_plural_and_both_domains(matcher):
    tags = matcher.match("Les lanceurs européens et l'imagerie : satellite imagery boom")
    assert tags == ["geospatial", "newspace"]


def test_whole_words_only(matcher):
    assert matcher.match("A sarcastic article about telecommunications") == []
    assert matcher.match("Reconstellation of ideas") == []


def test_acronyms_are_case_sensitive(matcher):
    assert matcher.match("New SAR satellite") == ["geospatial"]
    assert matcher.match("sar and leo are names") == []
    assert matcher.match("Deploying to LEO") == ["newspace"]


def test_domain_exclusion(matcher):
    assert matcher.match("SAR helicopter for search and rescue") == []


def test_global_exclusion(matcher):
    assert matcher.is_excluded("Votre horoscope du jour")
    assert matcher.is_excluded("Un trou noir observé par un lanceur")
    assert not matcher.is_excluded("Constellation IRIS²")


def _item(title, source):
    return Item(id=title, title=title, url="u", source=source, section="politique",
                published="2026-10-22T08:00:00+02:00", summary="")


def test_tag_items_general_vs_specialised_sources(matcher):
    sources = {
        "general": {"domain": "newspace", "require_keywords": True},
        "special": {"domain": "geospatial", "require_keywords": False},
    }
    items = [
        _item("Budget des armées voté", "general"),          # dropped: no keyword
        _item("Nouveau lanceur réutilisable", "general"),   # kept: keyword
        _item("Interview du CEO", "special"),                # kept: default domain
        _item("Horoscope spatial", "special"),               # dropped: global exclude
    ]
    kept = tag_items(items, matcher, sources)
    assert [(i.title, i.tags) for i in kept] == [
        ("Nouveau lanceur réutilisable", ["newspace"]),
        ("Interview du CEO", ["geospatial"]),
    ]


def test_real_keywords_file_loads():
    m = KeywordMatcher(load_yaml("config/keywords.yaml"))
    assert "geospatial" in m.match("ICEYE launches new SAR satellites")
    assert m.match("Ses résultats trimestriels") == []


def test_truncate_excerpt():
    text = "mot " * 100
    out = truncate(text, 200)
    assert len(out) <= 200 and out.endswith("…")
    assert truncate("court", 200) == "court"
