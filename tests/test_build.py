import copy
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import build, tenders
from src.classify import Classifier
from src.config import load_settings, load_yaml
from src.entities import load_reference
from src.models import Item, SourceStatus
from src.store import save_items

NOW = datetime(2026, 10, 23, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("site")
    settings = copy.deepcopy(load_settings())
    settings["paths"].update(items=str(tmp / "items.json"), entities=str(tmp / "entities.json"),
                             tender_data=str(tmp / "tenders.json"), output_dir=str(tmp / "docs"))
    clf = Classifier(load_reference(settings), load_yaml(settings["paths"]["geopolitics"]))

    def item(n, title, section, days_ago, tags=("newspace",), via=""):
        return clf.classify(Item(id=f"i{n}", title=title, url=f"https://example.com/{n}", source="Test",
                                 section=section, summary="",
                                 published=(NOW - timedelta(days=days_ago)).isoformat(),
                                 tags=list(tags), via=via))

    items = [
        item(1, "ICEYE wins contract with the Portuguese navy", "economie", 0, ("geospatial",)),
        item(2, "ESA Council names new directors", "politique", 2),
        item(3, "Old <script> story about CNES", "politique", 200),   # outside retention
        item(4, "Sudan war: RSF advance near Khartoum", "geopolitique", 1),
    ]
    save_items(tmp / "items.json", items, [SourceStatus("Feed", "u", True)], NOW)
    book = tenders.TenderBook(meta={"updated_at": NOW.isoformat(), "fx": {"date": "2026-10-22", "rates": {}},
                                    "sources": [SourceStatus("TED", "u", True, via="tender").to_dict()]})
    common = dict(source="ted", notice="1", notice_type="cn-standard", buyer="Ministère des Armées",
                  buyer_country="FR", url="https://ted.europa.eu/fr/notice/-/detail/1-2026", countries=["FR"])
    book.upsert(tenders.Tender(id="ted:1", kind="open", title="Satellite imagery <b>services</b>",
                               published="2026-10-20", deadline="2026-10-28", value=2_500_000, currency="EUR",
                               **common), NOW)
    book.upsert(tenders.Tender(id="ted:2", kind="award", title="Earth observation data for maritime surveillance",
                               published="2026-10-10", winners=["ICEYE Oy"], **common), NOW)
    book.save(tmp / "tenders.json")
    build.render(settings)
    return tmp / "docs"


def test_pages_exist(site):
    for name in ("index.html", "economie.html", "marche.html", "entite/iceye.html", "entite/esa.html", "static/app.js"):
        assert (site / name).exists(), name


def test_items_land_in_their_tab_with_filter_data(site):
    eco = (site / "economie.html").read_text(encoding="utf-8")
    pol = (site / "index.html").read_text(encoding="utf-8")
    assert "ICEYE wins contract" in eco and "ICEYE wins contract" not in pol
    assert "ESA Council names new directors" in eco          # institutions now live in Économie
    assert "Sudan war: RSF advance near Khartoum" in pol      # Géopolitique is the home page
    assert 'data-themes="conflits"' in pol
    assert 'class="worldmap"' in pol and 'data-country="SD"' in pol
    assert 'data-entities="iceye"' in eco
    assert 'data-events="contract_won"' in eco
    assert 'data-tags="geospatial"' in eco
    assert "Old &lt;script&gt;" not in pol and "Old <script>" not in pol   # pruned


def test_entity_page_lists_its_news_and_profile(site):
    page = (site / "entite" / "iceye.html").read_text(encoding="utf-8")
    assert "<h1>ICEYE</h1>" in page
    assert "ICEYE wins contract" in page
    assert "Observation radar (SAR)" in page
    assert 'href="../static/style.css' in page          # relative paths from the subfolder
    assert "None" not in page


def test_sidebar_counts(site):
    eco = (site / "economie.html").read_text(encoding="utf-8")
    assert 'data-field="entities" data-key="iceye">1<' in eco
    assert 'data-field="entities" data-key="planet">0<' in eco
    pol = (site / "index.html").read_text(encoding="utf-8")
    assert 'data-field="countries" data-key="SD">1<' in pol


def test_country_pages(site):
    page = (site / "pays" / "SD.html").read_text(encoding="utf-8")
    assert "<h1>Soudan</h1>" in page and "Sudan war" in page
    assert not (site / "pays" / "ZW.html").exists()           # only countries cited by a story


def test_market_page_embeds_business_records(site):
    import json
    import re

    page = (site / "marche.html").read_text(encoding="utf-8")
    raw = re.search(r'<script type="application/json" id="market-data">(.*?)</script>', page, re.S).group(1)
    data = json.loads(raw)
    titles = [r["ti"] for r in data["records"]]
    assert "ICEYE wins contract with the Portuguese navy" in titles      # economie item
    assert "ESA Council names new directors" not in titles               # politique only
    rec = data["records"][0]
    assert rec["e"] == ["contract_won"] and rec["c"] == ["iceye"] and "europe" in rec["r"]
    assert [s["key"] for s in data["series"]] == load_settings()["market"]["series"]
    assert data["complete_days"] >= 1 and data["coverage_days"] >= data["complete_days"]
    assert 'static/market.js' in page


def test_marche_page_embeds_tenders_and_press_view(site):
    page = (site / "marche.html").read_text(encoding="utf-8")
    assert 'id="tender-data"' in page and 'data-view-panel="presse"' in page
    assert r"Satellite imagery \u003cb\u003eservices" in page          # escaped inside the JSON block
    assert "<b>services" not in page
    assert '"o": true' in page


def test_tenders_on_country_and_entity_pages(site):
    fr = (site / "pays" / "FR.html").read_text(encoding="utf-8")
    assert "Appels d&#39;offres publics" in fr and "Satellite imagery &lt;b&gt;services&lt;/b&gt;" in fr
    assert "marche.html#pays=FR" in fr and "J-5" in fr and "2,5 M€" in fr
    iceye = (site / "entite" / "iceye.html").read_text(encoding="utf-8")
    assert "Appels d&#39;offres remportés" in iceye and "Earth observation data for maritime surveillance" in iceye


def test_concurrents_page_matrix_cards_and_feed(site):
    page = (site / "concurrents.html").read_text(encoding="utf-8")
    assert 'href="concurrents.html" aria-current="page"' in page
    assert "Matrice des mouvements" in page and "Fiches de combat" in page
    # ICEYE: one contract story and one tender won (TED award to "ICEYE Oy")
    assert 'href="#entities=iceye&amp;moves=contrats&amp;days=30"' in page
    assert 'href="entite/iceye.html#appels-offres"' in page
    assert "ICEYE wins contract" in page and 'data-moves="contrats"' in page
    assert 'data-move="contrats"' in page                   # move chips in the feed filters
    assert "ESA Council" not in page                         # not a competitor story
    assert "Optique américaine" in page and "Asie et Moyen-Orient" in page


def test_competitor_profile_has_battle_card(site):
    page = (site / "entite" / "iceye.html").read_text(encoding="utf-8")
    assert "Fiche de combat" in page and "Concurrent · Radar (SAR)" in page
    assert 'id="appels-offres"' in page
    assert "Fiche de combat" not in (site / "entite" / "esa.html").read_text(encoding="utf-8")
