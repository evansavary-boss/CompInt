"""Data for the Marché tab: a compact record per business news item.

The page aggregates these records in the browser (static/market.js) so that the
domain, period and region filters re-scope every chart at once.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

MAX_TITLE = 160


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
