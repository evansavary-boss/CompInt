"""Public tenders: TED (EU) and Find a Tender (UK), filtered for geospatial and analysed by Claude.

TED is queried by CPV code and full-text expressions; Find a Tender has no search, so every
notice of each covered day is read and filtered here. Notices are kept for a year in
data/tenders.json with their Claude analysis (relevance, segment, French summary, fit with the
Airbus offer). A failing source never fails the build: the previous notices are kept.

Usage:
    python -m src.tenders                # fetch recent notices, analyse new ones, print a report
    python -m src.tenders --backfill     # first fill (backfill_days in config/tenders.yaml)
    python -m src.tenders --no-analyse   # rules only, no Claude call
    python -m src.tenders --offline      # no network: re-apply the rules to stored notices
    python -m src.tenders --max 200      # raise the analysis cap for this run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

import requests

from .config import load_settings, load_yaml, resolve
from .enrich import AnalysisError, get_analyzer, truncate
from .models import SourceStatus
from .textmatch import TermIndex, fold

log = logging.getLogger("tenders")

TED, FTS = "ted", "fts"
SOURCE_NAMES = {TED: "TED", FTS: "Find a Tender"}
VIA = "tender"
TED_FIELDS = [
    "publication-number", "notice-type", "notice-title", "title-proc", "description-proc", "buyer-name",
    "buyer-country", "place-of-performance", "publication-date", "classification-cpv",
    "deadline-receipt-tender-date-lot", "deadline-receipt-request-date-lot",
    "estimated-value-proc", "estimated-value-cur-proc", "estimated-value-lot", "estimated-value-cur-lot",
    "total-value", "total-value-cur", "result-value-notice", "result-value-cur-notice", "winner-name",
]
TED_LANGS = ("eng", "fra")          # preferred language of multilingual TED fields
INSTRUCTION = "Analyse les avis de marchés publics fournis en entrée (JSON) selon tes instructions."


@dataclass
class Tender:
    id: str                    # "ted:<publication number>" or "fts:<ocid>:<kind>"
    source: str                # ted | fts
    kind: str                  # open | planned | award
    notice: str                # notice number shown to users
    notice_type: str           # TED notice type or Find a Tender notice code
    title: str                 # object of the notice, in the buyer's language
    buyer: str
    buyer_country: str         # ISO alpha-2
    published: str             # YYYY-MM-DD
    url: str
    cpv_label: str = ""        # French label of the main CPV code (TED)
    countries: list[str] = field(default_factory=list)    # place of performance, ISO alpha-2
    cpv: list[str] = field(default_factory=list)
    deadline: str = ""         # YYYY-MM-DD, latest lot deadline
    value: float | None = None # estimated value (open, planned) or awarded value (award)
    currency: str = ""
    winners: list[str] = field(default_factory=list)
    description: str = ""      # beginning of the description, for the analysis
    status: str = ""           # "cancelled" when the buyer withdrew it
    first_seen: str = ""
    analysis: dict[str, Any] = field(default_factory=dict)
    analysed_at: str = ""
    analysis_model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tender":
        return cls(**{k: data[k] for k in cls.__dataclass_fields__ if k in data})


class TenderBook:
    """Stored notices (data/tenders.json) and the metadata of the last runs."""

    def __init__(self, tenders: dict[str, Tender] | None = None, meta: dict[str, Any] | None = None):
        self.tenders = tenders or {}
        self.meta = meta or {}

    @classmethod
    def load(cls, path: Path) -> "TenderBook":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("unreadable %s, starting empty", path)
            return cls()
        tenders = {d["id"]: Tender.from_dict(d) for d in data.get("tenders", [])}
        return cls(tenders, {k: v for k, v in data.items() if k != "tenders"})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(self.tenders.values(), key=lambda t: (t.published, t.id), reverse=True)
        payload = {**self.meta, "tenders": [t.to_dict() for t in rows]}
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def upsert(self, tender: Tender, now: datetime) -> bool:
        """Add or refresh a notice; its first-seen date and analysis survive updates."""
        old = self.tenders.get(tender.id)
        if old is None:
            tender.first_seen = now.isoformat(timespec="seconds")
            self.tenders[tender.id] = tender
            return True
        tender.first_seen = old.first_seen
        tender.analysis, tender.analysed_at, tender.analysis_model = old.analysis, old.analysed_at, old.analysis_model
        tender.status = tender.status or old.status
        self.tenders[tender.id] = tender
        return False

    def cancel(self, tender_id: str) -> None:
        if tender_id in self.tenders:
            self.tenders[tender_id].status = "cancelled"

    def prune(self, today: date, retention_days: int) -> int:
        cutoff = (today - timedelta(days=retention_days)).isoformat()
        old = [k for k, t in self.tenders.items() if t.published < cutoff]
        for k in old:
            del self.tenders[k]
        return len(old)


# ---------------------------------------------------------------- rules


class Rules:
    """Relevance, segment and entity matching, all from config/tenders.yaml and the reference."""

    def __init__(self, tcfg: dict[str, Any], ref: dict[str, Any], competitors: dict[str, Any]):
        rel = tcfg["relevance"]
        self.strong = TermIndex(plurals=False)
        self.strong.add_many(rel.get("strong"), "strong")
        self.strong.add_many(rel.get("strong_cs"), "strong", case_sensitive=True)
        self.exclude = TermIndex(plurals=False)
        self.exclude.add_many(rel.get("exclude"), "exclude")
        self.fts_candidates = TermIndex(plurals=True)
        self.fts_candidates.add_many((tcfg.get("fts") or {}).get("candidates"), "candidate")
        self.segments: list[tuple[str, TermIndex]] = []
        for key, seg in tcfg["segments"].items():
            index = TermIndex(plurals=False)
            index.add_many(seg.get("patterns"), key)
            self.segments.append((key, index))
        self.default_segment = tcfg["default_segment"]
        self.companies = TermIndex(plurals=False)
        self.institutions = TermIndex(plurals=False)
        for eid, e in ref["entities"].items():
            index = self.companies if e["kind"] == "company" else self.institutions
            index.add_many(e.get("aliases"), eid)
            index.add_many(e.get("aliases_cs"), eid, case_sensitive=True)
        self.own = set(competitors.get("own") or [])
        self.rivals = {eid: key for key, g in (competitors.get("groups") or {}).items()
                       for eid in g.get("entities") or []}
        self.a3 = {c["a3"]: code for code, c in ref["countries"].items() if c.get("a3")}

    @staticmethod
    def text(t: Tender) -> str:
        return f"{t.title} {t.description}"

    def excluded(self, t: Tender) -> bool:
        return bool(self.exclude.targets(f"{t.title} {t.buyer}"))

    def is_strong(self, t: Tender) -> bool:
        return bool(self.strong.targets(self.text(t)))

    def fts_candidate(self, title: str, description: str, cpv: Iterable[str], prefixes: Iterable[str]) -> bool:
        if any(c.startswith(tuple(prefixes)) for c in cpv):
            return True
        return bool(self.fts_candidates.targets(f"{title} {description}"))

    def segment(self, t: Tender) -> str:
        text = self.text(t)
        for key, index in self.segments:
            if index.targets(text):
                return key
        return self.default_segment

    def visible(self, t: Tender) -> bool:
        """Shown on the site: Claude said relevant, or not analysed yet and clearly geospatial."""
        if self.excluded(t):
            return False
        if t.analysis:
            return bool(t.analysis.get("relevant"))
        return self.is_strong(t)

    def winner_rows(self, t: Tender) -> list[dict[str, str]]:
        rows = []
        for name in t.winners:
            ids = self.companies.targets(name)
            eid = ids[0] if ids else ""
            role = "own" if eid in self.own else ("rival" if eid in self.rivals else "")
            rows.append({"name": name, "id": eid, "role": role})
        return rows

    def buyer_entity(self, t: Tender) -> str:
        ids = self.institutions.targets(t.buyer)
        return ids[0] if ids else ""

    def country(self, a3: str) -> str:
        return self.a3.get((a3 or "").upper(), "")


def validate_config(tcfg: dict[str, Any], competitors: dict[str, Any], ref: dict[str, Any],
                    acfg: dict[str, Any]) -> list[str]:
    errors = []
    known = {eid for eid, e in ref["entities"].items() if e["kind"] == "company"}
    for eid in list(competitors.get("own") or []) + [e for g in (competitors.get("groups") or {}).values()
                                                      for e in g.get("entities") or []]:
        if eid not in known:
            errors.append(f"competitors.yaml : entreprise inconnue {eid!r}")
    for key, move in (competitors.get("moves") or {}).items():
        errors += [f"competitors.yaml moves.{key} : type d'événement inconnu {e!r}"
                   for e in move.get("events") or [] if e not in ref["events"]]
        errors += [f"competitors.yaml moves.{key} : thème inconnu {t!r}"
                   for t in move.get("themes") or [] if t not in acfg["themes"]]
    if tcfg["default_segment"] not in tcfg["segments"]:
        errors.append(f"tenders.yaml : default_segment {tcfg['default_segment']!r} absent de segments")
    kinds = set(tcfg["kinds"])
    for where, table in (("ted.notice_types", tcfg["ted"]["notice_types"]), ("fts.tags", tcfg["fts"]["tags"])):
        errors += [f"tenders.yaml {where} : nature inconnue {v!r}" for v in table.values() if v not in kinds]
    for key in tcfg["display"].get("press_themes") or []:
        if key not in acfg["themes"]:
            errors.append(f"tenders.yaml display.press_themes : thème inconnu {key!r}")
    for key in tcfg["display"].get("press_events") or []:
        if key not in ref["events"]:
            errors.append(f"tenders.yaml display.press_events : type d'événement inconnu {key!r}")
    return errors


# ---------------------------------------------------------------- helpers


def _headers(settings: dict[str, Any]) -> dict[str, str]:
    return {"User-Agent": settings["http"]["user_agent"]}


def _lang_value(value: Any) -> Any:
    """Pick the preferred language of a TED multilingual field ({lang: text or [texts]})."""
    if not isinstance(value, dict):
        return value
    for lang in TED_LANGS:
        if value.get(lang):
            return value[lang]
    return next(iter(value.values()), None)


def _first_text(value: Any) -> str:
    value = _lang_value(value)
    if isinstance(value, list):
        value = next((v for v in value if v), "")
    return str(value or "").strip()


def _all_texts(value: Any) -> list[str]:
    value = _lang_value(value)
    values = value if isinstance(value, list) else [value]
    out = []
    for v in values:
        v = str(v or "").strip()
        if v and v not in out:
            out.append(v)
    return out


def _amount(value: Any) -> float | None:
    """Sum of a TED amount field (a number or one number per lot)."""
    values = value if isinstance(value, list) else [value]
    total, seen = 0.0, False
    for v in values:
        try:
            total += float(v)
            seen = True
        except (TypeError, ValueError):
            continue
    return total if seen and total > 0 else None


def _uniq(values: Iterable[str]) -> list[str]:
    out = []
    for v in values:
        if v and v not in out:
            out.append(v)
    return out


# ---------------------------------------------------------------- TED


def ted_query(cfg: dict[str, Any], since: date) -> str:
    cpv = " ".join(str(c) for c in cfg["cpv"])
    terms = " OR ".join(f'"{p}"' for p in cfg["fulltext"])
    return f"(classification-cpv IN ({cpv}) OR FT~({terms})) AND publication-date>={since:%Y%m%d}"


def fetch_ted(cfg: dict[str, Any], settings: dict[str, Any], since: date) -> list[dict[str, Any]]:
    """Every notice matching the query, page by page (raises on network or API errors)."""
    notices: list[dict[str, Any]] = []
    query = ted_query(cfg, since)
    timeout = settings["http"]["timeout_seconds"] * 3
    for page in range(1, cfg["max_pages"] + 1):
        if page > 1:
            time.sleep(cfg["delay_seconds"])
        body = {"query": query, "fields": TED_FIELDS, "limit": cfg["page_size"], "page": page, "scope": "ALL"}
        for attempt in range(2):
            try:
                resp = requests.post(cfg["url"], json=body, headers=_headers(settings), timeout=timeout)
                break
            except requests.ConnectionError:
                if attempt:
                    raise
                time.sleep(cfg["delay_seconds"] * 5)   # TED resets bursts of connections
        if resp.status_code != 200:
            raise requests.HTTPError(f"HTTP {resp.status_code} : {resp.text[:160]}")
        batch = resp.json().get("notices") or []
        notices += batch
        if len(batch) < cfg["page_size"]:
            break
    return notices


def parse_ted(n: dict[str, Any], cfg: dict[str, Any], rules: Rules, desc_chars: int) -> Tender | None:
    kind = cfg["notice_types"].get(n.get("notice-type", ""))
    number = n.get("publication-number", "")
    if not kind or not number:
        return None
    # notice-title reads "Country – CPV label – object of the notice" in every EU language.
    titles = n.get("notice-title") or {}
    parts = (_first_text(titles)).split(" – ", 2)
    title = parts[2] if len(parts) == 3 else _first_text(n.get("title-proc")) or parts[-1]
    fr = str(titles.get("fra", "")).split(" – ", 2) if isinstance(titles, dict) else []
    countries = _uniq(rules.country(c) for c in n.get("place-of-performance") or [])
    buyer_country = next((rules.country(c) for c in n.get("buyer-country") or [] if rules.country(c)), "")
    deadlines = [str(d)[:10] for d in (n.get("deadline-receipt-tender-date-lot")
                                       or n.get("deadline-receipt-request-date-lot") or []) if d]
    if kind == "award":
        value = _amount(n.get("total-value")) or _amount(n.get("result-value-notice"))
        currency = _first_text(n.get("total-value-cur")) or _first_text(n.get("result-value-cur-notice"))
    else:
        value = _amount(n.get("estimated-value-proc")) or _amount(n.get("estimated-value-lot"))
        currency = _first_text(n.get("estimated-value-cur-proc")) or _first_text(n.get("estimated-value-cur-lot"))
    return Tender(
        id=f"{TED}:{number}",
        source=TED,
        kind=kind,
        notice=number,
        notice_type=n.get("notice-type", ""),
        title=title.strip(),
        buyer=_first_text(n.get("buyer-name")),
        buyer_country=buyer_country,
        published=str(n.get("publication-date", ""))[:10],
        url=cfg["notice_url"].format(id=number),
        cpv_label=fr[1].strip() if len(fr) == 3 else "",
        countries=countries or ([buyer_country] if buyer_country else []),
        cpv=_uniq(str(c) for c in n.get("classification-cpv") or []),
        deadline=max(deadlines) if deadlines else "",
        value=value,
        currency=currency.upper() if value else "",
        winners=_all_texts(n.get("winner-name")),
        description=truncate(_first_text(n.get("description-proc")), desc_chars),
    )


# ---------------------------------------------------------------- Find a Tender


def _fts_cpv(release: dict[str, Any]) -> list[str]:
    t = release.get("tender") or {}
    codes = [(t.get("classification") or {}).get("id", "")]
    for it in (t.get("items") or []) + [i for a in release.get("awards") or [] for i in a.get("items") or []]:
        codes += [c.get("id", "") for c in it.get("additionalClassifications") or [] if c.get("scheme") == "CPV"]
    return _uniq(codes)


def _fts_value(v: dict[str, Any] | None) -> tuple[float | None, str]:
    v = v or {}
    amount = v.get("amount") if v.get("amount") is not None else v.get("amountGross")
    try:
        amount = float(amount)
    except (TypeError, ValueError):
        return None, ""
    return (amount, str(v.get("currency") or "").upper()) if amount > 0 else (None, "")


def parse_fts(release: dict[str, Any], cfg: dict[str, Any], rules: Rules, desc_chars: int) -> list[Tender]:
    """Tenders of one OCDS release; a cancellation comes back as an open notice with status cancelled."""
    tags = release.get("tag") or []
    kinds = {cfg["tags"][t] for t in tags if t in cfg["tags"]}
    cancelled = "tenderCancellation" in tags
    if cancelled:
        kinds.add("open")
    if not kinds:
        return []
    kind = next(k for k in ("award", "open", "planned") if k in kinds)
    t = release.get("tender") or {}
    title = str(t.get("title") or "").strip()
    description = str(t.get("description") or "")
    cpv = _fts_cpv(release)
    if not cancelled and not rules.fts_candidate(title, description, cpv, cfg["cpv_prefixes"]):
        return []
    awards = release.get("awards") or []
    docs = (t.get("documents") or []) + ((release.get("planning") or {}).get("documents") or []) \
        + [d for a in awards for d in a.get("documents") or []]
    docs = sorted((d for d in docs if d.get("url")), key=lambda d: d.get("datePublished", ""), reverse=True)
    if kind == "award":
        values = [_fts_value(a.get("value")) for a in awards]
        amounts = [v for v, _ in values if v]
        value, currency = (sum(amounts), next(c for v, c in values if v)) if amounts else (None, "")
        winners = _uniq(str(s.get("name") or "").strip() for a in awards for s in a.get("suppliers") or [])
    else:
        value, currency = _fts_value(t.get("value"))
        winners = []
    buyer = release.get("buyer") or {}
    country = next(((p.get("address") or {}).get("country", "") for p in release.get("parties") or []
                    if "buyer" in (p.get("roles") or []) and (p.get("address") or {}).get("country")), "")
    country = (country or cfg["country"]).upper()
    end = ((t.get("tenderPeriod") or {}).get("endDate") or "")[:10]
    return [Tender(
        id=f"{FTS}:{release.get('ocid', '')}:{kind}",
        source=FTS,
        kind=kind,
        notice=docs[0].get("id", "") if docs else release.get("id", ""),
        notice_type=docs[0].get("noticeType", "") if docs else "",
        title=title,
        buyer=str(buyer.get("name") or "").strip(),
        buyer_country=country,
        published=str(release.get("date", ""))[:10],
        url=docs[0]["url"] if docs else "",
        countries=[country],
        cpv=cpv,
        deadline=end if kind == "open" else "",
        value=value,
        currency=currency,
        winners=winners,
        description=truncate(description, desc_chars),
        status="cancelled" if cancelled else "",
    )]


def _fts_day(cfg: dict[str, Any], settings: dict[str, Any], rules: Rules, day: datetime, nxt: datetime,
             desc_chars: int, wait_first: bool) -> tuple[list[Tender], int]:
    """Every release updated in [day, nxt), page by page (raises on network or API errors)."""
    kept: list[Tender] = []
    scanned = 0
    timeout = settings["http"]["timeout_seconds"] * 3
    url: str | None = cfg["url"]
    params: dict[str, Any] | None = {"updatedFrom": f"{day:%Y-%m-%dT%H:%M:%S}",
                                     "updatedTo": f"{nxt:%Y-%m-%dT%H:%M:%S}", "limit": cfg["page_size"]}
    for page in range(cfg["max_pages_per_day"]):
        if not url:
            break
        if page or wait_first:
            time.sleep(cfg["delay_seconds"])
        resp = requests.get(url, params=params, headers=_headers(settings), timeout=timeout)
        if resp.status_code == 429:   # 12 requests per minute: wait once, then retry
            time.sleep(cfg["retry_after_seconds"])
            resp = requests.get(url, params=params, headers=_headers(settings), timeout=timeout)
        if resp.status_code != 200:
            raise requests.HTTPError(f"HTTP {resp.status_code} : {resp.text[:160]}")
        data = resp.json()
        for release in data.get("releases") or []:
            scanned += 1
            kept += parse_fts(release, cfg, rules, desc_chars)
        url, params = (data.get("links") or {}).get("next"), None
    return kept, scanned


def fetch_fts(cfg: dict[str, Any], settings: dict[str, Any], rules: Rules, start: datetime, end: datetime,
              desc_chars: int) -> tuple[list[Tender], int, datetime, str]:
    """Read every release updated in [start, end), one day at a time, and keep the candidates.

    A failure stops the reading but keeps the days already read; the returned date is where
    the next run resumes, with the error message (empty when everything was read)."""
    kept: list[Tender] = []
    scanned = 0
    day = start
    while day < end:
        nxt = min(day + timedelta(days=1), end)
        try:
            found, n = _fts_day(cfg, settings, rules, day, nxt, desc_chars, wait_first=day > start)
        except (requests.RequestException, ValueError) as exc:
            return kept, scanned, day, str(exc)[:200]
        kept += found
        scanned += n
        day = nxt
    return kept, scanned, end, ""


# ---------------------------------------------------------------- exchange rates

_ECB_NS = {"e": "http://www.ecb.int/vocabulary/2002-08-01/eurofxref"}


def parse_ecb(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    day = root.find(".//e:Cube[@time]", _ECB_NS)
    if day is None:
        raise ValueError("taux BCE introuvables")
    rates = {c.get("currency"): float(c.get("rate")) for c in day.findall("e:Cube", _ECB_NS)}
    return {"date": day.get("time"), "rates": rates}


def fetch_fx(cfg: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(cfg["url"], headers=_headers(settings), timeout=settings["http"]["timeout_seconds"])
    resp.raise_for_status()
    return parse_ecb(resp.text)


def to_eur(value: float | None, currency: str, fx: dict[str, Any]) -> float | None:
    if value is None:
        return None
    if not currency or currency == "EUR":
        return value
    rate = (fx.get("rates") or {}).get(currency)
    return value / rate if rate else None


# ---------------------------------------------------------------- analysis


def build_schema(tcfg: dict[str, Any], acfg: dict[str, Any]) -> dict[str, Any]:
    tender = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "relevant": {"type": "boolean"},
            "segment": {"type": "string", "enum": list(tcfg["segments"])},
            "title_fr": {"type": "string", "maxLength": 140},
            "summary": {"type": "string", "maxLength": 320},
            "why": {"type": "string", "maxLength": 220},
            "industries": {"type": "array", "items": {"type": "string", "enum": list(acfg["industries"])}},
            "fit": {"type": "string", "enum": list(tcfg["analysis"]["fit"])},
        },
        "required": ["id", "relevant", "segment", "title_fr", "summary", "why", "industries", "fit"],
        "additionalProperties": False,
    }
    return {"type": "object", "properties": {"tenders": {"type": "array", "items": tender}},
            "required": ["tenders"], "additionalProperties": False}


def build_system_prompt(tcfg: dict[str, Any], acfg: dict[str, Any]) -> str:
    text = resolve(tcfg["analysis"]["prompt_file"]).read_text(encoding="utf-8").strip()
    lines = [text, "", "## Segments"]
    lines += [f"- `{k}` : {v['label']}" for k, v in tcfg["segments"].items()]
    lines += ["", "## Industries clientes"]
    lines += [f"- `{k}` : {v}" for k, v in acfg["industries"].items()]
    lines += ["", "## Adéquation à l'offre Airbus (`fit`)"]
    lines += [f"- `{k}` : {v}" for k, v in tcfg["analysis"]["fit"].items()]
    return "\n".join(lines) + "\n"


def validate(entry: Any, tcfg: dict[str, Any], acfg: dict[str, Any], known: set[str]) -> dict[str, Any] | None:
    if not isinstance(entry, dict) or entry.get("id") not in known:
        return None
    fits = list(tcfg["analysis"]["fit"])
    out = {
        "relevant": bool(entry.get("relevant")),
        "segment": entry.get("segment") if entry.get("segment") in tcfg["segments"] else tcfg["default_segment"],
        "title_fr": str(entry.get("title_fr", "")).strip()[:140],
        "summary": str(entry.get("summary", "")).strip()[:320],
        "why": str(entry.get("why", "")).strip()[:220],
        "industries": [k for k in entry.get("industries") or [] if k in acfg["industries"]],
        "fit": entry.get("fit") if entry.get("fit") in fits else fits[-1],
    }
    if out["relevant"] and not out["title_fr"]:
        return None
    return out


def payload(t: Tender, tcfg: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    names = {k: c["name"] for k, c in ref["countries"].items()}
    return {
        "id": t.id,
        "nature": tcfg["kinds"][t.kind],
        "titre": t.title,
        "intitule_cpv": t.cpv_label,
        "acheteur": t.buyer,
        "pays_acheteur": names.get(t.buyer_country, t.buyer_country),
        "lieu": [names.get(c, c) for c in t.countries][:5],
        "cpv": t.cpv[:6],
        "date": t.published,
        "description": t.description,
        "gagnants": t.winners[:6],
    }


def select_due(book: TenderBook, rules: Rules, tcfg: dict[str, Any], today: date, cap: int) -> list[Tender]:
    """Notices to analyse: clearly geospatial first, then still open, then most recent."""
    oldest = (today - timedelta(days=tcfg["analysis"]["max_age_days"])).isoformat()
    due = [t for t in book.tenders.values()
           if not t.analysis and t.published >= oldest and not rules.excluded(t) and t.status != "cancelled"]
    due.sort(key=lambda t: (rules.is_strong(t), t.kind == "open" and t.deadline >= today.isoformat(),
                            t.published), reverse=True)
    return due[:cap]


def analyse_tenders(book: TenderBook, rules: Rules, tcfg: dict[str, Any], acfg: dict[str, Any],
                    ref: dict[str, Any], now: datetime, cap: int | None = None, analyzer=None) -> dict[str, Any]:
    report: dict[str, Any] = {"analysed": 0, "relevant": 0, "error": "", "cost_usd": None}
    cfg = tcfg["analysis"]
    analyzer = analyzer or get_analyzer(acfg)
    ok, why = analyzer.available()
    if not ok or not hasattr(analyzer, "call"):
        report["error"] = why or "moteur sans analyse des appels d'offres"
        return report
    due = select_due(book, rules, tcfg, now.date(), cap or cfg["max_per_run"])
    schema = json.dumps(build_schema(tcfg, acfg), ensure_ascii=False)
    prompt = build_system_prompt(tcfg, acfg)
    started, cost = time.monotonic(), 0.0
    for start in range(0, len(due), cfg["batch_size"]):
        if time.monotonic() - started > cfg["time_budget_minutes"] * 60:
            report["error"] = (f"budget de temps atteint ({cfg['time_budget_minutes']} min) : "
                               f"{len(due) - start} avis reportés au prochain passage")
            break
        batch = due[start:start + cfg["batch_size"]]
        body = json.dumps({"avis": [payload(t, tcfg, ref) for t in batch]}, ensure_ascii=False)
        try:
            result = analyzer.call(body, prompt, schema, INSTRUCTION, "tenders", timeout=cfg.get("timeout_seconds"))
        except AnalysisError as exc:
            report["error"] = f"analyse interrompue : {exc}"
            log.warning("tender analysis stopped: %s", exc)
            break
        if result.cost_usd is not None:
            cost += result.cost_usd
        ids = {t.id for t in batch}
        for entry in result.entries:
            clean = validate(entry, tcfg, acfg, ids)
            if clean is None:
                continue
            t = book.tenders[entry["id"]]
            t.analysis, t.analysed_at = clean, now.isoformat(timespec="seconds")
            t.analysis_model = f"{analyzer.name}:{acfg['analysis'].get('model', '')}"
            report["analysed"] += 1
            report["relevant"] += clean["relevant"]
    report["cost_usd"] = round(cost, 4) if cost else None
    return report


# ---------------------------------------------------------------- orchestration


def load_rules(settings: dict[str, Any], ref: dict[str, Any]) -> tuple[dict[str, Any], Rules]:
    tcfg = load_yaml(settings["paths"]["tenders"])
    return tcfg, Rules(tcfg, ref, load_yaml(settings["paths"]["competitors"]))


def update(settings: dict[str, Any], ref: dict[str, Any], analyse: bool = True, backfill: bool = False,
           offline: bool = False, max_tenders: int | None = None, dry_run: bool = False) -> dict[str, Any]:
    """Fetch recent notices, analyse the new ones, save data/tenders.json. Returns a run report."""
    paths = settings["paths"]
    tcfg, rules = load_rules(settings, ref)
    acfg = load_yaml(paths["analysis"])
    path = resolve(paths["tender_data"])
    book = TenderBook.load(path)
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz).replace(microsecond=0)
    desc_chars = tcfg["analysis"]["description_chars"]
    report: dict[str, Any] = {"new": 0, "sources": [], "analysis": {}}
    statuses = {s["name"]: s for s in book.meta.get("sources", [])}

    if not offline:
        ted = tcfg["ted"]
        if ted.get("enabled"):
            since = now.date() - timedelta(days=ted["backfill_days"] if backfill else ted["lookback_days"])
            status = SourceStatus(SOURCE_NAMES[TED], ted["url"], ok=True, via=VIA)
            try:
                notices = fetch_ted(ted, settings, since)
                parsed = [t for t in (parse_ted(n, ted, rules, desc_chars) for n in notices) if t]
                parsed = [t for t in parsed if not rules.excluded(t)]
                status.fetched, status.kept = len(notices), len(parsed)
                report["new"] += sum(book.upsert(t, now) for t in parsed)
            except (requests.RequestException, ValueError) as exc:
                status.ok, status.error = False, str(exc)[:200]
                log.warning("TED failed: %s", exc)
            statuses[status.name] = status.to_dict()

        fts = tcfg["fts"]
        if fts.get("enabled"):
            end = now.astimezone(timezone.utc).replace(tzinfo=None, minute=0, second=0)
            start = end - timedelta(days=fts["backfill_days"] if backfill else fts["lookback_days"])
            synced = book.meta.get("fts_synced_until")
            if synced and not backfill:   # catch up after failed runs, within the backfill window
                start = max(min(start, datetime.fromisoformat(synced)), end - timedelta(days=fts["backfill_days"]))
            status = SourceStatus(SOURCE_NAMES[FTS], fts["url"], ok=True, via=VIA)
            parsed, scanned, synced, error = fetch_fts(fts, settings, rules, start, end, desc_chars)
            status.fetched = scanned
            for t in parsed:
                if t.status == "cancelled" and t.id in book.tenders:
                    book.cancel(t.id)
                elif t.status != "cancelled" and not rules.excluded(t):
                    report["new"] += book.upsert(t, now)
                    status.kept += 1
            book.meta["fts_synced_until"] = synced.isoformat()
            if error:
                status.ok, status.error = False, error
                log.warning("Find a Tender stopped at %s: %s", synced, error)
            statuses[status.name] = status.to_dict()

        try:
            book.meta["fx"] = fetch_fx(tcfg["fx"], settings)
        except (requests.RequestException, ValueError, ET.ParseError) as exc:
            log.warning("ECB rates failed, keeping previous ones: %s", exc)

    report["pruned"] = book.prune(now.date(), tcfg["retention_days"])
    if analyse and tcfg["analysis"].get("enabled") and acfg["analysis"].get("enabled"):
        report["analysis"] = analyse_tenders(book, rules, tcfg, acfg, ref, now, max_tenders)
    report["sources"] = list(statuses.values())
    book.meta.update({"updated_at": now.isoformat(), "sources": report["sources"], "last_run": report})
    if not dry_run:
        book.save(path)
    report["book"] = book
    report["rules"] = rules
    return report


def print_report(report: dict[str, Any]) -> None:
    book: TenderBook = report["book"]
    rules: Rules = report["rules"]
    print("\nAPPELS D'OFFRES")
    for s in report["sources"]:
        state = "ok " if s["ok"] else "ERR"
        print(f"  {state} {s['name']:<14} {s['fetched']:>6} avis lus  {s['kept']:>5} retenus"
              + (f"  ({s['error']})" if s.get("error") else ""))
    tenders = list(book.tenders.values())
    visible = [t for t in tenders if rules.visible(t)]
    analysed = [t for t in tenders if t.analysis]
    by_kind: dict[str, int] = {}
    for t in visible:
        by_kind[t.kind] = by_kind.get(t.kind, 0) + 1
    print(f"  Nouveaux avis : {report['new']}   Stockés : {len(tenders)}   Affichés : {len(visible)} "
          f"({', '.join(f'{k} {n}' for k, n in sorted(by_kind.items()))})   Analysés : {len(analysed)}")
    a = report.get("analysis") or {}
    if a:
        print(f"  Analysés ce passage : {a.get('analysed', 0)} (pertinents : {a.get('relevant', 0)})")
        if a.get("error"):
            print(f"  Analyse : {a['error']}")
        if a.get("cost_usd") is not None:
            print(f"  Usage de l'abonnement Claude : ce passage aurait coûté {a['cost_usd']:.3f} $ au tarif de "
                  f"l'API (indication seulement, rien n'est facturé ; seul ton quota est consommé)")
    for t in sorted(visible, key=lambda t: t.published, reverse=True)[:8]:
        title = t.analysis.get("title_fr") or t.title
        print(f"  {t.published} {SOURCE_NAMES[t.source]:<13} {t.kind:<7} {t.buyer_country:2} {title[:80]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch and analyse public tenders.")
    parser.add_argument("--backfill", action="store_true", help="first fill: backfill_days of history")
    parser.add_argument("--no-analyse", action="store_true", help="rules only, no Claude call")
    parser.add_argument("--offline", action="store_true", help="no network: reuse data/tenders.json")
    parser.add_argument("--dry-run", action="store_true", help="fetch and report, write nothing")
    parser.add_argument("--max", type=int, default=None, help="analysis cap for this run")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from .entities import load_reference

    settings = load_settings()
    ref = load_reference(settings)
    tcfg, _ = load_rules(settings, ref)
    errors = validate_config(tcfg, load_yaml(settings["paths"]["competitors"]), ref,
                             load_yaml(settings["paths"]["analysis"]))
    if errors:
        print("Configuration invalide :\n  - " + "\n  - ".join(errors))
        return 1
    print_report(update(settings, ref, analyse=not args.no_analyse, backfill=args.backfill,
                        offline=args.offline, max_tenders=args.max, dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
