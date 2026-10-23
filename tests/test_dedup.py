from src.filter import canonical_url, dedupe, titles_similar
from src.models import Item

TRACKING = ["utm_source", "fbclid", "xtor"]


def make(id_, title, url, published="2026-10-22T08:00:00+02:00"):
    return Item(id=id_, title=title, url=url, source="s", section="entreprises",
                published=published, summary="")


def test_canonical_url_strips_tracking_www_and_slash():
    a = canonical_url("http://www.Example.com/news/article/?utm_source=rss&id=3#top", TRACKING)
    b = canonical_url("https://example.com/news/article?id=3&fbclid=xyz", TRACKING)
    assert a == b == "https://example.com/news/article?id=3"


def test_canonical_url_keeps_meaningful_params():
    assert canonical_url("https://e.com/a?p=1") != canonical_url("https://e.com/a?p=2")


def test_dedupe_by_url():
    items = [
        make("1", "ICEYE wins contract", "https://a.com/x?utm_source=feed"),
        make("2", "Totally different title", "https://www.a.com/x/"),
    ]
    assert [i.id for i in dedupe(items, 0.88, TRACKING)] == ["1"]


def test_dedupe_by_similar_title():
    items = [
        make("1", "Rocket Lab wins $500M Space Development Agency contract", "https://a.com/1"),
        make("2", "Rocket Lab wins $500M Space Development Agency contract.", "https://b.com/2"),
        make("3", "Rocket Lab launches Electron from New Zealand", "https://c.com/3"),
    ]
    assert [i.id for i in dedupe(items, 0.88, TRACKING)] == ["1", "3"]


def test_dedupe_against_existing_history():
    old = [make("old", "Eutelsat raises capital", "https://a.com/eut")]
    new = [make("new", "Eutelsat raises capital", "https://b.com/other")]
    assert dedupe(new, 0.88, TRACKING, existing=old) == []


def test_titles_similar_accents_and_case():
    assert titles_similar("Lancement réussi d'Ariane 6", "LANCEMENT REUSSI D'ARIANE 6", 0.9)
    assert not titles_similar("Ariane 6 launch", "Vega C launch failure investigation", 0.88)
