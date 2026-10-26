"""Fetch publisher RSS feeds and Google News searches, then filter, dedupe, enrich,
classify and store items.

Usage:
    python -m src.collect               # update data/items.json and print a report
    python -m src.collect --dry-run     # same, without writing anything
    python -m src.collect --backfill    # wider Google News window (first fill)
    python -m src.collect --no-gnews    # publisher feeds only
    python -m src.collect --no-analyse  # group stories without calling Claude
"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import feedparser
import requests

from .classify import Classifier
from .config import load_settings, load_yaml, resolve
from . import stories as stories_mod
from .enrich import truncate
from .entities import load_reference
from .filter import KeywordMatcher, canonical_url, dedupe, tag_items
from .models import Item, SourceStatus
from .store import load_items, prune, save_items, sort_items

log = logging.getLogger("collect")

_TAG_RE = re.compile(r"<[^>]+>")
GNEWS = "gnews"


def clean_html(raw: str, strip_patterns: list[re.Pattern] = (), max_chars: int | None = None) -> str:
    """Turn an HTML feed fragment into plain text."""
    text = _TAG_RE.sub(" ", raw or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    for rx in strip_patterns:
        text = rx.sub("", text).strip()
    if max_chars is not None:
        text = text[:max_chars]
    return text


def parse_entry_date(entry: dict, tz: ZoneInfo, now: datetime) -> tuple[datetime, bool]:
    """Return the entry date in `tz` and whether it had to be estimated.

    feedparser exposes `*_parsed` as UTC struct_time. Missing dates fall back
    to `now`; dates in the future are clamped to `now`.
    """
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        st = entry.get(key)
        if st:
            try:
                dt = datetime(*st[:6], tzinfo=timezone.utc).astimezone(tz)
            except (TypeError, ValueError):
                continue
            return (min(dt, now), False)
    return (now, True)


def item_id(url: str, tracking_params: list[str]) -> str:
    return hashlib.sha1(canonical_url(url, tracking_params).encode("utf-8")).hexdigest()[:16]


def split_gnews_title(title: str, publisher: str) -> str:
    """Google News appends " - Publisher" to every title."""
    suffix = f" - {publisher}"
    if publisher and title.endswith(suffix):
        return title[: -len(suffix)].strip()
    return title


# ---------------------------------------------------------------- Google News sources


def gnews_sources(ref: dict[str, Any], settings: dict[str, Any], backfill: bool = False) -> list[dict[str, Any]]:
    """One virtual source per entity and Google News edition."""
    cfg = settings["google_news"]
    if not cfg.get("enabled"):
        return []
    when = cfg["backfill_when"] if backfill else cfg["when"]
    rk = cfg.get("require_keywords_for") or {}
    out = []
    for eid, e in ref["entities"].items():
        main_country = (e.get("countries") or [None])[0]
        editions = e.get("news_editions") or cfg["country_editions"].get(main_country) or cfg["default_editions"]
        require = e.get("news_require_keywords")
        if require is None:
            require = bool(set(e.get("segments") or []) & set(rk.get("segments") or [])
                           or e.get("type") in (rk.get("types") or []))
        query = e.get("news_query") or f'"{e["name"]}"'
        for ed in editions:
            params = {"q": f"{query} when:{when}", **cfg["editions"][ed]}
            out.append({
                "name": f"Google News · {e['name']} ({ed})",
                "url": f"{cfg['base_url']}?{urlencode(params)}",
                "section": "politique" if e["kind"] == "institution" else "economie",
                "domains": e.get("domains") or [],
                "require_keywords": require,
                "entity": eid,
                "via": GNEWS,
            })
    return out


# ---------------------------------------------------------------- fetching


def fetch_feed(url: str, http_cfg: dict[str, Any]) -> feedparser.FeedParserDict:
    """Download a feed with timeout, explicit User-Agent and a small retry."""
    attempts = 1 + int(http_cfg.get("retries", 0))
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": http_cfg["user_agent"]},
                timeout=http_cfg["timeout_seconds"],
            )
            if resp.status_code >= 500 and attempt < attempts - 1:
                raise requests.HTTPError(f"HTTP {resp.status_code}")
            resp.raise_for_status()
            return feedparser.parse(resp.content)
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < attempts - 1:
                time.sleep(http_cfg.get("retry_delay_seconds", 2))
    raise last_exc  # type: ignore[misc]


def collect_source(
    source: dict[str, Any],
    settings: dict[str, Any],
    tz: ZoneInfo,
    now: datetime,
    strip_patterns: list[re.Pattern],
) -> tuple[list[Item], SourceStatus]:
    status = SourceStatus(name=source["name"], url=source["url"], ok=False, via=source.get("via", ""))
    try:
        feed = fetch_feed(source["url"], settings["http"])
    except Exception as exc:  # never let one feed break the run
        status.error = _short_error(exc)
        log.warning("%s: %s", source["name"], status.error)
        return [], status

    is_gnews = source.get("via") == GNEWS
    if not feed.entries:
        if is_gnews and not feed.bozo:   # an empty search is not an error
            status.ok = True
            return [], status
        status.error = "flux vide ou illisible" if feed.bozo else "aucune entrée"
        log.warning("%s: %s", source["name"], status.error)
        return [], status

    tracking = settings["dedup"]["tracking_params"]
    max_raw = settings["text"]["raw_summary_max_chars"]
    items = []
    for entry in feed.entries:
        link = entry.get("link")
        title = clean_html(entry.get("title", ""))
        if not link or not title:
            continue
        published, estimated = parse_entry_date(entry, tz, now)
        publisher = source["name"]
        summary = clean_html(entry.get("summary") or entry.get("description") or "", strip_patterns, max_raw)
        if is_gnews:
            publisher = clean_html((entry.get("source") or {}).get("title", "")) or "Google News"
            title = split_gnews_title(title, publisher)
            summary = ""   # Google News only repeats the title and the publisher
        items.append(
            Item(
                id=item_id(link, tracking),
                title=title,
                url=link,
                source=publisher,
                section=source["section"],
                published=published.isoformat(timespec="seconds"),
                summary=summary,
                first_seen=now.isoformat(timespec="seconds"),
                date_estimated=estimated,
                via=source.get("via", ""),
                query_entities=[source["entity"]] if source.get("entity") else [],
            )
        )
    if is_gnews:
        items.sort(key=lambda i: datetime.fromisoformat(i.published), reverse=True)
        items = items[: settings["google_news"]["max_items_per_query"]]
    status.ok = True
    status.fetched = len(items)
    return items, status


def _short_error(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "délai dépassé"
    if isinstance(exc, requests.ConnectionError):
        return "connexion impossible"
    return f"{type(exc).__name__}: {str(exc)[:80]}"


def fetch_all(sources: list[dict[str, Any]], settings: dict[str, Any], tz: ZoneInfo, now: datetime,
              strip_patterns: list[re.Pattern]) -> list[tuple[dict[str, Any], list[Item], SourceStatus]]:
    """Publisher feeds in parallel (distinct hosts); Google News one request at a time."""
    feeds = [s for s in sources if s.get("via") != GNEWS]
    searches = [s for s in sources if s.get("via") == GNEWS]
    results = []
    with ThreadPoolExecutor(max_workers=settings["http"]["max_workers"]) as pool:
        for src, (items, status) in zip(feeds, pool.map(
                lambda s: collect_source(s, settings, tz, now, strip_patterns), feeds)):
            results.append((src, items, status))
    delay = settings["google_news"].get("delay_seconds", 1)
    for n, src in enumerate(searches):
        if n:
            time.sleep(delay)
        items, status = collect_source(src, settings, tz, now, strip_patterns)
        results.append((src, items, status))
    return results


# ---------------------------------------------------------------- pipeline


def drop_excluded(items: list[Item], matcher: KeywordMatcher, settings: dict[str, Any]) -> list[Item]:
    """Remove items hit by `global_exclude` or, for Google News, by `title_exclude_regex`.

    Also applied to already stored items, so new exclusions clean the history.
    """
    title_rx = [re.compile(p) for p in settings["google_news"].get("title_exclude_regex") or []]
    return [i for i in items
            if not matcher.is_excluded(f"{i.title} {i.summary}")
            and not (i.via == GNEWS and any(rx.search(i.title) for rx in title_rx))]


def _merge_query_entities(kept: Item, dropped: Item) -> None:
    for eid in dropped.query_entities:
        if eid not in kept.query_entities:
            kept.query_entities.append(eid)


def run(settings: dict[str, Any] | None = None, dry_run: bool = False,
        backfill: bool = False, use_gnews: bool = True, analyse: bool = True,
        analyse_max: int | None = None) -> dict[str, Any]:
    """Full news update. Returns a summary used by the CLI report and build."""
    settings = settings or load_settings()
    paths = settings["paths"]
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz).replace(microsecond=0)

    ref = load_reference(settings)
    classifier = Classifier(ref)
    sources = load_yaml(paths["sources"])["sources"]
    if use_gnews:
        sources = sources + gnews_sources(ref, settings, backfill)
    matcher = KeywordMatcher(load_yaml(paths["keywords"]))
    strip_patterns = [
        re.compile(p, re.IGNORECASE) for p in settings["text"].get("summary_strip_patterns", [])
    ]

    results = fetch_all(sources, settings, tz, now, strip_patterns)

    retention = settings["history"]["retention_days"]
    tracking = settings["dedup"]["tracking_params"]
    threshold = settings["dedup"]["title_similarity"]

    items_path = resolve(paths["items"])
    existing, _ = load_items(items_path)
    existing = prune(existing, now, retention)

    statuses = []
    feed_items: list[Item] = []
    search_items: list[Item] = []
    for src, items, status in results:
        tagged = tag_items(items, matcher, source=src)
        if src.get("via") == GNEWS:
            # The searched entity must appear in the title: Google News also matches
            # on article bodies, which brings in many loosely related stories.
            tagged = [i for i in tagged if src["entity"] in classifier.find_entities(i.title)]
            search_items.extend(tagged)
        else:
            feed_items.extend(tagged)
        status.kept = len(tagged)
        statuses.append(status)

    def oldest_first(items: list[Item]) -> list[Item]:
        return sorted(prune(items, now, retention), key=lambda i: datetime.fromisoformat(i.published))

    # Publisher feeds first, so their direct links and excerpts win over Google News.
    new_items = dedupe(oldest_first(feed_items), threshold, tracking, existing=existing,
                       on_duplicate=_merge_query_entities)
    new_items += dedupe(oldest_first(search_items), threshold, tracking, existing=existing + new_items,
                        on_duplicate=_merge_query_entities)
    for item in new_items:
        item.summary = truncate(item.summary, settings["text"]["excerpt_max_chars"])

    # Exclusions and classification are cheap and rule-based: redo them on the whole
    # history so that keyword, alias or pattern changes apply retroactively.
    all_items = sort_items(classifier.classify_all(drop_excluded(existing + new_items, matcher, settings)))
    book = None
    if not dry_run:
        save_items(items_path, all_items, statuses, now)
        book = stories_mod.update(all_items, settings, ref, analyse=analyse, max_stories=analyse_max)

    return {
        "updated_at": now,
        "statuses": statuses,
        "new_items": new_items,
        "items": all_items,
        "ref": ref,
        "book": book,
        "display_days": settings["history"]["display_days"],
    }


# ---------------------------------------------------------------- report


def print_report(summary: dict[str, Any]) -> None:
    statuses: list[SourceStatus] = summary["statuses"]
    items: list[Item] = summary["items"]
    ref = summary["ref"]
    feeds = [s for s in statuses if s.via != GNEWS]
    searches = [s for s in statuses if s.via == GNEWS]

    print(f"\nMise à jour : {summary['updated_at']:%Y-%m-%d %H:%M %Z}")
    print(f"Flux éditeurs actifs : {sum(s.ok for s in feeds)}/{len(feeds)}   "
          f"Recherches Google News réussies : {sum(s.ok for s in searches)}/{len(searches)}")
    for s in feeds:
        if not s.ok:
            print(f"  [ERR] {s.name}: {s.error}")
    for s in searches:
        if not s.ok:
            print(f"  [ERR] {s.name}: {s.error}")
    print(f"Google News : {sum(s.fetched for s in searches)} lus, {sum(s.kept for s in searches)} gardés "
          f"(entité citée dans le titre + filtres)")

    new = summary["new_items"]
    print(f"\nNouveaux items : {len(new)} (dont {sum(i.via == GNEWS for i in new)} via Google News)"
          f"   Total historique : {len(items)}")

    def top(counter: Counter, labels: dict[str, str] | None = None, n: int = 12) -> str:
        return ", ".join(f"{(labels or {}).get(k, k)} {v}" for k, v in counter.most_common(n))

    for tab, label in (("politique", "POLITIQUE"), ("economie", "ÉCONOMIE")):
        tab_items = [i for i in items if tab in i.sections]
        print(f"\n== {label} : {len(tab_items)} items")
        ev = Counter(e for i in tab_items for e in i.events
                     if ref["events"][e]["section"] in (tab, "auto"))
        print("  Événements :", top(ev, {k: v["label"] for k, v in ref["events"].items()}))
        print("  Régions    :", top(Counter(r for i in tab_items for r in i.regions), ref["regions"]))
        print("  Pays       :", top(Counter(c for i in tab_items for c in i.countries),
                                    {k: v["name"] for k, v in ref["countries"].items()}))
        kind = "institution" if tab == "politique" else "company"
        ents = Counter(e for i in tab_items for e in i.entities if ref["entities"][e]["kind"] == kind)
        print("  Entités    :", top(ents, {k: v["name"] for k, v in ref["entities"].items()}, 15))

    if summary.get("book") is not None:
        stories_mod.print_report(summary["book"], items)

    unclassified = [i for i in items if not i.entities and not i.events]
    print(f"\nSans entité ni événement : {len(unclassified)} / {len(items)}")
    silent = [e["name"] for eid, e in ref["entities"].items() if not any(eid in i.entities for i in items)]
    print(f"Entités sans aucun article : {len(silent)}" + (f" ({', '.join(silent)})" if silent else ""))


def reclassify(settings: dict[str, Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Re-run classification on the stored history, without any network access."""
    settings = settings or load_settings()
    ref = load_reference(settings)
    path = resolve(settings["paths"]["items"])
    items, meta = load_items(path)
    matcher = KeywordMatcher(load_yaml(settings["paths"]["keywords"]))
    items = sort_items(Classifier(ref).classify_all(drop_excluded(items, matcher, settings)))
    statuses = [SourceStatus(**s) for s in meta.get("sources", [])]
    updated_at = datetime.fromisoformat(meta["updated_at"]) if meta.get("updated_at") else datetime.now()
    book = None
    if not dry_run:
        save_items(path, items, statuses, updated_at)
        book = stories_mod.update(items, settings, ref, analyse=False)
    return {"updated_at": updated_at, "statuses": statuses, "new_items": [], "items": items, "ref": ref,
            "book": book}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect, filter and classify news.")
    parser.add_argument("--dry-run", action="store_true", help="do not write data/items.json")
    parser.add_argument("--backfill", action="store_true", help="wider Google News window")
    parser.add_argument("--no-gnews", action="store_true", help="publisher feeds only")
    parser.add_argument("--no-analyse", action="store_true", help="group stories without calling Claude")
    parser.add_argument("--reclassify", action="store_true",
                        help="no download: re-run classification on data/items.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.reclassify:
        print_report(reclassify(dry_run=args.dry_run))
    else:
        print_report(run(dry_run=args.dry_run, backfill=args.backfill, use_gnews=not args.no_gnews,
                         analyse=not args.no_analyse))
    return 0


if __name__ == "__main__":
    sys.exit(main())
