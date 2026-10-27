"""Group articles that report the same news into stories.

A story keeps a stable id (that of the article that created it) across daily runs, so
its Claude analysis is computed once and cached in data/stories.json.

Usage:
    python -m src.stories              # regroup stored items, analyse new stories, print a report
    python -m src.stories --no-analyse # regroup only
    python -m src.stories --max 400    # raise the analysis cap for this run (backfill)
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from .config import load_settings, load_yaml, resolve
from .models import Item
from .textmatch import fold

log = logging.getLogger("stories")

# Function words ignored when comparing titles (English and French).
STOPWORDS = set("""
a an the and or of to in on at for with by from as is are was were be been its it this that these those
into over after before about amid new says said will would could may than then up out via vs
le la les un une des du de d l et ou au aux en dans sur pour par avec sans sous chez son sa ses leur
leurs ce cet cette ces qui que quoi dont est sont a ont été plus moins selon face apres avant entre
""".split())
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def title_tokens(title: str) -> frozenset[str]:
    """Significant, accent-free, singularised words of a title."""
    out = set()
    for tok in _TOKEN_RE.findall(fold(title)):
        if tok in STOPWORDS or (len(tok) < 3 and not tok.isdigit()):
            continue
        if len(tok) > 4 and tok.endswith("s") and not tok.isdigit():
            tok = tok[:-1]
        out.add(tok)
    return frozenset(out)


def _numbers(tokens: Iterable[str]) -> set[str]:
    """Digit runs of the title ("$500M" and "500 million" both give "500")."""
    return {n for t in tokens for n in re.findall(r"\d+", t)}


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def same_news(a: Item, b: Item, ta: frozenset[str], tb: frozenset[str], cfg: dict[str, Any]) -> float:
    """Similarity score if `a` and `b` report the same news, else 0.

    Different figures ("28 Starlink satellites" vs "24") mean different news even when
    the wording is identical, as with daily launch reports.
    """
    na, nb = _numbers(ta), _numbers(tb)
    if na and nb and not (na & nb):
        return 0.0
    j = jaccard(ta, tb)
    if j >= cfg["title_similarity"]:
        return j
    shared_entity = set(a.entities) & set(b.entities)
    same_event = bool(set(a.events) & set(b.events)) or (not a.events and not b.events)
    if j >= cfg["entity_similarity"] and shared_entity and same_event:
        return j
    return 0.0


@dataclass
class Story:
    id: str
    item_ids: list[str]
    analysis: dict[str, Any] = field(default_factory=dict)
    analysed_at: str = ""
    analysed_size: int = 0
    analysis_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Story":
        known = {k: data[k] for k in cls.__dataclass_fields__ if k in data}
        return cls(**known)


class StoryBook:
    """All stories, with the item -> story index."""

    def __init__(self, stories: Iterable[Story] = (), meta: dict[str, Any] | None = None):
        self.stories: dict[str, Story] = {s.id: s for s in stories}
        self.meta: dict[str, Any] = meta or {}

    # ------------------------------------------------------------ persistence

    @classmethod
    def load(cls, path: Path) -> "StoryBook":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("unreadable %s, starting from scratch", path)
            return cls()
        stories = [Story.from_dict(s) for s in data.get("stories", [])]
        return cls(stories, {k: v for k, v in data.items() if k != "stories"})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {**self.meta, "stories": [s.to_dict() for s in self.stories.values()]}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8", newline="\n")
        tmp.replace(path)

    # ------------------------------------------------------------ grouping

    def story_of(self) -> dict[str, str]:
        return {iid: s.id for s in self.stories.values() for iid in s.item_ids}

    def assign(self, items: list[Item], cfg: dict[str, Any]) -> int:
        """Put every item in a story. Returns the number of new stories."""
        by_id = {i.id: i for i in items}
        # Forget items that left the history, then empty stories.
        for s in list(self.stories.values()):
            s.item_ids = [iid for iid in s.item_ids if iid in by_id]
            if not s.item_ids:
                del self.stories[s.id]

        index = self.story_of()
        tokens = {i.id: title_tokens(i.title) for i in items}
        published = {i.id: datetime.fromisoformat(i.published) for i in items}
        window = timedelta(hours=cfg["window_hours"])
        anchors = {sid: min(published[iid] for iid in s.item_ids) for sid, s in self.stories.items()}

        created = 0
        for item in sorted(items, key=lambda i: published[i.id]):
            if item.id in index:
                continue
            when = published[item.id]
            best, best_score = None, 0.0
            for sid, anchor in anchors.items():
                if abs(when - anchor) > window:
                    continue
                for other_id in self.stories[sid].item_ids:
                    score = same_news(item, by_id[other_id], tokens[item.id], tokens[other_id], cfg)
                    if score > best_score:
                        best, best_score = sid, score
            if best is None:
                sid = f"s{item.id}"
                self.stories[sid] = Story(id=sid, item_ids=[item.id])
                anchors[sid] = when
                created += 1
            else:
                self.stories[best].item_ids.append(item.id)
                anchors[best] = min(anchors[best], when)
            index[item.id] = best or f"s{item.id}"
        return created

    def merge(self, source_id: str, target_id: str) -> bool:
        """Move every item of `source_id` into `target_id` (duplicate found by Claude)."""
        if source_id == target_id or source_id not in self.stories or target_id not in self.stories:
            return False
        target = self.stories[target_id]
        for iid in self.stories.pop(source_id).item_ids:
            if iid not in target.item_ids:
                target.item_ids.append(iid)
        return True


# ---------------------------------------------------------------- views


def headline(items: list[Item]) -> Item:
    """The article that represents a story: publisher feeds first, then the earliest."""
    return min(items, key=lambda i: (i.via != "", i.published))


def aggregate(story: Story, by_id: dict[str, Item]) -> dict[str, Any]:
    """Story-level fields derived from its items (union of the rule-based tags)."""
    items = sorted((by_id[i] for i in story.item_ids if i in by_id), key=lambda i: i.published)
    head = headline(items)

    def union(attr: str) -> list[str]:
        out: list[str] = []
        for it in items:
            for v in getattr(it, attr):
                if v not in out:
                    out.append(v)
        return out

    sources = []
    for it in items:
        if it.source not in sources:
            sources.append(it.source)
    return {
        "story": story,
        "items": items,
        "headline": head,
        "first_published": items[0].published,
        "last_published": items[-1].published,
        "sources": sources,
        "entities": union("entities"),
        "countries": union("countries"),
        "regions": union("regions"),
        "events": union("events"),
        "sections": union("sections"),
        "tags": union("tags"),
        "geo_themes": union("geo_themes"),
    }


# ---------------------------------------------------------------- pipeline


def update(items: list[Item], settings: dict[str, Any], ref: dict[str, Any],
           analyse: bool = True, max_stories: int | None = None) -> StoryBook:
    """Regroup items into stories, analyse new ones when enabled, save data/stories.json."""
    from .enrich import analyse_stories   # local import: enrich depends on this module

    acfg = load_yaml(settings["paths"]["analysis"])
    path = resolve(settings["paths"]["stories"])
    book = StoryBook.load(path)
    created = book.assign(items, acfg["stories"])
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz).replace(microsecond=0)
    report: dict[str, Any] = {"created": created, "analysed": 0, "merged": 0, "error": ""}
    if analyse and acfg["analysis"].get("enabled"):
        report.update(analyse_stories(book, items, acfg, ref, now, max_stories))
    book.meta.update({"updated_at": now.isoformat(), "last_run": report})
    book.save(path)
    return book


def print_report(book: StoryBook, items: list[Item]) -> None:
    by_id = {i.id: i for i in items}
    sizes = [len(s.item_ids) for s in book.stories.values()]
    multi = [n for n in sizes if n > 1]
    analysed = [s for s in book.stories.values() if s.analysis]
    run = book.meta.get("last_run", {})
    print(f"\nSujets : {len(sizes)} pour {len(items)} articles "
          f"({len(multi)} sujets regroupent {sum(multi)} articles ; le plus gros : {max(sizes, default=0)})")
    print(f"Nouveaux sujets : {run.get('created', 0)}   Analysés ce passage : {run.get('analysed', 0)}   "
          f"Doublons fusionnés par Claude : {run.get('merged', 0)}")
    if run.get("error"):
        print(f"Analyse : {run['error']}")
    if run.get("cost_usd") is not None:
        print(f"Usage de l'abonnement Claude : ce passage aurait coûté {run['cost_usd']:.3f} $ au tarif de "
              f"l'API (indication seulement, rien n'est facturé ; seul ton quota est consommé)")
    print(f"Sujets analysés au total : {len(analysed)} / {len(sizes)}")
    top = sorted(book.stories.values(), key=lambda s: -len(s.item_ids))[:8]
    for s in top:
        view = aggregate(s, by_id)
        title = s.analysis.get("title_fr") or view["headline"].title
        print(f"  {len(s.item_ids):3d} articles  {len(view['sources']):2d} sources  "
              f"[{s.analysis.get('impact', '-'):6}] {title[:90]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Group items into stories and analyse them.")
    parser.add_argument("--no-analyse", action="store_true", help="regroup only, no Claude call")
    parser.add_argument("--max", type=int, default=None, help="analysis cap for this run")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from .entities import load_reference
    from .store import load_items

    settings = load_settings()
    items, _ = load_items(resolve(settings["paths"]["items"]))
    book = update(items, settings, load_reference(settings), analyse=not args.no_analyse, max_stories=args.max)
    print_report(book, items)
    return 0


if __name__ == "__main__":
    sys.exit(main())
