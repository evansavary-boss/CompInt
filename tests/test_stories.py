from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src.config import load_yaml
from src.models import Item
from src.stories import StoryBook, aggregate, headline, title_tokens

CFG = load_yaml("config/analysis.yaml")["stories"]
T0 = datetime(2026, 10, 22, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))


def item(iid, title, hours=0, entities=(), events=(), via="gnews", source="Media"):
    return Item(id=iid, title=title, url=f"https://ex.com/{iid}", source=source, section="economie",
                published=(T0 + timedelta(hours=hours)).isoformat(), summary="",
                entities=list(entities), events=list(events), via=via)


def test_title_tokens_drop_stopwords_and_accents():
    assert title_tokens("Les lanceurs européens et la Défense") == {"lanceur", "europeen", "defense"}


def test_paraphrases_are_grouped():
    items = [
        item("a", "ICEYE and Nokia sign letter of intent with Ukraine ministry", 0, ["iceye"]),
        item("b", "ICEYE, Nokia sign LoI with Ukraine on sovereign satellite communications", 3, ["iceye"]),
        item("c", "Nokia and ICEYE to help Ukraine develop sovereign satellite comms", 20, ["iceye"]),
    ]
    book = StoryBook()
    assert book.assign(items, CFG) == 1
    assert sorted(next(iter(book.stories.values())).item_ids) == ["a", "b", "c"]


def test_shared_entity_and_event_allow_looser_titles():
    items = [
        item("a", "Rocket Lab wins $500M SDA contract", 0, ["rocket-lab", "sda"], ["contract_won"]),
        item("b", "SDA awards Rocket Lab $500 million contract for Tranche 3", 5, ["rocket-lab", "sda"],
             ["contract_won"]),
    ]
    book = StoryBook()
    book.assign(items, CFG)
    assert len(book.stories) == 1


def test_different_figures_are_different_news():
    items = [
        item("a", "SpaceX launches 28 Starlink satellites from Florida", 0, ["spacex"], ["launch"]),
        item("b", "SpaceX launches 24 Starlink satellites from Florida", 20, ["spacex"], ["launch"]),
    ]
    book = StoryBook()
    book.assign(items, CFG)
    assert len(book.stories) == 2


def test_time_window_and_unrelated_titles():
    items = [
        item("a", "ICEYE opens office in Japan", 0, ["iceye"]),
        item("b", "ICEYE opens office in Japan", 24 * 5, ["iceye"]),     # same words, a week later
        item("c", "ESA selects new astronaut class", 1),
    ]
    book = StoryBook()
    book.assign(items, CFG)
    assert len(book.stories) == 3


def test_ids_are_stable_and_items_follow_history():
    first = [item("a", "Eutelsat raises capital with French state", 0, ["eutelsat"])]
    book = StoryBook()
    book.assign(first, CFG)
    sid = next(iter(book.stories))
    assert sid == "sa"
    later = first + [item("b", "French state backs Eutelsat capital raise", 6, ["eutelsat"])]
    book.assign(later, CFG)
    assert list(book.stories) == ["sa"] and book.stories["sa"].item_ids == ["a", "b"]
    book.assign([later[1]], CFG)            # "a" left the 90-day history
    assert book.stories["sa"].item_ids == ["b"]


def test_merge_and_save_roundtrip(tmp_path):
    items = [item("a", "Satellogic signs deal in Argentina", 0), item("b", "Totally different news", 1)]
    book = StoryBook()
    book.assign(items, CFG)
    assert book.merge("sb", "sa")
    assert book.stories["sa"].item_ids == ["a", "b"]
    book.stories["sa"].analysis = {"title_fr": "Titre"}
    book.save(tmp_path / "stories.json")
    again = StoryBook.load(tmp_path / "stories.json")
    assert again.stories["sa"].analysis == {"title_fr": "Titre"}


def test_headline_prefers_publisher_feed_and_aggregates():
    items = [
        item("a", "Via Google News", 0, ["iceye"], source="Reuters"),
        item("b", "From the publisher feed", 2, ["planet"], via="", source="SpaceNews"),
    ]
    book = StoryBook()
    book.stories["s"] = __import__("src.stories", fromlist=["Story"]).Story(id="s", item_ids=["a", "b"])
    view = aggregate(book.stories["s"], {i.id: i for i in items})
    assert headline(view["items"]).id == "b"
    assert view["sources"] == ["Reuters", "SpaceNews"]
    assert view["entities"] == ["iceye", "planet"]
