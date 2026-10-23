from datetime import date, timedelta

from src.build import fmt_pct, fmt_price, trend
from src.market import compute_changes


def series(start: date, values: list[float], skip_weekends: bool = True):
    out, d = [], start
    for v in values:
        while skip_weekends and d.weekday() >= 5:
            d += timedelta(days=1)
        out.append((d, v))
        d += timedelta(days=1)
    return out


def test_changes_day_week_month():
    # 23 trading days from Mon 2026-09-21 to Wed 2026-10-21.
    closes = series(date(2026, 9, 21), [100.0] * 16 + [110.0] * 6 + [121.0])
    res = compute_changes(closes, week_days=7, month_days=30)
    assert res["date"] == "2026-10-21"
    assert res["price"] == 121.0
    assert res["d1"] == 10.0          # 110 -> 121
    assert res["w1"] == 10.0          # close on 2026-10-14 was 110
    assert res["m1"] == 21.0          # close on 2026-09-21 was 100


def test_changes_short_history_and_nan():
    closes = [(date(2026, 10, 21), 50.0), (date(2026, 10, 22), float("nan"))]
    res = compute_changes(closes, 7, 30)
    assert res["price"] == 50.0 and res["d1"] is None and res["m1"] is None


def test_changes_empty():
    assert compute_changes([], 7, 30) is None


def test_french_formatting():
    assert fmt_price(1015.0, "EUR") == "1 015,00 €"
    assert fmt_price(1.8005, "EUR") == "1,800 €"
    assert fmt_pct(-4.63) == "−4,6 %"
    assert fmt_pct(None) == "–"
    assert trend(0.04) == "" and trend(2) == "up" and trend(-2) == "down"
