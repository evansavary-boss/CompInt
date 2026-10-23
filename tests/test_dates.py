import time
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser

from src.collect import clean_html, parse_entry_date

PARIS = ZoneInfo("Europe/Paris")
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=PARIS)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>t</title>
<item><title>RFC 822 GMT</title><link>https://a.example/1</link>
  <pubDate>Wed, 21 Oct 2026 22:30:00 GMT</pubDate></item>
<item><title>RFC 822 offset</title><link>https://a.example/2</link>
  <pubDate>Wed, 21 Oct 2026 18:00:00 -0400</pubDate></item>
<item><title>No date</title><link>https://a.example/3</link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><title>ISO</title><link href="https://b.example/1"/>
  <updated>2026-01-15T08:00:00Z</updated></entry>
</feed>"""


def test_rss_gmt_converted_to_paris_summer_time():
    entry = feedparser.parse(RSS).entries[0]
    dt, estimated = parse_entry_date(entry, PARIS, NOW)
    assert not estimated
    assert dt.isoformat() == "2026-10-22T00:30:00+02:00"


def test_rss_numeric_offset():
    entry = feedparser.parse(RSS).entries[1]
    dt, _ = parse_entry_date(entry, PARIS, NOW)
    assert dt.isoformat() == "2026-10-22T00:00:00+02:00"


def test_atom_winter_time():
    entry = feedparser.parse(ATOM).entries[0]
    dt, _ = parse_entry_date(entry, PARIS, NOW)
    assert dt.isoformat() == "2026-01-15T09:00:00+01:00"


def test_missing_date_falls_back_to_now():
    entry = feedparser.parse(RSS).entries[2]
    dt, estimated = parse_entry_date(entry, PARIS, NOW)
    assert estimated and dt == NOW


def test_future_date_is_clamped():
    entry = {"published_parsed": time.strptime("2030-01-01 00:00", "%Y-%m-%d %H:%M")}
    dt, estimated = parse_entry_date(entry, PARIS, NOW)
    assert dt == NOW and not estimated


def test_clean_html():
    import re

    raw = "<p>Ariane&nbsp;6 <b>launch</b></p> The post X appeared first on Y."
    pattern = re.compile(r"The post .*? appeared first on .*?\.?$")
    assert clean_html(raw, [pattern]) == "Ariane 6 launch"
    assert clean_html(raw, [pattern], max_chars=6) == "Ariane"
