"""Shared data structures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Values written by older versions of the pipeline.
_LEGACY_SECTIONS = {"entreprises": "economie"}


@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str               # publisher name
    section: str              # default tab of the source: "politique" | "economie"
    published: str            # ISO 8601 with Europe/Paris offset
    summary: str              # cleaned feed text; shortened to an excerpt by the enricher
    tags: list[str] = field(default_factory=list)          # geospatial / newspace
    first_seen: str = ""
    date_estimated: bool = False   # True when the feed gave no usable date
    via: str = ""                  # "" (publisher feed) or "gnews"
    query_entities: list[str] = field(default_factory=list)  # entities whose Google News query found it
    geo_themes: list[str] = field(default_factory=list)      # geopolitics families (config/geopolitics.yaml)
    # Set by src/classify.py
    entities: list[str] = field(default_factory=list)
    countries: list[str] = field(default_factory=list)
    regions: list[str] = field(default_factory=list)
    events: list[str] = field(default_factory=list)
    sections: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        item = cls(**known)
        item.section = _LEGACY_SECTIONS.get(item.section, item.section)
        return item


@dataclass
class SourceStatus:
    name: str
    url: str
    ok: bool
    error: str = ""
    fetched: int = 0
    kept: int = 0
    via: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
