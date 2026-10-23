"""Read and write the JSON history of items."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .models import Item, SourceStatus


def load_items(path: Path) -> tuple[list[Item], dict[str, Any]]:
    """Return stored items and the metadata of the previous run."""
    if not path.exists():
        return [], {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    items = [Item.from_dict(d) for d in data.get("items", [])]
    meta = {k: v for k, v in data.items() if k != "items"}
    return items, meta


def prune(items: list[Item], now: datetime, retention_days: int) -> list[Item]:
    cutoff = now - timedelta(days=retention_days)
    return [i for i in items if datetime.fromisoformat(i.published) >= cutoff]


def sort_items(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda i: datetime.fromisoformat(i.published), reverse=True)


def save_items(
    path: Path,
    items: list[Item],
    statuses: list[SourceStatus],
    updated_at: datetime,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": updated_at.isoformat(timespec="seconds"),
        "sources": [s.to_dict() for s in statuses],
        "items": [i.to_dict() for i in items],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
