import copy
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import market, tenders
from src.config import load_settings, load_yaml
from src.enrich import AnalysisError, BatchResult
from src.entities import load_reference

TCFG = load_yaml("config/tenders.yaml")
ACFG = load_yaml("config/analysis.yaml")
COMPETITORS = load_yaml("config/competitors.yaml")
NOW = datetime(2026, 10, 27, 8, 0, tzinfo=ZoneInfo("Europe/Paris"))


@pytest.fixture(scope="module")
def ref():
    return load_reference(load_settings())


@pytest.fixture(scope="module")
def rules(ref):
    return tenders.Rules(TCFG, ref, COMPETITORS)


def ted_notice(**kw):
    n = {
        "publication-number": "618506-2026",
        "notice-type": "cn-standard",
        "notice-title": {
            "eng": "Spain – Digital mapping services – Framework contract for the supply of geospatial products",
            "fra": "Espagne – Services de cartographie numérique – Framework contract for the supply of geospatial products",
        },
        "buyer-name": {"eng": ["European Union Satellite Centre"]},
        "buyer-country": ["ESP"],
        "place-of-performance": ["ESP", "ESP"],
        "publication-date": "2026-09-25+02:00",
        "classification-cpv": ["71354100", "71354100"],
        "deadline-receipt-tender-date-lot": ["2026-10-29+01:00", "2026-11-05+01:00"],
        "estimated-value-proc": 6100000,
        "estimated-value-cur-proc": "EUR",
        "description-proc": {"eng": "Satellite imagery and geospatial products for the Copernicus Security Service."},
    }
    n.update(kw)
    return n


def test_parse_ted_contract_notice(rules):
    t = tenders.parse_ted(ted_notice(), TCFG["ted"], rules, 600)
    assert t.id == "ted:618506-2026" and t.kind == "open" and t.source == "ted"
    assert t.title == "Framework contract for the supply of geospatial products"   # no country / CPV prefix
    assert t.cpv_label == "Services de cartographie numérique"
    assert t.buyer == "European Union Satellite Centre"
    assert t.buyer_country == "ES" and t.countries == ["ES"] and t.cpv == ["71354100"]
    assert t.deadline == "2026-11-05" and t.published == "2026-09-25"
    assert t.value == 6100000 and t.currency == "EUR"
    assert t.url == "https://ted.europa.eu/fr/notice/-/detail/618506-2026"


def test_parse_ted_award_and_ignored_types(rules):
    award = ted_notice(**{"notice-type": "can-standard", "winner-name": {"pol": ["ICEYE Polska", "ICEYE Polska"]},
                          "total-value": [120000, 80000], "total-value-cur": ["PLN"]})
    t = tenders.parse_ted(award, TCFG["ted"], rules, 600)
    assert t.kind == "award" and t.winners == ["ICEYE Polska"] and t.value == 200000 and t.currency == "PLN"
    assert tenders.parse_ted(ted_notice(**{"notice-type": "can-modif"}), TCFG["ted"], rules, 600) is None


def fts_release(**kw):
    r = {
        "ocid": "ocds-h6vhtk-0001", "id": "101270-2026", "tag": ["tender"], "date": "2026-10-26T19:06:47Z",
        "buyer": {"name": "UK Hydrographic Office"},
        "parties": [{"roles": ["buyer"], "address": {"country": "GB"}}],
        "tender": {
            "title": "Satellite derived bathymetry services", "description": "Earth observation data for charts.",
            "value": {"amountGross": 600000, "amount": 500000, "currency": "GBP"},
            "tenderPeriod": {"endDate": "2026-11-25T12:00:00Z"},
            "documents": [{"id": "101270-2026", "noticeType": "UK4", "datePublished": "2026-10-26T19:06:47Z",
                           "url": "https://www.find-tender.service.gov.uk/Notice/101270-2026"}],
        },
    }
    r.update(kw)
    return r


def test_parse_fts_tender_award_and_filter(rules):
    cfg = TCFG["fts"]
    [t] = tenders.parse_fts(fts_release(), cfg, rules, 600)
    assert t.id == "fts:ocds-h6vhtk-0001:open" and t.kind == "open" and t.buyer_country == "GB"
    assert t.deadline == "2026-11-25" and t.value == 500000 and t.currency == "GBP"
    assert t.url.endswith("/Notice/101270-2026") and t.notice == "101270-2026"

    off_topic = fts_release(tender={"title": "School catering", "description": "Meals"})
    assert tenders.parse_fts(off_topic, cfg, rules, 600) == []

    award = fts_release(tag=["award", "contract"], awards=[
        {"value": {"amount": 100000, "currency": "GBP"}, "suppliers": [{"name": "Airbus Defence and Space Ltd"}]},
        {"value": {"amount": 50000, "currency": "GBP"}, "suppliers": [{"name": "Planet Labs PBC"}]}])
    [a] = tenders.parse_fts(award, cfg, rules, 600)
    assert a.kind == "award" and a.value == 150000 and a.deadline == ""
    assert a.winners == ["Airbus Defence and Space Ltd", "Planet Labs PBC"]

    cancel = fts_release(tag=["tenderCancellation"], tender={"title": "Anything"})
    [c] = tenders.parse_fts(cancel, cfg, rules, 600)
    assert c.status == "cancelled" and c.id == "fts:ocds-h6vhtk-0001:open"


def tender(tid="ted:1", **kw):
    base = dict(id=tid, source="ted", kind="open", notice="1", notice_type="cn-standard",
                title="Satellite imagery for forest monitoring", buyer="Ministry", buyer_country="FR",
                published="2026-10-20", url="https://ted.europa.eu/x", countries=["FR"], deadline="2026-11-10")
    base.update(kw)
    return tenders.Tender(**base)


def test_rules_relevance_segment_and_winners(rules):
    assert rules.is_strong(tender()) and rules.visible(tender())
    weak = tender(title="Modernisation of the land register", description="")
    assert not rules.is_strong(weak) and not rules.visible(weak)
    assert rules.visible(tender(title="Modernisation of the land register", analysis={"relevant": True}))
    assert not rules.visible(tender(analysis={"relevant": False}))
    assert rules.excluded(tender(title="Dostawa narzędzi", buyer="Copernicus PL Sp. z o.o."))
    assert rules.segment(tender()) == "imagerie_satellite"
    assert rules.segment(tender(title="Orthophoto and LiDAR survey")) == "aerien_lidar"
    assert rules.segment(tender(title="Road works study")) == TCFG["default_segment"]
    rows = rules.winner_rows(tender(winners=["Airbus DS Geo SA", "ICEYE Oy", "GeoSurvey SRL"]))
    assert [(r["id"], r["role"]) for r in rows] == [("airbus-ds", "own"), ("iceye", "rival"), ("", "")]


def test_book_upsert_keeps_analysis_and_prunes():
    book = tenders.TenderBook()
    assert book.upsert(tender(), NOW)
    book.tenders["ted:1"].analysis = {"relevant": True}
    assert not book.upsert(tender(title="Updated"), NOW + timedelta(days=1))
    t = book.tenders["ted:1"]
    assert t.title == "Updated" and t.analysis == {"relevant": True} and t.first_seen == NOW.isoformat()
    book.upsert(tender("ted:old", published="2025-01-01"), NOW)
    assert book.prune(date(2026, 10, 27), 365) == 1 and "ted:old" not in book.tenders


def test_ecb_rates_and_conversion():
    xml = ("<?xml version='1.0'?><gesmes:Envelope xmlns:gesmes='http://www.gesmes.org/xml/2002-08-01' "
           "xmlns='http://www.ecb.int/vocabulary/2002-08-01/eurofxref'><Cube><Cube time='2026-10-26'>"
           "<Cube currency='GBP' rate='0.8'/><Cube currency='PLN' rate='4'/></Cube></Cube></gesmes:Envelope>")
    fx = tenders.parse_ecb(xml)
    assert fx["date"] == "2026-10-26" and fx["rates"]["GBP"] == 0.8
    assert tenders.to_eur(800, "GBP", fx) == 1000 and tenders.to_eur(10, "EUR", fx) == 10
    assert tenders.to_eur(10, "XYZ", fx) is None and tenders.to_eur(None, "EUR", fx) is None


class FakeAnalyzer:
    name = "fake"

    def __init__(self, error=None):
        self.error, self.calls = error, []

    def available(self):
        return True, ""

    def call(self, payload, prompt, schema, instruction, key, timeout=None):
        self.calls.append(payload)
        if self.error:
            raise AnalysisError(self.error)
        import json
        ids = [a["id"] for a in json.loads(payload)["avis"]]
        return BatchResult([{"id": i, "relevant": i != "ted:2", "segment": "imagerie_satellite",
                             "title_fr": "Images satellites", "summary": "Achat.", "why": "", "industries": ["defense", "zzz"],
                             "fit": "forte"} for i in ids] + [{"id": "unknown"}], 0.01)


def test_analyse_tenders_stores_validated_answers(ref, rules):
    book = tenders.TenderBook()
    for t in (tender("ted:1"), tender("ted:2", title="Cadastral survey"), tender("ted:3", published="2025-01-01")):
        book.upsert(t, NOW)
    fake = FakeAnalyzer()
    report = tenders.analyse_tenders(book, rules, TCFG, ACFG, ref, NOW, analyzer=fake)
    assert report["analysed"] == 2 and report["relevant"] == 1 and len(fake.calls) == 1
    a = book.tenders["ted:1"].analysis
    assert a["fit"] == "forte" and a["industries"] == ["defense"]
    assert not book.tenders["ted:2"].analysis["relevant"]
    assert not book.tenders["ted:3"].analysis          # older than max_age_days
    assert '"ted:1"' in fake.calls[0].split("ted:2")[0]   # clearly geospatial notices first

    stopped = tenders.analyse_tenders(tenders.TenderBook({"ted:9": tender("ted:9")}), rules, TCFG, ACFG, ref, NOW,
                                      analyzer=FakeAnalyzer(error="usage limit"))
    assert stopped["analysed"] == 0 and "usage limit" in stopped["error"]


def test_schema_and_prompt_follow_config():
    schema = tenders.build_schema(TCFG, ACFG)
    item = schema["properties"]["tenders"]["items"]
    assert item["properties"]["segment"]["enum"] == list(TCFG["segments"])
    assert item["properties"]["fit"]["enum"] == list(TCFG["analysis"]["fit"])
    prompt = tenders.build_system_prompt(TCFG, ACFG)
    assert "## Segments" in prompt and "imagerie_satellite" in prompt and "`forte`" in prompt


def test_fts_retries_once_after_rate_limit(monkeypatch, rules):
    calls = []

    class Resp:
        def __init__(self, code):
            self.status_code, self.text = code, "Rate limit of 12 exceeded"

        def json(self):
            return {"releases": [fts_release()], "links": {}}

    def fake_get(*a, **k):
        calls.append(1)
        return Resp(429 if len(calls) == 1 else 200)

    monkeypatch.setattr(tenders.requests, "get", fake_get)
    monkeypatch.setattr(tenders.time, "sleep", lambda s: None)
    start = datetime(2026, 10, 26)
    kept, scanned, synced, error = tenders.fetch_fts(TCFG["fts"], load_settings(), rules, start,
                                                     start + timedelta(hours=12), 600)
    assert len(calls) == 2 and scanned == 1 and kept[0].kind == "open"
    assert synced == start + timedelta(hours=12) and error == ""


def test_fts_failure_keeps_the_days_already_read(monkeypatch, rules):
    calls = []

    class Resp:
        status_code, text = 200, ""

        def json(self):
            return {"releases": [fts_release()], "links": {}}

    def fake_get(*a, **k):
        calls.append(1)
        if len(calls) == 2:
            raise tenders.requests.ConnectionError("DNS failure")
        return Resp()

    monkeypatch.setattr(tenders.requests, "get", fake_get)
    monkeypatch.setattr(tenders.time, "sleep", lambda s: None)
    start = datetime(2026, 10, 20)
    kept, scanned, synced, error = tenders.fetch_fts(TCFG["fts"], load_settings(), rules, start,
                                                     start + timedelta(days=3), 600)
    assert scanned == 1 and len(kept) == 1
    assert synced == start + timedelta(days=1) and "DNS failure" in error


def test_config_is_valid(ref):
    assert tenders.validate_config(TCFG, COMPETITORS, ref, ACFG) == []
    bad = copy.deepcopy(COMPETITORS)
    bad["groups"]["radar"]["entities"].append("not-a-company")
    bad["moves"]["contrats"]["events"].append("not-an-event")
    errors = tenders.validate_config(TCFG, bad, ref, ACFG)
    assert any("not-a-company" in e for e in errors) and any("not-an-event" in e for e in errors)


def test_tender_rows_dedupe_status_and_amounts(tmp_path, ref):
    settings = copy.deepcopy(load_settings())
    settings["paths"]["tender_data"] = str(tmp_path / "tenders.json")
    book = tenders.TenderBook(meta={"fx": {"date": "2026-10-26", "rates": {"PLN": 4.0}}})
    book.upsert(tender("ted:1", published="2026-10-01"), NOW)
    book.upsert(tender("ted:2", published="2026-10-15", value=4_000_000, currency="PLN"), NOW)   # republished
    book.upsert(tender("ted:3", deadline="2026-10-01", title="Satellite data archive"), NOW)       # closed
    book.upsert(tender("ted:4", kind="award", winners=["ICEYE Oy"], deadline=""), NOW)
    book.upsert(tender("ted:5", title="Land register update"), NOW)                                 # not shown
    book.save(tmp_path / "tenders.json")
    entities = {k: {**v, "id": k} for k, v in ref["entities"].items()}
    months = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
    rows, meta = market.tender_rows(settings, ref, entities, date(2026, 10, 27), months)
    by_id = {r["id"]: r for r in rows}
    assert set(by_id) == {"ted:2", "ted:3", "ted:4"}
    assert by_id["ted:2"]["open"] and by_id["ted:2"]["value"] == 1_000_000 and by_id["ted:2"]["value_label"] == "1 M€"
    assert by_id["ted:2"]["days_left"] == 14 and by_id["ted:2"]["deadline_label"] == "10 nov. 2026"
    assert not by_id["ted:3"]["open"]
    assert by_id["ted:4"]["winners"][0]["role"] == "rival"
    payload = market.tender_payload(rows, meta, ref, ACFG, entities, date(2026, 10, 27), months)
    assert [m["key"] for m in payload["months"]][-2:] == ["2026-09", "2026-10"] and len(payload["months"]) == 6
    assert payload["names"]["iceye"] == "ICEYE" and payload["countries"] == [{"key": "FR", "label": "France"}]


def test_fmt_eur():
    assert market.fmt_eur(850_000) == "850 k€"
    assert market.fmt_eur(1_250_000) == "1,2 M€"
    assert market.fmt_eur(12_400_000) == "12 M€"
    assert market.fmt_eur(3_000_000_000) == "3 Md€"
    assert market.fmt_eur(None) == ""
