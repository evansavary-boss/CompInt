"""Keyword filtering, domain tagging and deduplication."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .models import Item

# Simple plural forms accepted after a keyword ("lanceur" -> "lanceurs").
_PLURAL_SUFFIX = r"(?:s|x|es)?"


def normalize_text(text: str) -> str:
    """Lowercase, strip accents and unify apostrophes/whitespace."""
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.replace("’", "'").replace("‘", "'").replace("²", "2")
    return re.sub(r"\s+", " ", text).strip().lower()


def _phrase_regex(keywords: Iterable[str], case_sensitive: bool) -> re.Pattern | None:
    parts = []
    for kw in keywords:
        kw = kw.strip()
        if not kw:
            continue
        if not case_sensitive:
            kw = normalize_text(kw)
        # Allow any whitespace or hyphen between words of a phrase.
        tokens = [re.escape(t) for t in re.split(r"[\s-]+", kw)]
        parts.append(r"[\s-]+".join(tokens) + _PLURAL_SUFFIX)
    if not parts:
        return None
    # Longest first so that multi-word phrases win over their prefixes.
    parts.sort(key=len, reverse=True)
    pattern = r"(?<![\w])(?:" + "|".join(parts) + r")(?![\w])"
    return re.compile(pattern)


class KeywordMatcher:
    """Assign domain tags from config/keywords.yaml."""

    def __init__(self, config: dict[str, Any]):
        self.domains: dict[str, dict[str, re.Pattern | None]] = {}
        for domain, rules in (config.get("domains") or {}).items():
            rules = rules or {}
            self.domains[domain] = {
                "include": _phrase_regex(rules.get("include") or [], case_sensitive=False),
                "acronyms": _phrase_regex(rules.get("acronyms") or [], case_sensitive=True),
                "exclude": _phrase_regex(rules.get("exclude") or [], case_sensitive=False),
            }
        self.global_exclude = _phrase_regex(config.get("global_exclude") or [], case_sensitive=False)

    def is_excluded(self, text: str) -> bool:
        return bool(self.global_exclude and self.global_exclude.search(normalize_text(text)))

    def match(self, text: str) -> list[str]:
        """Return the domains whose keywords appear in `text` (config order)."""
        norm = normalize_text(text)
        found = []
        for domain, rx in self.domains.items():
            hit = (rx["include"] and rx["include"].search(norm)) or (
                rx["acronyms"] and rx["acronyms"].search(text or "")
            )
            if hit and not (rx["exclude"] and rx["exclude"].search(norm)):
                found.append(domain)
        return found


def tag_items(
    items: list[Item],
    matcher: KeywordMatcher,
    sources: dict[str, dict[str, Any]],
) -> list[Item]:
    """Tag items and drop those that are excluded or off-topic.

    Specialised sources (require_keywords: false) keep every item and fall back
    to their default domain; general sources only keep keyword matches.
    """
    kept = []
    for item in items:
        source = sources.get(item.source, {})
        if any(p in item.url for p in source.get("exclude_url_patterns") or []):
            continue
        text = f"{item.title} {item.summary}"
        if matcher.is_excluded(text):
            continue
        tags = matcher.match(text)
        if not tags:
            if source.get("require_keywords", True):
                continue
            tags = [source["domain"]] if source.get("domain") else []
        item.tags = tags
        kept.append(item)
    return kept


def canonical_url(url: str, tracking_params: Iterable[str] = ()) -> str:
    """Normalise a URL for duplicate detection."""
    parts = urlsplit((url or "").strip())
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    drop = {p.lower() for p in tracking_params}
    query = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in drop and not k.lower().startswith("utm_")
    ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(sorted(query)), ""))


def _title_key(title: str) -> str:
    return re.sub(r"[^\w ]", " ", normalize_text(title)).strip()


def titles_similar(a: str, b: str, threshold: float) -> bool:
    ka, kb = _title_key(a), _title_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    # Cheap word-overlap guard before the more expensive ratio.
    wa, wb = set(ka.split()), set(kb.split())
    if len(wa & wb) / max(len(wa | wb), 1) < 0.4:
        return False
    return SequenceMatcher(None, ka, kb).ratio() >= threshold


def dedupe(
    items: list[Item],
    threshold: float,
    tracking_params: Iterable[str] = (),
    existing: Iterable[Item] = (),
) -> list[Item]:
    """Drop duplicates, first by canonical URL then by near-identical title.

    Items in `existing` win over new ones; among new items, the first one wins
    (callers sort oldest first so the original publication is kept).
    """
    tracking_params = list(tracking_params)
    seen_urls = {canonical_url(i.url, tracking_params) for i in existing}
    seen_titles = [i.title for i in existing]
    result = []
    for item in items:
        cu = canonical_url(item.url, tracking_params)
        if cu in seen_urls:
            continue
        if any(titles_similar(item.title, t, threshold) for t in seen_titles):
            continue
        seen_urls.add(cu)
        seen_titles.append(item.title)
        result.append(item)
    return result
