"""Item enrichment.

V0 ships `NoopEnricher`, which only turns the feed text into a short excerpt.
A future `ClaudeEnricher` (Anthropic API, key read from the environment) can
re-rank, summarise in two lines and set tags by implementing the same
interface and registering itself in `ENRICHERS`; nothing else needs to change.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from .models import Item


def truncate(text: str, max_chars: int) -> str:
    """Cut `text` at a word boundary so the result is at most `max_chars` long."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.-–—") + "…"


class Enricher(ABC):
    """Transforms a list of tagged items. May drop, reorder or retag items."""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings

    @abstractmethod
    def enrich(self, items: list[Item]) -> list[Item]:
        ...


class NoopEnricher(Enricher):
    """Keeps the feed excerpt as is, shortened to the configured length."""

    def enrich(self, items: list[Item]) -> list[Item]:
        max_chars = self.settings["text"]["excerpt_max_chars"]
        for item in items:
            item.summary = truncate(item.summary, max_chars)
        return items


ENRICHERS: dict[str, type[Enricher]] = {
    "noop": NoopEnricher,
}


def get_enricher(settings: dict[str, Any]) -> Enricher:
    name = settings.get("enricher", "noop")
    try:
        return ENRICHERS[name](settings)
    except KeyError:
        raise ValueError(f"Unknown enricher '{name}'. Available: {', '.join(ENRICHERS)}")
