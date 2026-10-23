"""Fetch quotes and upcoming earnings for the ticker basket (Yahoo Finance).

Usage:
    python -m src.market             # update data/market.json and print a table
    python -m src.market --dry-run
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import warnings
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import load_settings, load_yaml, resolve

log = logging.getLogger("market")


def _ref_close(closes: list[tuple[date, float]], target: date) -> float | None:
    """Last close on or before `target` (closes sorted oldest first)."""
    ref = None
    for d, c in closes:
        if d > target:
            break
        ref = c
    return ref


def _pct(last: float, ref: float | None) -> float | None:
    if ref is None or ref == 0:
        return None
    return round((last / ref - 1) * 100, 2)


def compute_changes(
    closes: list[tuple[date, float]], week_days: int, month_days: int
) -> dict[str, Any] | None:
    """Price and day / week / month % changes from a daily close series."""
    closes = sorted((d, c) for d, c in closes if c is not None and c == c)  # drop NaN
    if not closes:
        return None
    last_date, last = closes[-1]
    prev = closes[-2][1] if len(closes) > 1 else None
    return {
        "price": round(last, 4),
        "date": last_date.isoformat(),
        "d1": _pct(last, prev),
        "w1": _pct(last, _ref_close(closes, last_date - timedelta(days=week_days))),
        "m1": _pct(last, _ref_close(closes, last_date - timedelta(days=month_days))),
    }


def _series(frame, symbol: str) -> list[tuple[date, float]]:
    try:
        col = frame[symbol]["Close"] if symbol in frame.columns.get_level_values(0) else frame["Close"]
    except (KeyError, AttributeError):
        return []
    col = col.dropna()
    return [(idx.date(), float(v)) for idx, v in col.items()]


def fetch_quotes(symbols: list[str], period: str) -> dict[str, list[tuple[date, float]]]:
    """Batch download, then retry one by one any symbol that came back empty."""
    import yfinance as yf

    out: dict[str, list[tuple[date, float]]] = {}
    try:
        frame = yf.download(symbols, period=period, interval="1d", auto_adjust=False,
                            group_by="ticker", progress=False, threads=True)
        for s in symbols:
            out[s] = _series(frame, s)
    except Exception as exc:
        log.warning("batch download failed: %s", exc)
    for s in symbols:
        if out.get(s):
            continue
        try:
            frame = yf.download(s, period=period, interval="1d", auto_adjust=False,
                                group_by="ticker", progress=False, threads=False)
            out[s] = _series(frame, s)
        except Exception as exc:
            log.warning("%s: %s", s, exc)
            out[s] = []
    return out


def fetch_next_earnings(symbol: str, today: date, horizon_days: int) -> date | None:
    """Next earnings date within the horizon, if Yahoo knows it."""
    import yfinance as yf

    try:
        cal = yf.Ticker(symbol).calendar
    except Exception as exc:
        log.info("%s: no calendar (%s)", symbol, type(exc).__name__)
        return None
    dates = cal.get("Earnings Date") if isinstance(cal, dict) else None
    upcoming = sorted(
        d for d in (dates or [])
        if isinstance(d, date) and today <= d <= today + timedelta(days=horizon_days)
    )
    return upcoming[0] if upcoming else None


def run(settings: dict[str, Any] | None = None, dry_run: bool = False) -> dict[str, Any]:
    settings = settings or load_settings()
    cfg = settings["market"]
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz).replace(microsecond=0)
    conf = load_yaml(settings["paths"]["tickers"])
    tickers = conf["tickers"]
    path = resolve(settings["paths"]["market"])

    previous = {}
    if path.exists():
        try:
            old = json.loads(path.read_text(encoding="utf-8"))
            previous = {r["symbol"]: r for g in old.get("groups", []) for r in g["rows"]}
        except (OSError, json.JSONDecodeError, KeyError):
            pass

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        quotes = fetch_quotes([t["symbol"] for t in tickers], cfg["history_period"])

    rows, errors, earnings = {}, [], []
    for t in tickers:
        sym = t["symbol"]
        row = {"symbol": sym, "name": t["name"], "currency": t.get("currency", ""), "stale": False}
        changes = compute_changes(quotes.get(sym, []), cfg["week_days"], cfg["month_days"])
        if changes:
            row.update(changes)
        elif sym in previous and "price" in previous[sym]:
            row.update({k: previous[sym][k] for k in ("price", "date", "d1", "w1", "m1")})
            row["stale"] = True
            errors.append(sym)
        else:
            errors.append(sym)
            continue
        rows[sym] = row
        nxt = fetch_next_earnings(sym, now.date(), cfg["earnings_horizon_days"])
        if nxt:
            earnings.append({"date": nxt.isoformat(), "symbol": sym, "name": t["name"]})

    groups = []
    for key, label in conf["groups"].items():
        group_rows = [rows[t["symbol"]] for t in tickers if t["group"] == key and t["symbol"] in rows]
        if group_rows:
            groups.append({"key": key, "label": label, "rows": group_rows})

    payload = {
        "updated_at": now.isoformat(timespec="seconds"),
        "groups": groups,
        "earnings": sorted(earnings, key=lambda e: e["date"]),
        "errors": errors,
    }
    if not dry_run:
        if not rows and path.exists():
            log.warning("no quote at all: keeping previous %s", path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    return payload


def print_report(payload: dict[str, Any]) -> None:
    def pct(v):
        return "   n/d" if v is None else f"{v:+6.1f}%"

    for g in payload["groups"]:
        print(f"\n== {g['label']}")
        for r in g["rows"]:
            stale = " (ancien)" if r["stale"] else ""
            print(f"  {r['name']:<20} {r['symbol']:<8} {r['price']:>10.2f} {r['currency']}  "
                  f"1j {pct(r['d1'])}  1s {pct(r['w1'])}  1m {pct(r['m1'])}  {r['date']}{stale}")
    print("\nProchains résultats :")
    for e in payload["earnings"]:
        print(f"  {e['date']}  {e['name']}")
    if payload["errors"]:
        print(f"\nSymboles en erreur : {', '.join(payload['errors'])}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Update market data.")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print_report(run(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    sys.exit(main())
