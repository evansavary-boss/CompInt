"""Build the static site in docs/.

Usage:
    python -m src.build                  # collect news, then render docs/
    python -m src.build --skip-collect   # render from the existing data/*.json
    python -m src.build --serve          # build, then serve docs/ on http://localhost:8000
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import logging
import shutil
import sys
from datetime import datetime, timedelta
from itertools import groupby
from typing import Any
from zoneinfo import ZoneInfo

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import collect, market
from .config import load_settings, resolve
from .filter import normalize_text
from .models import Item
from .store import load_items

log = logging.getLogger("build")

MONTHS = ["janv.", "févr.", "mars", "avr.", "mai", "juin",
          "juil.", "août", "sept.", "oct.", "nov.", "déc."]
WEEKDAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]

SECTIONS = [
    ("politique", "Politique", "Défense & espace, UE / ESA / OTAN, budgets, réglementation"),
    ("entreprises", "Entreprises", "Contrats, lancements, levées de fonds, rachats, produits"),
]


def day_label(day: datetime, today: datetime) -> str:
    delta = (today.date() - day.date()).days
    if delta == 0:
        return "Aujourd'hui"
    if delta == 1:
        return "Hier"
    return f"{WEEKDAYS[day.weekday()].capitalize()} {day.day} {MONTHS[day.month - 1]}"


def format_datetime(dt: datetime) -> str:
    return f"{dt.day} {MONTHS[dt.month - 1]} {dt.year} à {dt:%H:%M}"


def group_by_day(items: list[Item], now: datetime) -> list[dict[str, Any]]:
    """Group items (already sorted newest first) into day buckets for the template."""
    groups = []
    for day, day_items in groupby(items, key=lambda i: datetime.fromisoformat(i.published).date()):
        day_items = list(day_items)
        first = datetime.fromisoformat(day_items[0].published)
        groups.append({
            "label": day_label(first, now),
            "age_days": (now.date() - day).days,
            "items": [
                {
                    "item": i,
                    "time": f"{datetime.fromisoformat(i.published):%H:%M}",
                    "search": normalize_text(f"{i.title} {i.summary} {i.source}"),
                }
                for i in day_items
            ],
        })
    return groups


CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£"}


def fmt_number(value: float, decimals: int) -> str:
    """French number formatting: 1 015,00."""
    s = f"{value:,.{decimals}f}"
    return s.replace(",", " ").replace(".", ",")


def fmt_price(value: float | None, currency: str) -> str:
    if value is None:
        return "–"
    decimals = 3 if value < 10 else 2
    return f"{fmt_number(value, decimals)} {CURRENCY_SYMBOLS.get(currency, currency)}"


def fmt_pct(value: float | None) -> str:
    if value is None:
        return "–"
    sign = "+" if value > 0 else ("−" if value < 0 else "")
    return f"{sign}{fmt_number(abs(value), 1)} %"


def trend(value: float | None) -> str:
    if value is None or round(value, 1) == 0:
        return ""
    return "up" if value > 0 else "down"


def short_date(iso: str) -> str:
    d = datetime.fromisoformat(iso)
    return f"{WEEKDAYS[d.weekday()][:3]}. {d.day} {MONTHS[d.month - 1]}"


def load_market(path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        market = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if market.get("updated_at"):
        market["updated_label"] = format_datetime(datetime.fromisoformat(market["updated_at"]))
    return market


def render(settings: dict[str, Any]) -> None:
    paths = settings["paths"]
    tz = ZoneInfo(settings["timezone"])
    items, meta = load_items(resolve(paths["items"]))
    updated_at = datetime.fromisoformat(meta["updated_at"]) if meta.get("updated_at") else datetime.now(tz)
    sources = meta.get("sources", [])

    retention = settings["history"]["retention_days"]
    cutoff = updated_at - timedelta(days=retention)
    items = [i for i in items if datetime.fromisoformat(i.published) >= cutoff]

    sections = []
    for key, title, subtitle in SECTIONS:
        sec_items = [i for i in items if i.section == key]
        sections.append({
            "key": key, "title": title, "subtitle": subtitle,
            "groups": group_by_day(sec_items, updated_at),
        })

    env = Environment(
        loader=FileSystemLoader(resolve(paths["templates"])),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters.update(price=fmt_price, pct=fmt_pct, trend=trend, short_date=short_date)
    html = env.get_template("index.html.j2").render(
        updated_at=updated_at,
        updated_label=format_datetime(updated_at),
        sections=sections,
        market=load_market(resolve(paths["market"])),
        sources_ok=[s for s in sources if s.get("ok")],
        sources_err=[s for s in sources if not s.get("ok")],
        display_days=settings["history"]["display_days"],
        retention_days=retention,
        asset_version=updated_at.strftime("%Y%m%d%H%M"),
    )

    out = resolve(paths["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.html").write_text(html, encoding="utf-8")
    (out / ".nojekyll").touch()
    static_out = out / "static"
    if static_out.exists():
        shutil.rmtree(static_out)
    shutil.copytree(resolve(paths["static"]), static_out)
    log.info("Site written to %s (%d items)", out / "index.html", len(items))


def serve(settings: dict[str, Any], port: int) -> None:
    directory = resolve(settings["paths"]["output_dir"])
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(directory))
    with http.server.ThreadingHTTPServer(("127.0.0.1", port), handler) as httpd:
        print(f"Aperçu : http://localhost:{port}  (Ctrl+C pour arrêter)")
        httpd.serve_forever()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static site.")
    parser.add_argument("--skip-collect", action="store_true", help="reuse data/items.json")
    parser.add_argument("--skip-market", action="store_true", help="reuse data/market.json")
    parser.add_argument("--serve", action="store_true", help="serve docs/ after building")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    settings = load_settings()
    if not args.skip_collect:
        collect.print_report(collect.run(settings))
    if not args.skip_market:
        try:
            market.print_report(market.run(settings))
        except Exception:  # market data must never break the build
            log.exception("market update failed; keeping previous data/market.json")
    render(settings)
    if args.serve:
        serve(settings, args.port)
    return 0


if __name__ == "__main__":
    sys.exit(main())
