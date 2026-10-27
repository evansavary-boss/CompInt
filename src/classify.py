"""Rule-based classification: entities, countries, regions, event types and tabs.

Everything comes from the reference YAML files (see src/entities.py), so classification
can be re-run on the whole history whenever aliases or patterns change.
"""

from __future__ import annotations

import re
from typing import Any, Iterable

from .models import Item
from .textmatch import TermIndex, fold

TABS = ("geopolitique", "politique", "economie")


def geo_theme_index(geo_cfg: dict[str, Any] | None) -> TermIndex:
    """Expressions of each geopolitics family (config/geopolitics.yaml)."""
    index = TermIndex(plurals=True)
    for key, theme in ((geo_cfg or {}).get("themes") or {}).items():
        index.add_many(theme.get("patterns"), key)
        index.add_many(theme.get("patterns_cs"), key, case_sensitive=True)
    return index


class Classifier:
    def __init__(self, ref: dict[str, Any], geo_cfg: dict[str, Any] | None = None):
        self.ref = ref
        self.geo_themes = geo_theme_index(geo_cfg)
        self.entities = TermIndex(plurals=False)
        for eid, e in ref["entities"].items():
            self.entities.add_many(e.get("aliases"), eid)
            self.entities.add_many(e.get("aliases_cs"), eid, case_sensitive=True)

        self.countries = TermIndex(plurals=True)
        for code, c in ref["countries"].items():
            self.countries.add_many(c.get("aliases"), code)
            self.countries.add_many(c.get("aliases_cs"), code, case_sensitive=True)

        self.events = TermIndex(plurals=True)
        self.event_regex: list[tuple[str, re.Pattern]] = []
        for key, ev in ref["events"].items():
            self.events.add_many(ev.get("patterns"), key)
            self.events.add_many(ev.get("patterns_cs"), key, case_sensitive=True)
            self.event_regex += [(key, re.compile(rx)) for rx in ev.get("regex") or []]
        self.event_order = list(ref["events"])

    # ------------------------------------------------------------ parts

    def find_entities(self, text: str) -> list[str]:
        return self.entities.targets(text)

    def find_events(self, text: str, entity_ids: Iterable[str] = ()) -> list[str]:
        found = set(self.events.targets(text))
        folded = fold(text)
        found |= {key for key, rx in self.event_regex if rx.search(folded)}
        kinds = {self.ref["entities"][e]["kind"] for e in entity_ids}
        found = {k for k in found if self.ref["events"][k].get("requires") in (None, *kinds)}
        hidden = {o for key in found for o in self.ref["events"][key].get("overrides") or []}
        return [k for k in self.event_order if k in found and k not in hidden]

    def locate(self, text: str, entity_ids: Iterable[str]) -> tuple[list[str], list[str]]:
        """Countries named in the text plus each entity's main country; their regions."""
        countries = self.countries.targets(text)
        regions: list[str] = []
        for eid in entity_ids:
            e = self.ref["entities"][eid]
            main = (e.get("countries") or [None])[0]
            if main and main not in countries:
                countries.append(main)
            if e.get("region") and e["region"] not in regions:
                regions.append(e["region"])
        for code in countries:
            region = self.ref["countries"][code]["region"]
            if region not in regions:
                regions.append(region)
        return countries, regions

    def tabs(self, events: list[str], entity_ids: list[str], source_section: str) -> list[str]:
        """Tabs where the item is listed.

        Event types decide first. Without any event, institutions point to Politique,
        companies to Économie, and the source's default tab fills the gap.
        """
        kinds = {self.ref["entities"][e]["kind"] for e in entity_ids}
        ev_tabs: set[str] = set()
        for k in events:
            section = self.ref["events"][k]["section"]
            if section != "auto":
                ev_tabs.add(section)
                continue
            # "auto" events (appointments) follow the entities involved.
            if "institution" in kinds:
                ev_tabs.add("politique")
            if "company" in kinds:
                ev_tabs.add("economie")
            if not kinds:
                ev_tabs.add(source_section if source_section in TABS else "economie")
        tabs = set(ev_tabs)
        if not ev_tabs:
            if "institution" in kinds:
                tabs.add("politique")
            if "company" in kinds:
                tabs.add("economie")
            if source_section in TABS:
                tabs.add(source_section)
        if not tabs:
            tabs.add(source_section if source_section in TABS else "economie")
        return [t for t in TABS if t in tabs]

    # ------------------------------------------------------------ item

    def classify(self, item: Item) -> Item:
        text = f"{item.title}. {item.summary}"
        entity_ids = self.find_entities(text)
        for eid in item.query_entities:
            if eid in self.ref["entities"] and eid not in entity_ids:
                entity_ids.append(eid)
        item.entities = entity_ids
        item.events = self.find_events(text, entity_ids)
        item.countries, item.regions = self.locate(text, entity_ids)
        item.sections = self.tabs(item.events, entity_ids, item.section)
        if item.section == "geopolitique":
            # Keep the source's default family when the wording no longer matches any expression.
            item.geo_themes = self.geo_themes.targets(text) or item.geo_themes
        return item

    def classify_all(self, items: Iterable[Item]) -> list[Item]:
        return [self.classify(i) for i in items]
