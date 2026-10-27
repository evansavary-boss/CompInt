"""Build the static site in docs/.

Pages: index.html (Géopolitique), economie.html, marche.html, concurrents.html, one page per
entity in entite/<id>.html and one per country in pays/<code>.html.

Usage:
    python -m src.build                  # refresh entities, collect news, render docs/
    python -m src.build --skip-collect   # render from the existing data/*.json
    python -m src.build --serve          # build, then serve docs/ on http://localhost:8000
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import http.server
import json
import logging
import shutil
import sys
from collections import Counter
from datetime import datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import collect, entities as entities_mod, market, tenders as tenders_mod, worldmap
from .config import load_settings, load_yaml, resolve
from .filter import normalize_text
from .models import Item
from .store import load_items
from .stories import StoryBook, aggregate

log = logging.getLogger("build")

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
          "juil.", "août", "sept.", "oct.", "nov.", "déc."]
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

# Feed tabs: key, file, title, subtitle, sidebar kinds ("country", "competitor" or entity kinds).
GEO = "geopolitique"
COMP = "concurrents"
TABS = [
    (GEO, "index.html", "Géopolitique",
     "Conflits, sanctions, ressources, crises : le contexte mondial du géospatial et de la défense",
     ("country",)),
    ("economie", "economie.html", "Économie",
     "Institutions, budgets, contrats, lancements, financements et entreprises", ("company", "institution")),
    (COMP, "concurrents.html", "Concurrents",
     "Les mouvements des concurrents directs de l'activité géospatiale d'Airbus", ("competitor",)),
]
MARKET_PAGE = ("marche", "marche.html", "Marché",
               "Appels d'offres publics (TED, Find a Tender) et signaux du marché dans la presse", None)

FACT_LABELS = [
    ("founded", "Création"),
    ("headquarters", "Siège"),
    ("ceo", "Direction générale"),
    ("director", "Direction"),
    ("parent", "Maison mère"),
    ("employees", "Effectif"),
]


# ---------------------------------------------------------------- formatting


def day_label(day: datetime, today: datetime) -> str:
    delta = (today.date() - day.date()).days
    if delta == 0:
        return "Aujourd'hui"
    if delta == 1:
        return "Hier"
    label = f"{WEEKDAYS[day.weekday()].capitalize()} {day.day} {MONTHS[day.month - 1]}"
    return label if day.year == today.year else f"{label} {day.year}"


def format_datetime(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year} à {dt:%H:%M}"


def format_date(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year}"


def fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def site_domain(url: str) -> str:
    host = urlsplit(url or "").netloc
    return host[4:] if host.startswith("www.") else host


def on_page(row: dict[str, Any], key: str) -> bool:
    """Which tab lists a story: Claude's tabs when analysed, else the rule-based sections."""
    tabs = row["analysis_tabs"]
    if tabs:
        return (GEO in tabs) if key == GEO else bool(set(tabs) - {GEO})
    sections = set(row["sections"])
    return (GEO in sections) if key == GEO else bool(sections & {"politique", "economie"})


# ---------------------------------------------------------------- view models


class SiteData:
    """Everything the templates need, computed once."""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        paths = settings["paths"]
        tz = ZoneInfo(settings["timezone"])
        self.ref = entities_mod.load_reference(settings)
        cache = entities_mod.load_cache(resolve(paths["entities"]))
        self.entities = entities_mod.merged_entities(self.ref, cache)
        self.facts_date = cache.get("updated_at")

        items, meta = load_items(resolve(paths["items"]))
        self.updated_at = (datetime.fromisoformat(meta["updated_at"]) if meta.get("updated_at")
                           else datetime.now(tz))
        self.sources = meta.get("sources", [])
        cutoff = self.updated_at - timedelta(days=settings["history"]["retention_days"])
        self.items = [i for i in items if datetime.fromisoformat(i.published) >= cutoff]

        self.event_labels = {k: v["label"] for k, v in self.ref["events"].items()}
        self.country_names = {k: v["name"] for k, v in self.ref["countries"].items()}
        self.region_names = self.ref["regions"]
        self.segment_names = self.ref["segments"]
        self.type_names = self.ref["types"]

        self.rows = {i.id: self._row(i) for i in self.items}
        # Daily collection starts on the first run; before it, the history is a capped backfill.
        first_run = min((datetime.fromisoformat(i.first_seen) for i in self.items if i.first_seen),
                        default=self.updated_at)
        self.complete_days = (self.updated_at.date() - first_run.date()).days + 1
        self.complete_since = format_date(first_run)

        self.competitors = load_yaml(paths["competitors"])
        self.moves = self.competitors["moves"]
        self.groups = self.competitors["groups"]
        self.own_ids = [e for e in self.competitors.get("own") or [] if e in self.entities]
        self.rival_group = {eid: key for key, g in self.groups.items() for eid in g.get("entities") or []
                            if eid in self.entities}

        self.analysis = load_yaml(paths["analysis"])
        impact_keys = list(self.analysis["impact"])
        self.impact_rank = {k: len(impact_keys) - n for n, k in enumerate(impact_keys)}   # fort = 3
        self.theme_names = {k: v["label"] for k, v in self.analysis["themes"].items()}
        self.industry_names = self.analysis["industries"]
        self.stories = self._load_stories()
        self.tenders, self.tender_meta = market.tender_rows(settings, self.ref, self.entities,
                                                            self.updated_at.date(), MONTHS)

    # ------------------------------------------------------------ items (Marché page)

    def _row(self, item: Item) -> dict[str, Any]:
        published = datetime.fromisoformat(item.published)
        ents = [self.entities[e] for e in item.entities if e in self.entities]
        return {
            "item": item,
            "date": format_date(published),
            "age": (self.updated_at.date() - published.date()).days,
            "segments": sorted({s for e in ents for s in e.get("segments") or []}),
        }

    # ------------------------------------------------------------ stories

    def _load_stories(self) -> list[dict[str, Any]]:
        """Story rows, hiding the ones Claude judged off-topic."""
        book = StoryBook.load(resolve(self.settings["paths"]["stories"]))
        by_id = {i.id: i for i in self.items}
        if set(by_id) - set(book.story_of()):
            book.assign(self.items, self.analysis["stories"])   # in memory only: never lose an item
        rows = []
        for story in book.stories.values():
            if not any(iid in by_id for iid in story.item_ids):
                continue
            if story.analysis and not story.analysis.get("relevant", True):
                continue
            rows.append(self._story_row(aggregate(story, by_id)))
        rows.sort(key=lambda r: (-r["last"].date().toordinal(), -r["impact_rank"], -r["n_sources"],
                                 -r["last"].timestamp()))
        return rows

    def _story_row(self, view: dict[str, Any]) -> dict[str, Any]:
        story, head = view["story"], view["headline"]
        a = story.analysis
        last = datetime.fromisoformat(view["last_published"])
        ents = [self.entities[e] for e in view["entities"] if e in self.entities]
        countries = list(view["countries"]) + [c for c in a.get("countries", [])
                                                if c not in view["countries"] and c in self.country_names]
        regions = list(view["regions"])
        for c in countries:
            region = self.ref["countries"][c]["region"]
            if region not in regions:
                regions.append(region)
        impact = a.get("impact", "")
        theme_keys = a.get("themes") or view["geo_themes"]
        moves = [k for k, m in self.moves.items() if not m.get("tenders")
                 and (set(m.get("events") or []) & set(view["events"]) or set(m.get("themes") or []) & set(theme_keys))]
        themes = [(k, self.theme_names[k]) for k in theme_keys if k in self.theme_names]
        industries = [(k, self.industry_names[k]) for k in a.get("industries", []) if k in self.industry_names]
        limit = self.analysis["stories"]["max_sources_listed"]
        listed = []
        for it in sorted(view["items"], key=lambda i: i.published, reverse=True)[:limit]:
            when = datetime.fromisoformat(it.published)
            listed.append({"title": it.title, "url": it.url, "source": it.source, "via": it.via,
                           "date": f"{when.day} {MONTHS[when.month - 1]} · {when:%H:%M}"})
        search = " ".join([a.get("title_fr", ""), a.get("summary", ""), a.get("why", "")]
                          + [it.title for it in view["items"]] + view["sources"]
                          + [e["name"] for e in ents]
                          + [self.country_names.get(c, "") for c in countries]
                          + [label for _, label in themes + industries])
        return {
            "id": story.id,
            "title": a.get("title_fr") or head.title,
            "url": head.url,
            "source": head.source,
            "via": head.via,
            "summary": a.get("summary") or head.summary,
            "why": a.get("why", ""),
            "analysed": bool(a),
            "impact": impact,
            "impact_label": self.analysis["impact"].get(impact, {}).get("label", ""),
            "impact_rank": self.impact_rank.get(impact, 0),
            "themes": themes,
            "industries": industries,
            "tags": view["tags"],
            "entity_ids": view["entities"],
            "entities": [{"id": e["id"], "name": e["name"], "kind": e["kind"]} for e in ents],
            "countries": countries,
            "country_names": [self.country_names.get(c, c) for c in countries],
            "regions": regions,
            "event_keys": view["events"],
            "events": [{"key": k, "label": self.event_labels.get(k, k)} for k in view["events"]],
            "segments": sorted({s for e in ents for s in e.get("segments") or []}),
            "sections": view["sections"],
            "analysis_tabs": a.get("tabs") or [],
            "theme_keys": [k for k, _ in themes],
            "move_keys": moves,
            "n_items": len(view["items"]),
            "n_sources": len(view["sources"]),
            "sources": listed,
            "more_sources": max(0, len(view["items"]) - limit),
            "last": last,
            "time": f"{last:%H:%M}",
            "date": format_date(last),
            "age": (self.updated_at.date() - last.date()).days,
            "search": normalize_text(search),
        }

    def group_by_day(self, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        groups = []
        for day, day_rows in groupby(rows, key=lambda r: r["last"].date()):
            day_rows = list(day_rows)
            groups.append({
                "label": day_label(day_rows[0]["last"], self.updated_at),
                "age": (self.updated_at.date() - day).days,
                "rows": day_rows,
            })
        return groups

    # ------------------------------------------------------------ filter options

    def options(self, rows: list[dict[str, Any]], kind: str | None, tab: str | None) -> dict[str, Any]:
        """Filter choices present in `rows`, most frequent first."""
        countries = Counter(c for r in rows for c in r["countries"])
        regions = Counter(g for r in rows for g in r["regions"])
        ents = Counter(e for r in rows for e in r["entity_ids"] if e in self.entities and (
            kind is None or (e in self.rival_group if kind == "competitor" else self.entities[e]["kind"] == kind)))
        events = Counter(e for r in rows for e in r["event_keys"])
        segments = Counter(s for r in rows for s in r["segments"])
        industries = Counter(k for r in rows for k, _ in r["industries"])
        themes = Counter(k for r in rows for k in r["theme_keys"])
        # Business/policy event chips everywhere except Géopolitique (families) and Concurrents (moves).
        event_keys = [k for k in self.ref["events"] if k in events and tab not in (GEO, COMP)]
        moves = Counter(k for r in rows for k in r["move_keys"])
        theme_tabs = {GEO} if tab == GEO else ({"economie", "marche", "concurrents"} if tab else None)
        return {
            "regions": [(k, self.region_names[k]) for k in self.region_names if k in regions],
            "countries": [(k, self.country_names.get(k, k), n) for k, n in countries.most_common()],
            "entities": sorted(((k, self.entities[k]["name"], n) for k, n in ents.items()),
                               key=lambda t: (-t[2], t[1].lower())),
            "segments": [(k, v) for k, v in self.segment_names.items() if k in segments],
            "industries": [(k, v, industries[k]) for k, v in self.industry_names.items() if k in industries],
            "events": [(k, self.event_labels[k], events[k]) for k in event_keys],
            "moves": [(k, m["label"], moves[k]) for k, m in self.moves.items() if moves[k]] if tab == COMP else [],
            "analysed": any(r["analysed"] for r in rows),
            "themes": [(k, v["label"], themes[k]) for k, v in self.analysis["themes"].items()
                       if (theme_tabs is None or v["tab"] in theme_tabs) and themes[k]],
        }

    def sidebar(self, kind: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """One sidebar box: entities of a kind grouped by segment/type, or countries by region,
        each with its story count (updated live by the filters)."""
        if kind == "competitor":
            counts = Counter(e for r in rows for e in r["entity_ids"])
            out = [{"label": g["label"], "entities": sorted(
                ({"id": eid, "name": self.entities[eid]["name"], "count": counts[eid], "href": f"entite/{eid}.html"}
                 for eid in g.get("entities") or [] if eid in self.entities),
                key=lambda r: (-r["count"], r["name"].lower()))} for g in self.groups.values()]
            return {"title": "Concurrents", "field": "entities", "groups": [g for g in out if g["entities"]]}
        if kind == "country":
            counts = Counter(c for r in rows for c in r["countries"])
            groups: dict[str, list] = {}
            for code, n in counts.items():
                c = self.ref["countries"].get(code)
                if c:
                    groups.setdefault(c["region"], []).append(
                        {"id": code, "name": c["name"], "count": n, "href": f"pays/{code}.html"})
            labels = self.region_names
            title, field = "Pays", "countries"
        else:
            counts = Counter(e for r in rows for e in r["entity_ids"])
            groups = {}
            for e in self.entities.values():
                if e["kind"] != kind:
                    continue
                key = (e.get("segments") or ["autre"])[0] if kind == "company" else e.get("type", "autre")
                groups.setdefault(key, []).append({"id": e["id"], "name": e["name"], "count": counts[e["id"]],
                                                   "href": f"entite/{e['id']}.html"})
            labels = self.segment_names if kind == "company" else self.type_names
            title, field = ("Entreprises" if kind == "company" else "Institutions"), "entities"
        out = []
        for key, label in labels.items():
            if key in groups:
                ranked = sorted(groups[key], key=lambda r: (-r["count"], r["name"].lower()))
                out.append({"label": label, "entities": ranked})
        return {"title": title, "field": field, "groups": out}

    # ------------------------------------------------------------ entity pages

    def entity_view(self, eid: str) -> dict[str, Any]:
        e = self.entities[eid]
        rows = [r for r in self.stories if eid in r["entity_ids"]]
        facts = e["facts"]
        fact_rows = []
        for key, label in FACT_LABELS:
            if key not in facts:
                continue
            if key == "director" and "ceo" in facts:
                continue   # one leader only: CEO first, else director
            value = facts[key]
            if key == "employees":
                value = fmt_int(value["value"]) + (f" ({value['year']})" if value.get("year") else "")
            fact_rows.append((label, str(value)))
        if e["kind"] == "company":
            places = ", ".join(self.country_names.get(c, c) for c in e.get("countries") or [])
            tags = [self.segment_names[s] for s in e.get("segments") or []]
        else:
            places = (", ".join(self.country_names.get(c, c) for c in e.get("countries") or [])
                      or self.region_names.get(e.get("region"), ""))
            tags = [self.type_names.get(e.get("type"), "")]
        events = Counter(k for r in rows for k in r["event_keys"])
        co = Counter(o for r in rows for o in r["entity_ids"] if o != eid and o in self.entities)
        if e["kind"] == "company":
            tenders = [t for t in self.tenders if t["kind"] == "award" and any(w["id"] == eid for w in t["winners"])]
        else:
            tenders = [t for t in self.tenders if t["buyer_id"] == eid]
        windows = {d: sum(1 for r in rows if r["age"] < d) for d in (7, 30, 90)}
        return {
            "entity": e,
            "places": places,
            "tags": tags,
            "facts": fact_rows,
            "website": facts.get("website"),
            "website_label": site_domain(facts.get("website", "")),
            "wikipedia": facts.get("wikipedia"),
            "logo": facts.get("logo"),
            "facts_overridden": [dict(FACT_LABELS).get(k, k).lower() for k in e.get("facts_overridden") or []
                                 if e["facts"].get(k) is not None or k in facts],
            "facts_fetched": (format_date(datetime.fromisoformat(e["facts_fetched_at"]))
                              if e.get("facts_fetched_at") else None),
            "events": [(k, self.event_labels[k], events[k]) for k in self.ref["events"] if events[k]],
            "events_max": max(events.values(), default=0),
            "related": [{"id": o, "name": self.entities[o]["name"], "kind": self.entities[o]["kind"], "count": n}
                        for o, n in co.most_common(10)],
            "windows": windows,
            "groups": self.group_by_day(rows),
            "count": len(rows),
            "options": self.options(rows, None, None),
            "tenders": self.tender_box(tenders),
            "competitor": self.competitor_card(eid) if eid in self.rival_group or eid in self.own_ids else None,
        }

    # ------------------------------------------------------------ competitors

    def competitor_rows(self) -> list[dict[str, Any]]:
        """Stories citing a tracked competitor (feed of the Concurrents tab)."""
        days = self.competitors["page"]["feed_days"]
        return [r for r in self.stories if r["age"] < days and set(r["entity_ids"]) & set(self.rival_group)]

    def _moves(self, eid: str, lo: int, hi: int) -> dict[str, Any]:
        """Moves of an entity between `lo` (included) and `hi` (excluded) days ago."""
        stories = [r for r in self.stories if eid in r["entity_ids"] and lo <= r["age"] < hi and r["move_keys"]]
        won = [t for t in self.tenders if t["kind"] == "award" and lo <= t["age"] < hi
               and any(w["id"] == eid for w in t["winners"])]
        counts = {k: (len(won) if m.get("tenders") else sum(1 for r in stories if k in r["move_keys"]))
                  for k, m in self.moves.items()}
        return {"counts": counts, "total": len(stories) + len(won), "stories": stories, "won": won}

    def competitor_card(self, eid: str) -> dict[str, Any]:
        """Battle card of a competitor (or of Airbus, the reference row)."""
        page = self.competitors["page"]
        e = self.entities[eid]
        days = page["card_days"]
        now, prev = self._moves(eid, 0, days), self._moves(eid, days, 2 * days)
        latest = sorted(now["stories"], key=lambda r: r["last"], reverse=True)[: page["card_moves"]]
        won_year = [t for t in self.tenders if t["kind"] == "award" and t["age"] < page["tender_days"]
                    and any(w["id"] == eid for w in t["winners"])]
        group = self.rival_group.get(eid)
        by_period = {p: self._moves(eid, 0, p) for p in page["periods"]}
        return {
            "id": eid,
            "name": e["name"],
            "own": eid in self.own_ids,
            "group": self.groups[group]["label"] if group else "Référence",
            "countries": ", ".join(self.country_names.get(c, c) for c in e.get("countries") or []),
            "segments": [self.segment_names[s] for s in e.get("segments") or [] if s in self.segment_names],
            "days": days,
            "total": now["total"],
            "delta": now["total"] - prev["total"],
            "comparable": self.complete_days >= 2 * days,
            "counts": [(k, m["label"], now["counts"][k]) for k, m in self.moves.items()],
            "periods": {p: m["counts"] for p, m in by_period.items()},
            "period_totals": {p: m["total"] for p, m in by_period.items()},
            "latest": [{"title": r["title"], "url": r["url"], "date": r["date"],
                        "moves": [self.moves[k]["label"] for k in r["move_keys"]]} for r in latest],
            "mentions": sum(1 for r in self.stories if eid in r["entity_ids"] and r["age"] < days),
            "tenders_won": len(won_year),
            "tender_days": page["tender_days"],
            "last_tender": won_year[0] if won_year else None,
        }

    def competitor_view(self) -> dict[str, Any]:
        """Matrix (competitors x move types, per period) and battle cards, grouped."""
        page = self.competitors["page"]
        periods = page["periods"]
        moves = [(k, m["label"]) for k, m in self.moves.items()]
        cards = {eid: self.competitor_card(eid) for eid in list(self.own_ids) + list(self.rival_group)}
        groups = []
        for key, g in self.groups.items():
            ids = [eid for eid in g.get("entities") or [] if eid in cards]
            groups.append({"key": key, "label": g["label"],
                           "cards": sorted((cards[i] for i in ids), key=lambda c: (-c["total"], c["name"].lower()))})
        matrix = {}
        for p in periods:
            values = [v for c in cards.values() if not c["own"] for v in c["periods"][p].values()]
            top = max(values, default=0) or 1
            matrix[p] = {"max": top, "bins": {eid: {k: (0 if not v else 1 + min(4, (5 * (v - 1)) // top))
                                                     for k, v in c["periods"][p].items()}
                                               for eid, c in cards.items()}}
        return {"moves": moves, "periods": periods, "groups": groups, "own": [cards[eid] for eid in self.own_ids],
                "matrix": matrix, "card_days": page["card_days"], "tender_days": page["tender_days"],
                "complete_days": self.complete_days, "complete_since": self.complete_since}

    # ------------------------------------------------------------ tenders

    def tender_box(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """Open tenders (closest deadline first) and latest awards, for a country or entity page."""
        disp = self.tender_meta["config"]["display"]
        opened = sorted((r for r in rows if r["open"]), key=lambda r: (r["deadline"] or "9999", r["published"]))
        awards = [r for r in rows if r["kind"] == "award"]
        return {"open": opened, "awards": awards[: disp["award_rows"] // 4], "awards_count": len(awards),
                "total": len(rows), "closing_soon": self.tender_meta["config"]["closing_soon_days"]}

    # ------------------------------------------------------------ country pages

    def country_codes(self) -> list[str]:
        """Countries cited by at least one story (one page each)."""
        cited = {c for r in self.stories for c in r["countries"]} | {t["country"] for t in self.tenders}
        return sorted(c for c in cited if c in self.ref["countries"])

    def country_view(self, code: str) -> dict[str, Any]:
        c = self.ref["countries"][code]
        rows = [r for r in self.stories if code in r["countries"]]
        geo_rows = [r for r in rows if on_page(r, GEO)]
        themes = Counter(k for r in rows for k in r["theme_keys"])
        local = [e for e in self.entities.values() if code in (e.get("countries") or [])]
        cited = Counter(e for r in rows for e in r["entity_ids"] if e in self.entities)
        return {
            "code": code,
            "country": c,
            "region": self.region_names.get(c["region"], ""),
            "windows": {d: sum(1 for r in rows if r["age"] < d) for d in (7, 30, 90)},
            "strong": sum(1 for r in rows if r["impact"] == "fort"),
            "geo_count": len(geo_rows),
            "themes": [(k, self.theme_names[k], n) for k, n in themes.most_common() if k in self.theme_names],
            "themes_max": max(themes.values(), default=0),
            "local": sorted(({"id": e["id"], "name": e["name"], "kind": e["kind"]} for e in local),
                            key=lambda e: (e["kind"], e["name"].lower())),
            "cited": [{"id": k, "name": self.entities[k]["name"], "kind": self.entities[k]["kind"], "count": n}
                      for k, n in cited.most_common(12)],
            "groups": self.group_by_day(rows),
            "count": len(rows),
            "options": self.options(rows, None, None),
            "tenders": self.tender_box([t for t in self.tenders if t["country"] == code or code in t["countries"]]),
        }

    def entity_index(self) -> list[dict[str, str]]:
        return sorted(({"name": e["name"], "id": e["id"], "kind": e["kind"]} for e in self.entities.values()),
                      key=lambda r: r["name"].lower())


# ---------------------------------------------------------------- render


def static_version(directory: Path) -> str:
    """Short hash of the static files, so browsers reload CSS/JS as soon as they change."""
    digest = hashlib.sha1()
    for path in sorted(directory.rglob("*")):
        if path.is_file():
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


def render(settings: dict[str, Any]) -> None:
    paths = settings["paths"]
    data = SiteData(settings)
    env = Environment(
        loader=FileSystemLoader(resolve(paths["templates"])),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    feeds = [s for s in data.sources if s.get("via") not in collect.SEARCHES]
    searches = [s for s in data.sources if s.get("via") in collect.SEARCHES]
    common = {
        "updated_at": data.updated_at,
        "updated_label": format_datetime(data.updated_at),
        "tabs": [(k, f, t) for k, f, t, _, _ in TABS if k != COMP] + [MARKET_PAGE[:3]]
                + [(k, f, t) for k, f, t, _, _ in TABS if k == COMP],
        "country_pages": set(data.country_codes()),
        "entity_index": data.entity_index(),
        "feeds_ok": sum(1 for s in feeds if s.get("ok")),
        "feeds_total": len(feeds),
        "feeds_err": [s for s in feeds if not s.get("ok")],
        "searches_ok": sum(1 for s in searches if s.get("ok")),
        "searches_total": len(searches),
        "periods": settings["history"]["periods"],
        "display_days": settings["history"]["display_days"],
        "retention_days": settings["history"]["retention_days"],
        "default_domain": settings["history"].get("default_domain", "all"),
        "asset_version": static_version(resolve(paths["static"])),
    }

    out = resolve(paths["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    def write(name: str, template: str, **ctx) -> None:
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        root = "../" * name.count("/")
        html = env.get_template(template).render(root=root, **{**common, **ctx})
        path.write_text(html, encoding="utf-8", newline="\n")

    tab_days = settings["history"].get("tab_days", settings["history"]["retention_days"])
    world = worldmap.build_paths(resolve(paths["world_map"]), data.ref["countries"])
    world["names"] = {code: c["name"] for code, c in data.ref["countries"].items()}
    for key, filename, title, subtitle, kinds in TABS:
        days = data.competitors["page"]["feed_days"] if key == COMP else tab_days
        tab_rows = (data.competitor_rows() if key == COMP
                    else [r for r in data.stories if on_page(r, key) and r["age"] < tab_days])
        kind = kinds[0] if kinds[0] != "country" else None
        write(filename, "tab.html.j2", page=key, title=title, subtitle=subtitle, kind=kind,
              groups=data.group_by_day(tab_rows), count=len(tab_rows),
              options=data.options(tab_rows, kind, key),
              sidebars=[data.sidebar(k, tab_rows) for k in kinds],
              periods=[d for d in settings["history"]["periods"] if d <= days],
              world=world if key == GEO else None,
              competitors=data.competitor_view() if key == COMP else None)

    country_dir = out / "pays"
    if country_dir.exists():
        shutil.rmtree(country_dir)
    for code in data.country_codes():
        write(f"pays/{code}.html", "country.html.j2", page="pays",
              title=data.ref["countries"][code]["name"], view=data.country_view(code))
    tcfg = data.tender_meta["config"]
    sources = []
    for s in data.tender_meta["sources"]:
        when = data.tender_meta.get("updated_at")
        sources.append({**s, "when": format_datetime(datetime.fromisoformat(when)) if when else ""})
    write(MARKET_PAGE[1], "marche.html.j2", page="marche", title="Marché", subtitle=MARKET_PAGE[3],
          market=market.market_payload(data, settings, MONTHS),
          market_days=settings["market"]["default_days"],
          tenders=market.tender_payload(data.tenders, data.tender_meta, data.ref, data.analysis, data.entities,
                                        data.updated_at.date(), MONTHS),
          tender_days=tcfg["display"]["default_days"], tender_periods=tcfg["display"]["periods"],
          tender_sources=sources, tender_pending=data.tender_meta["pending"],
          fx_date=format_date(datetime.fromisoformat(data.tender_meta["fx_date"])) if data.tender_meta["fx_date"] else "",
          press=market.press_rows(data.stories, tcfg), tenders_press_days=tcfg["display"]["press_days"])

    entity_dir = out / "entite"
    if entity_dir.exists():
        shutil.rmtree(entity_dir)
    for eid in data.entities:
        write(f"entite/{eid}.html", "entity.html.j2", page="entite", title=data.entities[eid]["name"],
              view=data.entity_view(eid))

    (out / ".nojekyll").touch()
    static_out = out / "static"
    if static_out.exists():
        shutil.rmtree(static_out)
    shutil.copytree(resolve(paths["static"]), static_out)
    log.info("Site written to %s (%d items in %d stories, %d entity pages)",
             out, len(data.items), len(data.stories), len(data.entities))


def serve(settings: dict[str, Any], port: int) -> None:
    directory = resolve(settings["paths"]["output_dir"])
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Aperçu : http://localhost:{port}  (Ctrl+C pour arrêter)")
        httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static site.")
    parser.add_argument("--skip-collect", action="store_true",
                        help="no network: reuse data/items.json and data/entities.json")
    parser.add_argument("--serve", action="store_true", help="serve docs/ after building")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--analyse-max", type=int, default=None,
                        help="raise the daily cap of stories analysed by Claude (first fill)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = load_settings()
    if not args.skip_collect:
        ref = entities_mod.load_reference(settings)
        entities_mod.refresh(settings, ref)        # weekly; keeps the cache on failure
        collect.print_report(collect.run(settings, analyse_max=args.analyse_max))
        tenders_mod.print_report(tenders_mod.update(settings, ref))
    render(settings)
    if args.serve:
        serve(settings, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
