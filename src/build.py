"""Build the static site in docs/.

Pages: index.html (Politique), economie.html, marche.html and one page per entity
in entite/<id>.html.

Usage:
    python -m src.build                  # refresh entities, collect news, render docs/
    python -m src.build --skip-collect   # render from the existing data/*.json
    python -m src.build --serve          # build, then serve docs/ on http://localhost:8000
"""

from __future__ import annotations

import argparse
import functools
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

from . import collect, entities as entities_mod, market
from .config import load_settings, load_yaml, resolve
from .filter import normalize_text
from .models import Item
from .store import load_items
from .stories import StoryBook, aggregate

log = logging.getLogger("build")

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
          "juil.", "août", "sept.", "oct.", "nov.", "déc."]
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

# Tabs: key, file, title, subtitle, entity kind listed in the sidebar.
TABS = [
    ("politique", "index.html", "Politique",
     "Défense et espace, institutions, budgets, réglementation, coopération", "institution"),
    ("economie", "economie.html", "Économie",
     "Contrats, lancements, levées de fonds, rachats, produits", "company"),
]
MARKET_PAGE = ("marche", "marche.html", "Marché",
               "Contrats, levées de fonds, rachats et lancements détectés dans l'actualité", None)

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

        self.analysis = load_yaml(paths["analysis"])
        impact_keys = list(self.analysis["impact"])
        self.impact_rank = {k: len(impact_keys) - n for n, k in enumerate(impact_keys)}   # fort = 3
        self.theme_names = {k: v["label"] for k, v in self.analysis["themes"].items()}
        self.industry_names = self.analysis["industries"]
        self.stories = self._load_stories()

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
        themes = [(k, self.theme_names[k]) for k in a.get("themes", []) if k in self.theme_names]
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
        ents = Counter(e for r in rows for e in r["entity_ids"]
                       if e in self.entities and (kind is None or self.entities[e]["kind"] == kind))
        events = Counter(e for r in rows for e in r["event_keys"])
        segments = Counter(s for r in rows for s in r["segments"])
        industries = Counter(k for r in rows for k, _ in r["industries"])
        event_keys = [k for k, v in self.ref["events"].items()
                      if k in events and (tab is None or v["section"] in (tab, "auto"))]
        return {
            "regions": [(k, self.region_names[k]) for k in self.region_names if k in regions],
            "countries": [(k, self.country_names.get(k, k), n) for k, n in countries.most_common()],
            "entities": sorted(((k, self.entities[k]["name"], n) for k, n in ents.items()),
                               key=lambda t: (-t[2], t[1].lower())),
            "segments": [(k, v) for k, v in self.segment_names.items() if k in segments],
            "industries": [(k, v, industries[k]) for k, v in self.industry_names.items() if k in industries],
            "events": [(k, self.event_labels[k], events[k]) for k in event_keys],
            "analysed": any(r["analysed"] for r in rows),
        }

    def sidebar(self, kind: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Entities of one kind, grouped (segment or type), with their story counts."""
        counts = Counter(e for r in rows for e in r["entity_ids"])
        groups: dict[str, list] = {}
        for e in self.entities.values():
            if e["kind"] != kind:
                continue
            key = (e.get("segments") or ["autre"])[0] if kind == "company" else e.get("type", "autre")
            groups.setdefault(key, []).append({"id": e["id"], "name": e["name"], "count": counts[e["id"]]})
        labels = self.segment_names if kind == "company" else self.type_names
        out = []
        for key, label in labels.items():
            if key in groups:
                ranked = sorted(groups[key], key=lambda r: (-r["count"], r["name"].lower()))
                out.append({"label": label, "entities": ranked})
        return out

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
        }

    def entity_index(self) -> list[dict[str, str]]:
        return sorted(({"name": e["name"], "id": e["id"], "kind": e["kind"]} for e in self.entities.values()),
                      key=lambda r: r["name"].lower())


# ---------------------------------------------------------------- render


def render(settings: dict[str, Any]) -> None:
    paths = settings["paths"]
    data = SiteData(settings)
    env = Environment(
        loader=FileSystemLoader(resolve(paths["templates"])),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    feeds = [s for s in data.sources if s.get("via") != collect.GNEWS]
    searches = [s for s in data.sources if s.get("via") == collect.GNEWS]
    common = {
        "updated_at": data.updated_at,
        "updated_label": format_datetime(data.updated_at),
        "tabs": [(k, f, t) for k, f, t, _, _ in TABS] + [MARKET_PAGE[:3]],
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
        "asset_version": data.updated_at.strftime("%Y%m%d%H%M"),
    }

    out = resolve(paths["output_dir"])
    out.mkdir(parents=True, exist_ok=True)

    def write(name: str, template: str, **ctx) -> None:
        path = out / name
        path.parent.mkdir(parents=True, exist_ok=True)
        root = "../" * name.count("/")
        html = env.get_template(template).render(root=root, **common, **ctx)
        path.write_text(html, encoding="utf-8", newline="\n")

    for key, filename, title, subtitle, kind in TABS:
        tab_rows = [r for r in data.stories if key in r["sections"]]
        write(filename, "tab.html.j2", page=key, title=title, subtitle=subtitle, kind=kind,
              groups=data.group_by_day(tab_rows), count=len(tab_rows),
              options=data.options(tab_rows, kind, key), sidebar=data.sidebar(kind, tab_rows))
    write(MARKET_PAGE[1], "marche.html.j2", page="marche", title="Marché", subtitle=MARKET_PAGE[3],
          market=market.market_payload(data, settings, MONTHS),
          market_days=settings["market"]["default_days"])

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
    render(settings)
    if args.serve:
        serve(settings, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
