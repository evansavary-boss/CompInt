"""Fetch RSS feeds, then filter, tag, dedupe, enrich and store items.

Usage:
    python -m src.collect             # update data/items.json and print a report
    python -m src.collect --dry-run   # same, without writing anything
"""

from __future__ import annotations

import argparse
import hashlib
import html
import logging
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import feedparser
import requests

from .config import load_settings, load_yaml, resolve
from .enrich import get_enricher
from .filter import KeywordMatcher, canonical_url, dedupe, tag_items
from .models import Item, SourceStatus
from .store import load_items, prune, save_items, sort_items

log = logging.getLogger("collect")

_TAG_RE = re.compile(r"<[^>]+>")


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
    status = SourceStatus(name=source["name"], url=source["url"], ok=False)
    try:
        feed = fetch_feed(source["url"], settings["http"])
    except Exception as exc:  # never let one feed break the run
        status.error = _short_error(exc)
        log.warning("%s: %s", source["name"], status.error)
        return [], status

    if not feed.entries:
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
        raw_summary = entry.get("summary") or entry.get("description") or ""
        items.append(
            Item(
                id=item_id(link, tracking),
                title=title,
                url=link,
                source=source["name"],
                section=source["section"],
                published=published.isoformat(timespec="seconds"),
                summary=clean_html(raw_summary, strip_patterns, max_raw),
                first_seen=now.isoformat(timespec="seconds"),
                date_estimated=estimated,
            )
        )
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


def run(settings: dict[str, Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Full news update. Returns a summary used by the CLI report and build."""
    settings = settings or load_settings()
    paths = settings["paths"]
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz).replace(microsecond=0)

    sources = load_yaml(paths["sources"])["sources"]
    sources_by_name = {s["name"]: s for s in sources}
    matcher = KeywordMatcher(load_yaml(paths["keywords"]))
    strip_patterns = [
        re.compile(p, re.IGNORECASE) for p in settings["text"].get("summary_strip_patterns", [])
    ]

    with ThreadPoolExecutor(max_workers=settings["http"]["max_workers"]) as pool:
        results = list(
            pool.map(lambda s: collect_source(s, settings, tz, now, strip_patterns), sources)
        )

    retention = settings["history"]["retention_days"]
    tracking = settings["dedup"]["tracking_params"]
    threshold = settings["dedup"]["title_similarity"]

    items_path = resolve(paths["items"])
    existing, _ = load_items(items_path)
    existing = prune(existing, now, retention)

    statuses = []
    new_items: list[Item] = []
    for items, status in results:
        tagged = tag_items(items, matcher, sources_by_name)
        status.kept = len(tagged)
        statuses.append(status)
        new_items.extend(tagged)

    new_items = prune(new_items, now, retention)
    # Oldest first so the original publication survives deduplication.
    new_items.sort(key=lambda i: datetime.fromisoformat(i.published))
    new_items = dedupe(new_items, threshold, tracking, existing=existing)
    new_items = get_enricher(settings).enrich(new_items)

    all_items = sort_items(existing + new_items)
    if not dry_run:
        save_items(items_path, all_items, statuses, now)

    return {
        "updated_at": now,
        "statuses": statuses,
        "new_items": new_items,
        "items": all_items,
        "display_days": settings["history"]["display_days"],
    }


def print_report(summary: dict[str, Any]) -> None:
    statuses: list[SourceStatus] = summary["statuses"]
    items: list[Item] = summary["items"]
    ok = [s for s in statuses if s.ok]
    print(f"\nMise à jour : {summary['updated_at']:%Y-%m-%d %H:%M %Z}")
    print(f"Sources actives : {len(ok)}/{len(statuses)}\n")
    width = max(len(s.name) for s in statuses)
    for s in statuses:
        state = "OK " if s.ok else "ERR"
        detail = f"{s.fetched:3d} lus  {s.kept:3d} gardés" if s.ok else s.error
        print(f"  [{state}] {s.name:<{width}}  {detail}")

    print(f"\nNouveaux items : {len(summary['new_items'])}   Total historique : {len(items)}")
    for section in ("politique", "entreprises"):
        sec = [i for i in items if i.section == section]
        geo = sum("geospatial" in i.tags for i in sec)
        ns = sum("newspace" in i.tags for i in sec)
        print(f"\n== {section.upper()} : {len(sec)} items (géospatial {geo}, new space {ns})")
        for i in sec[:8]:
            tags = ",".join("GEO" if t == "geospatial" else "NS" for t in i.tags)
            print(f"  {i.published[:16].replace('T', ' ')}  [{tags:<6}] {i.title[:90]}  ({i.source})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect and filter RSS feeds.")
    parser.add_argument("--dry-run", action="store_true", help="do not write data/items.json")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_report(run(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
