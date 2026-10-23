from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from src import collect

SETTINGS = {
    "http": {"user_agent": "test", "timeout_seconds": 1, "retries": 0},
    "dedup": {"tracking_params": []},
    "text": {"raw_summary_max_chars": 1000},
}
SOURCE = {"name": "Broken", "url": "https://broken.invalid/feed", "section": "politique"}
NOW = datetime(2026, 10, 22, 12, 0, tzinfo=ZoneInfo("Europe/Paris"))


def test_broken_feed_is_reported_not_raised(monkeypatch):
    def boom(*args, **kwargs):
        raise requests.ConnectionError("no route")

    monkeypatch.setattr(collect.requests, "get", boom)
    items, status = collect.collect_source(SOURCE, SETTINGS, NOW.tzinfo, NOW, [])
    assert items == []
    assert not status.ok and status.error == "connexion impossible"


def test_garbage_feed_is_reported(monkeypatch):
    class Resp:
        status_code = 200
        content = b"<html>not a feed</html>"

        def raise_for_status(self):
            pass

    monkeypatch.setattr(collect.requests, "get", lambda *a, **k: Resp())
    items, status = collect.collect_source(SOURCE, SETTINGS, NOW.tzinfo, NOW, [])
    assert items == [] and not status.ok
