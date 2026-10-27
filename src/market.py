"""Data for the Marché tab.

- Tenders (data/tenders.json): one row per visible notice, used by the Marché page, the
  country pages and the entity pages; the Marché page aggregates compact records in the
  browser (static/tenders.js).
- Press signals: a compact record per business news item, aggregated by static/market.js so
  that the domain, period and region filters re-scope every chart at once.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from . import tenders as tenders_mod
from .config import load_yaml, resolve
from .textmatch import fold

MAX_TITLE = 160


# ---------------------------------------------------------------- tenders


def fmt_eur(value: float | None) -> str:
    """Short French amount: 850 k€, 1,2 M€, 3 Md€."""
    if value is None:
        return ""
    for div, unit in ((1e9, "Md€"), (1e6, "M€"), (1e3, "k€")):
        if value >= div:
            n = value / div
            text = f"{n:.1f}" if n < 10 else f"{n:.0f}"
            return f"{text.rstrip('0').rstrip('.') if '.' in text else text} {unit}".replace(".", ",")
    return f"{value:.0f} €"


def _dedupe(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """TED republishes corrected notices under new numbers: keep the latest of each
    (source, kind, buyer, object) and merge the winners of award notices."""
    best: dict[tuple, dict[str, Any]] = {}
    for row in sorted(rows, key=lambda r: r["published"], reverse=True):
        key = (row["source"], row["kind"], fold(row["buyer"]), fold(row["orig_title"])[:90])
        kept = best.get(key)
        if kept is None:
            best[key] = row
            continue
        names = {w["name"] for w in kept["winners"]}
        kept["winners"] += [w for w in row["winners"] if w["name"] not in names]
    return list(best.values())


def tender_rows(settings: dict[str, Any], ref: dict[str, Any], entities: dict[str, Any],
                today: date, months: list[str]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Visible tenders, newest first, and the metadata of the last collection."""
    tcfg, rules = tenders_mod.load_rules(settings, ref)
    acfg = load_yaml(settings["paths"]["analysis"])
    book = tenders_mod.TenderBook.load(resolve(settings["paths"]["tender_data"]))
    fx = book.meta.get("fx") or {}
    disp = tcfg["display"]
    seg_labels = {k: v["label"] for k, v in tcfg["segments"].items()}
    fit_keys = list(tcfg["analysis"]["fit"])
    names = {k: c["name"] for k, c in ref["countries"].items()}

    def label(d: str) -> str:
        if not d:
            return ""
        dt = date.fromisoformat(d)
        return f"{dt.day} {months[dt.month - 1]} {dt.year}"

    rows = []
    for t in book.tenders.values():
        if not rules.visible(t):
            continue
        a = t.analysis
        published = date.fromisoformat(t.published)
        deadline = date.fromisoformat(t.deadline) if t.deadline else None
        age = (today - published).days
        is_open = (t.kind == "open" and t.status != "cancelled"
                   and (deadline >= today if deadline else age <= disp["open_without_deadline_days"]))
        segment = a.get("segment") or rules.segment(t)
        value = tenders_mod.to_eur(t.value, t.currency, fx)
        buyer_id = rules.buyer_entity(t)
        country = t.buyer_country if t.buyer_country in ref["countries"] else ""
        fit = a.get("fit", "")
        rows.append({
            "id": t.id,
            "source": t.source,
            "source_label": tenders_mod.SOURCE_NAMES[t.source],
            "kind": t.kind,
            "kind_label": tcfg["kinds"][t.kind],
            "title": a.get("title_fr") or t.title,
            "orig_title": t.title,
            "cpv_label": t.cpv_label,
            "summary": a.get("summary", ""),
            "why": a.get("why", ""),
            "fit": fit,
            "fit_rank": len(fit_keys) - fit_keys.index(fit) if fit in fit_keys else 0,
            "segment": segment,
            "segment_label": seg_labels.get(segment, ""),
            "industries": [k for k in a.get("industries", []) if k in acfg["industries"]],
            "buyer": t.buyer,
            "buyer_id": buyer_id if buyer_id in entities else "",
            "country": country,
            "country_name": names.get(country, ""),
            "region": ref["countries"][country]["region"] if country else "",
            "countries": [c for c in t.countries if c in ref["countries"]],
            "published": t.published,
            "published_label": label(t.published),
            "age": age,
            "deadline": t.deadline,
            "deadline_label": label(t.deadline),
            "days_left": (deadline - today).days if deadline else None,
            "open": is_open,
            "cancelled": t.status == "cancelled",
            "value": round(value) if value else None,
            "value_label": fmt_eur(value),
            "winners": rules.winner_rows(t),
            "url": t.url,
            "notice": t.notice,
            "analysed": bool(a),
        })
    rows = _dedupe(rows)
    rows.sort(key=lambda r: (r["published"], r["id"]), reverse=True)
    meta = {
        "updated_at": book.meta.get("updated_at"),
        "sources": book.meta.get("sources", []),
        "fx_date": fx.get("date"),
        "stored": len(book.tenders),
        "pending": sum(1 for t in book.tenders.values() if not t.analysis and not rules.excluded(t)),
        "config": tcfg,
    }
    return rows, meta


def tender_payload(rows: list[dict[str, Any]], meta: dict[str, Any], ref: dict[str, Any],
                   acfg: dict[str, Any], entities: dict[str, Any], today: date,
                   months: list[str]) -> dict[str, Any]:
    """Compact records and labels embedded in marche.html for static/tenders.js."""
    tcfg = meta["config"]
    disp = tcfg["display"]
    records = [{
        "i": r["id"], "s": r["source"], "k": r["kind"], "t": r["title"], "u": r["url"], "b": r["buyer"],
        "be": r["buyer_id"], "c": r["country"], "r": r["region"], "g": r["segment"], "n": r["industries"],
        "f": r["fit"], "p": r["published"], "a": r["age"], "dl": r["deadline"], "o": r["open"],
        "v": r["value"], "w": [[w["name"], w["id"], w["role"]] for w in r["winners"]],
        "sm": r["summary"], "y": r["why"], "an": r["analysed"], "cpv": r["cpv_label"],
    } for r in rows]
    countries = {r["country"] for r in rows if r["country"]}
    first = date(today.year, today.month, 1)
    month_keys = []
    for k in range(disp["months"] - 1, -1, -1):
        y, m = divmod(first.month - 1 - k, 12)
        month_keys.append({"key": f"{first.year + y:04d}-{m + 1:02d}", "label": f"{months[m]} {first.year + y}"})
    named = {w["id"] for r in rows for w in r["winners"]} | {r["buyer_id"] for r in rows}
    return {
        "records": records,
        "today": today.isoformat(),
        "months": month_keys,
        "month_names": months,
        "periods": disp["periods"],
        "closing_soon": tcfg["closing_soon_days"],
        "top": {"buyers": disp["top_buyers"], "winners": disp["top_winners"], "countries": disp["top_countries"],
                "open": disp["open_rows"], "awards": disp["award_rows"]},
        "kinds": tcfg["kinds"],
        "sources": tenders_mod.SOURCE_NAMES,
        "segments": [{"key": k, "label": v["label"]} for k, v in tcfg["segments"].items()],
        "fits": {k: k.capitalize() for k in tcfg["analysis"]["fit"]},
        "industries": acfg["industries"],
        "regions": [{"key": k, "label": v} for k, v in ref["regions"].items()],
        "countries": sorted(({"key": c, "label": ref["countries"][c]["name"]} for c in countries),
                            key=lambda c: fold(c["label"])),
        "names": {eid: entities[eid]["name"] for eid in named if eid in entities},
    }


def press_rows(stories: list[dict[str, Any]], tcfg: dict[str, Any]) -> list[dict[str, Any]]:
    """Stories about tenders and contracts spotted in the press (rest of the world)."""
    disp = tcfg["display"]
    themes, events = set(disp["press_themes"]), set(disp["press_events"])
    rows = [r for r in stories if r["age"] < disp["press_days"]
            and (themes & set(r["theme_keys"]) or events & set(r["event_keys"]))]
    return rows[: disp["press_rows"]]


# ---------------------------------------------------------------- press signals


def business_events(ref: dict[str, Any]) -> list[str]:
    """Event types shown in the Marché tab: the economy ones, in config order."""
    return [k for k, v in ref["events"].items() if v["section"] == "economie"]


def week_labels(updated_at, weeks: int, months: list[str]) -> list[str]:
    """Label of each 7-day bucket, oldest first; bucket 0 ends on the update day."""
    labels = []
    for w in range(weeks - 1, -1, -1):
        start = updated_at - timedelta(days=7 * w + 6)
        labels.append(f"{start.day} {months[start.month - 1]}")
    return labels


def market_payload(data, settings: dict[str, Any], months: list[str]) -> dict[str, Any]:
    """Records and labels embedded in marche.html (`data` is build.SiteData)."""
    ref = data.ref
    events = business_events(ref)
    event_set = set(events)
    records = []
    for item in data.items:
        if "economie" not in item.sections:
            continue
        row = data.rows[item.id]
        ents = [data.entities[e] for e in item.entities if e in data.entities]
        records.append({
            "a": row["age"],
            "t": item.tags,
            "e": [e for e in item.events if e in event_set],
            "s": row["segments"],
            "r": item.regions,
            "c": [e["id"] for e in ents if e["kind"] == "company"],
            "i": [e["id"] for e in ents if e["kind"] == "institution"],
            "ti": item.title[:MAX_TITLE],
            "u": item.url,
            "so": item.source,
            "d": row["date"],
        })
    retention = settings["history"]["retention_days"]
    weeks = -(-retention // 7)
    # Daily collection only starts on the first run; before that, the history comes from
    # the Google News backfill, capped per query, so older weeks are partial.
    first_run = min((datetime.fromisoformat(i.first_seen) for i in data.items if i.first_seen),
                    default=data.updated_at)
    complete_days = (data.updated_at.date() - first_run.date()).days + 1
    m = re.match(r"(\d+)d", settings["google_news"].get("backfill_when", "0d"))
    coverage = min(retention, complete_days + (int(m.group(1)) if m else 0))
    cfg = settings["market"]
    unknown = [k for k in list(cfg["series"]) + list(cfg["lists"]) if k not in ref["events"]]
    if unknown:
        raise ValueError(f"settings.market : type d'événement inconnu {unknown}")
    return {
        "records": records,
        "coverage_days": coverage,
        "complete_days": complete_days,
        "complete_since": f"{first_run.day} {months[first_run.month - 1]} {first_run.year}",
        "weeks": week_labels(data.updated_at, weeks, months),
        "events": [{"key": k, "label": ref["events"][k]["label"]} for k in events],
        "series": [{"key": k, "label": ref["events"][k]["label"]} for k in cfg["series"]],
        "lists": [{"key": k, "label": ref["events"][k]["label"], "n": n} for k, n in cfg["lists"].items()],
        "segments": [{"key": k, "label": v} for k, v in ref["segments"].items()],
        "regions": [{"key": k, "label": v} for k, v in ref["regions"].items()],
        "names": {eid: e["name"] for eid, e in data.entities.items()},
        "top_companies": cfg["top_companies"],
        "top_customers": cfg["top_customers"],
    }
