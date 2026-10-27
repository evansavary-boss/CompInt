"""Fetch publisher RSS feeds and Google News searches, then filter, dedupe, enrich,
classify and store items.

Usage:
    python -m src.collect               # update data/items.json and print a report
    python -m src.collect --dry-run     # same, without writing anything
    python -m src.collect --backfill    # wider Google News window (first fill)
    python -m src.collect --backfill --entities e-geos,gaf   # Google News of new entities only
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
from .textmatch import TermIndex

log = logging.getLogger("collect")

_TAG_RE = re.compile(r"<[^>]+>")
GNEWS = "gnews"
GDELT = "gdelt"
SEARCHES = (GNEWS, GDELT)


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


def geo_sources(gcfg: dict[str, Any], settings: dict[str, Any], backfill: bool = False,
                use_searches: bool = True) -> list[dict[str, Any]]:
    """Geopolitics feeds, thematic Google News searches and GDELT queries."""
    out = [{**f, "section": "geopolitique", "gate": "geo", "via": ""} for f in gcfg.get("feeds") or []]
    if not use_searches:
        return out
    sset = gcfg.get("search_settings") or {}
    when = sset.get("backfill_when" if backfill else "when", "2d")
    cfg = settings["google_news"]
    for s in gcfg.get("searches") or []:
        params = {"q": f"{s['query']} when:{when}", **cfg["editions"][s["edition"]]}
        out.append({
            "name": f"Google News · {s['theme']} · {s['query'][:50]} ({s['edition']})",
            "url": f"{cfg['base_url']}?{urlencode(params)}",
            "section": "geopolitique", "gate": "geo", "theme": s["theme"], "via": GNEWS,
            "max_items": sset.get("max_items_per_query", cfg["max_items_per_query"]),
        })
    g = gcfg.get("gdelt") or {}
    if g.get("enabled"):
        for q in g.get("queries") or []:
            params = {"query": q["query"], "mode": "artlist", "format": "json", "sort": "datedesc",
                      "maxrecords": g["max_records"], "timespan": "14d" if backfill else g["timespan"]}
            out.append({
                "name": f"GDELT · {q['theme']}", "url": f"{g['url']}?{urlencode(params)}",
                "section": "geopolitique", "gate": "geo", "theme": q["theme"], "via": GDELT,
                "delay": g.get("delay_seconds", 12), "retry_after": g.get("retry_after_seconds", 20),
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
    cap = source.get("max_items") or (settings["google_news"]["max_items_per_query"] if is_gnews else None)
    if cap:
        items.sort(key=lambda i: datetime.fromisoformat(i.published), reverse=True)
        items = items[:cap]
    status.ok = True
    status.fetched = len(items)
    return items, status


def collect_gdelt(source: dict[str, Any], settings: dict[str, Any], tz: ZoneInfo,
                  now: datetime) -> tuple[list[Item], SourceStatus]:
    """GDELT DOC API article list (JSON). Rate-limited: callers space the requests."""
    status = SourceStatus(name=source["name"], url=source["url"], ok=False, via=GDELT)
    try:
        for attempt in range(2):
            resp = requests.get(source["url"], headers={"User-Agent": settings["http"]["user_agent"]},
                                timeout=settings["http"]["timeout_seconds"] * 2)
            if resp.status_code != 429 or attempt:
                break
            time.sleep(source.get("retry_after", 20))
        resp.raise_for_status()
        articles = resp.json().get("articles", [])
    except ValueError:
        status.error = "réponse non JSON (limite de requêtes GDELT ?)"
        log.warning("%s: %s", source["name"], status.error)
        return [], status
    except Exception as exc:  # never let one source break the run
        status.error = _short_error(exc)
        log.warning("%s: %s", source["name"], status.error)
        return [], status

    tracking = settings["dedup"]["tracking_params"]
    items = []
    for art in articles:
        url, title = art.get("url"), clean_html(art.get("title", ""))
        if not url or not title:
            continue
        try:
            when = datetime.strptime(art.get("seendate", ""), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            published, estimated = min(when.astimezone(tz), now), False
        except ValueError:
            published, estimated = now, True
        items.append(Item(
            id=item_id(url, tracking), title=title, url=url, source=art.get("domain") or "GDELT",
            section=source["section"], published=published.isoformat(timespec="seconds"), summary="",
            first_seen=now.isoformat(timespec="seconds"), date_estimated=estimated, via=GDELT,
        ))
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
    """Publisher feeds in parallel (distinct hosts); Google News and GDELT one request at a time."""
    feeds = [s for s in sources if s.get("via") not in SEARCHES]
    searches = [s for s in sources if s.get("via") == GNEWS]
    gdelt = [s for s in sources if s.get("via") == GDELT]
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
    for n, src in enumerate(gdelt):
        if n:
            time.sleep(src.get("delay", 8))
        items, status = collect_gdelt(src, settings, tz, now)
        results.append((src, items, status))
    return results


# ---------------------------------------------------------------- pipeline


def geo_exclusions(gcfg: dict[str, Any]) -> TermIndex:
    index = TermIndex(plurals=False)
    index.add_many(gcfg.get("exclude"), "x")
    return index


def geo_gate(items: list[Item], src: dict[str, Any], themes: TermIndex, exclude: TermIndex,
             matcher: KeywordMatcher) -> list[Item]:
    """Keep geopolitics items whose wording matches a family; set their families and domain tags.

    Searches match on the title only (GDELT and Google News also search article bodies);
    specialised publisher feeds (with a `theme`) keep their items under that family by default.
    """
    rx = re.compile(src["title_include_regex"]) if src.get("title_include_regex") else None
    kept = []
    for it in items:
        if rx and not rx.search(it.title):
            continue
        if exclude.targets(it.title) or matcher.is_excluded(f"{it.title} {it.summary}"):
            continue
        found = themes.targets(it.title if (it.via or src.get("title_only")) else f"{it.title} {it.summary}")
        if not found:
            if src.get("theme") and not it.via:
                found = [src["theme"]]
            else:
                continue
        it.geo_themes = found
        it.tags = matcher.match(f"{it.title} {it.summary}")
        kept.append(it)
    return kept


def drop_excluded(items: list[Item], matcher: KeywordMatcher, settings: dict[str, Any],
                  geo_exclude: TermIndex | None = None) -> list[Item]:
    """Remove items hit by `global_exclude`, by Google News `title_exclude_regex`, or (geopolitics
    items) by the geopolitics exclusions. Also applied to stored items, so new exclusions clean
    the history.
    """
    title_rx = [re.compile(p) for p in settings["google_news"].get("title_exclude_regex") or []]
    return [i for i in items
            if not matcher.is_excluded(f"{i.title} {i.summary}")
            and not (i.via == GNEWS and any(rx.search(i.title) for rx in title_rx))
            and not (geo_exclude and i.section == "geopolitique" and geo_exclude.targets(i.title))]


def _merge_query_entities(kept: Item, dropped: Item) -> None:
    for eid in dropped.query_entities:
        if eid not in kept.query_entities:
            kept.query_entities.append(eid)


def run(settings: dict[str, Any] | None = None, dry_run: bool = False,
        backfill: bool = False, use_gnews: bool = True, analyse: bool = True,
        analyse_max: int | None = None, only_entities: list[str] | None = None) -> dict[str, Any]:
    """Full news update. Returns a summary used by the CLI report and build.

    `only_entities` limits the download to the Google News searches of these entities (to fill
    the history of newly added ones); the other sources keep their previous status."""
    settings = settings or load_settings()
    paths = settings["paths"]
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz).replace(microsecond=0)

    ref = load_reference(settings)
    gcfg = load_yaml(paths["geopolitics"])
    classifier = Classifier(ref, gcfg)
    geo_exclude = geo_exclusions(gcfg)
    if only_entities:
        unknown = set(only_entities) - set(ref["entities"])
        if unknown:
            raise ValueError(f"entités inconnues : {', '.join(sorted(unknown))}")
        sources = [s for s in gnews_sources(ref, settings, backfill) if s["entity"] in only_entities]
    else:
        sources = load_yaml(paths["sources"])["sources"]
        if use_gnews:
            sources = sources + gnews_sources(ref, settings, backfill)
        sources = sources + geo_sources(gcfg, settings, backfill, use_searches=use_gnews)
    matcher = KeywordMatcher(load_yaml(paths["keywords"]))
    strip_patterns = [
        re.compile(p, re.IGNORECASE) for p in settings["text"].get("summary_strip_patterns", [])
    ]

    results = fetch_all(sources, settings, tz, now, strip_patterns)

    retention = settings["history"]["retention_days"]
    tracking = settings["dedup"]["tracking_params"]
    threshold = settings["dedup"]["title_similarity"]

    items_path = resolve(paths["items"])
    existing, meta = load_items(items_path)
    existing = prune(existing, now, retention)

    statuses = []
    feed_items: list[Item] = []
    search_items: list[Item] = []
    for src, items, status in results:
        if src.get("gate") == "geo":
            tagged = geo_gate(items, src, classifier.geo_themes, geo_exclude, matcher)
        else:
            tagged = tag_items(items, matcher, source=src)
        if src.get("entity"):
            # The searched entity must appear in the title: Google News also matches
            # on article bodies, which brings in many loosely related stories.
            tagged = [i for i in tagged if src["entity"] in classifier.find_entities(i.title)]
        if src.get("via") in SEARCHES:
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
    all_items = sort_items(classifier.classify_all(drop_excluded(existing + new_items, matcher, settings, geo_exclude)))
    saved_statuses = statuses
    if only_entities:
        fetched = {s.name for s in statuses}
        saved_statuses = statuses + [SourceStatus(**s) for s in meta.get("sources", []) if s["name"] not in fetched]
    book = None
    if not dry_run:
        save_items(items_path, all_items, saved_statuses, now)
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
    feeds = [s for s in statuses if s.via not in SEARCHES]
    searches = [s for s in statuses if s.via == GNEWS]
    gdelt = [s for s in statuses if s.via == GDELT]

    print(f"\nMise à jour : {summary['updated_at']:%Y-%m-%d %H:%M %Z}")
    print(f"Flux éditeurs actifs : {sum(s.ok for s in feeds)}/{len(feeds)}   "
          f"Recherches Google News réussies : {sum(s.ok for s in searches)}/{len(searches)}   "
          f"GDELT : {sum(s.ok for s in gdelt)}/{len(gdelt)}")
    for s in feeds + searches + gdelt:
        if not s.ok:
            print(f"  [ERR] {s.name}: {s.error}")
    if gdelt:
        print(f"GDELT : {sum(s.fetched for s in gdelt)} lus, {sum(s.kept for s in gdelt)} gardés (titre filtré)")
    print(f"Google News : {sum(s.fetched for s in searches)} lus, {sum(s.kept for s in searches)} gardés "
          f"(entité citée dans le titre + filtres)")

    new = summary["new_items"]
    print(f"\nNouveaux items : {len(new)} (dont {sum(i.via == GNEWS for i in new)} via Google News)"
          f"   Total historique : {len(items)}")

    def top(counter: Counter, labels: dict[str, str] | None = None, n: int = 12) -> str:
        return ", ".join(f"{(labels or {}).get(k, k)} {v}" for k, v in counter.most_common(n))

    geo = [i for i in items if "geopolitique" in i.sections]
    print(f"\n== GÉOPOLITIQUE : {len(geo)} items (sources géopolitiques)")
    print("  Familles   :", top(Counter(t for i in geo for t in i.geo_themes)))
    print("  Pays       :", top(Counter(c for i in geo for c in i.countries),
                                {k: v["name"] for k, v in ref["countries"].items()}, 15))

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
    gcfg = load_yaml(settings["paths"]["geopolitics"])
    items = sort_items(Classifier(ref, gcfg).classify_all(
        drop_excluded(items, matcher, settings, geo_exclusions(gcfg))))
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
    parser.add_argument("--entities", default="",
                        help="comma-separated entity ids: their Google News searches only (new entities)")
    parser.add_argument("--reclassify", action="store_true",
                        help="no download: re-run classification on data/items.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.reclassify:
        print_report(reclassify(dry_run=args.dry_run))
    else:
        only = [e.strip() for e in args.entities.split(",") if e.strip()] or None
        print_report(run(dry_run=args.dry_run, backfill=args.backfill, use_gnews=not args.no_gnews,
                         analyse=not args.no_analyse, only_entities=only))
    return 0


if __name__ == "__main__":
    sys.exit(main())
