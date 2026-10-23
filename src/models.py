"""Shared data structures."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Item:
    id: str
    title: str
    url: str
    source: str
    section: str              # "politique" | "entreprises"
    published: str            # ISO 8601 with Europe/Paris offset
    summary: str              # cleaned feed text; shortened to an excerpt by the enricher
    tags: list[str] = field(default_factory=list)
    first_seen: str = ""
    date_estimated: bool = False   # True when the feed gave no usable date

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Item":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


@dataclass
class SourceStatus:
    name: str
    url: str
    ok: bool
    error: str = ""
    fetched: int = 0
    kept: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
