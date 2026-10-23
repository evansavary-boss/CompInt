"""Reference data (companies, institutions, countries, event types) and Wikidata facts.

Usage:
    python -m src.entities            # refresh stale Wikidata facts, print a report
    python -m src.entities --force    # refresh every entity
    python -m src.entities --offline  # validate config and report from the cache only
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests

from .config import load_settings, load_yaml, resolve

log = logging.getLogger("entities")

DOMAINS = {"geospatial", "newspace"}
SECTIONS = {"economie", "politique"}
_ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_QID_RE = re.compile(r"^Q[0-9]+$")


# ---------------------------------------------------------------- reference loading


def load_reference(settings: dict[str, Any]) -> dict[str, Any]:
    """Load and validate every reference YAML file. Raises ValueError on bad config."""
    paths = settings["paths"]
    companies = load_yaml(paths["companies"])
    institutions = load_yaml(paths["institutions"])
    geo = load_yaml(paths["geo"])
    events = load_yaml(paths["events"])

    entities: dict[str, dict[str, Any]] = {}
    duplicates = []
    for kind, rows in (("company", companies.get("companies") or []),
                       ("institution", institutions.get("institutions") or [])):
        for row in rows:
            if row.get("id") in entities:
                duplicates.append(row.get("id"))
            entities[row.get("id")] = {**row, "kind": kind}

    ref = {
        "entities": entities,
        "segments": companies.get("segments") or {},
        "types": institutions.get("types") or {},
        "regions": geo.get("regions") or {},
        "countries": geo.get("countries") or {},
        "events": events.get("events") or {},
    }
    errors = [f"identifiant en double : {d}" for d in duplicates]
    errors += validate_reference(ref, settings["wikidata"]["properties"])
    editions = set((settings.get("google_news") or {}).get("editions") or {})
    for eid, e in entities.items():
        for ed in e.get("news_editions") or []:
            if ed not in editions:
                errors.append(f"{e['kind']} {eid!r}: édition Google News inconnue {ed!r}")
    if errors:
        raise ValueError("Configuration invalide :\n  - " + "\n  - ".join(errors))
    return ref


def validate_reference(ref: dict[str, Any], fact_fields: Iterable[str] = ()) -> list[str]:
    """Return human-readable problems in the reference data (empty list when valid).

    `fact_fields` lists the fact names a YAML `facts` override may use.
    """
    errors: list[str] = []
    countries, regions = ref["countries"], ref["regions"]

    for code, c in countries.items():
        if not isinstance(code, str) or len(code) != 2:
            errors.append(f"geo: code pays invalide {code!r} (mettre entre guillemets, ex. \"NO\")")
        if (c or {}).get("region") not in regions:
            errors.append(f"geo: {code}: région inconnue {(c or {}).get('region')!r}")
        if not (c or {}).get("aliases") and not (c or {}).get("aliases_cs"):
            errors.append(f"geo: {code}: aucun alias")

    seen_qids: dict[str, str] = {}
    for eid, e in ref["entities"].items():
        where = f"{e['kind']} {eid!r}"
        if not isinstance(eid, str) or not _ID_RE.match(eid):
            errors.append(f"{where}: id invalide (minuscules, chiffres et tirets)")
        for field in ("name", "description"):
            if not e.get(field):
                errors.append(f"{where}: champ {field!r} manquant")
        if not (e.get("aliases") or e.get("aliases_cs")):
            errors.append(f"{where}: aucun alias de détection")
        qid = e.get("wikidata")
        if qid:
            if not _QID_RE.match(str(qid)):
                errors.append(f"{where}: identifiant Wikidata invalide {qid!r}")
            elif qid in seen_qids:
                errors.append(f"{where}: Wikidata {qid} déjà utilisé par {seen_qids[qid]!r}")
            seen_qids[qid] = eid
        for code in e.get("countries") or []:
            if code not in countries:
                errors.append(f"{where}: pays inconnu {code!r}")
        if e.get("region") and e["region"] not in regions:
            errors.append(f"{where}: région inconnue {e['region']!r}")
        if not (e.get("countries") or e.get("region")):
            errors.append(f"{where}: indiquer `countries` ou `region`")
        allowed_facts = set(fact_fields) | {"wikipedia"}
        for key in (e.get("facts") or {}):
            if key not in allowed_facts:
                errors.append(f"{where}: fait inconnu {key!r} (autorisés : {', '.join(sorted(allowed_facts))})")
        bad_domains = set(e.get("domains") or []) - DOMAINS
        if bad_domains or not e.get("domains"):
            errors.append(f"{where}: domains doit contenir geospatial et/ou newspace")
        if e["kind"] == "company":
            for s in e.get("segments") or []:
                if s not in ref["segments"]:
                    errors.append(f"{where}: segment inconnu {s!r}")
        elif e.get("type") not in ref["types"]:
            errors.append(f"{where}: type inconnu {e.get('type')!r}")

    for key, ev in ref["events"].items():
        if (ev or {}).get("section") not in SECTIONS | {"auto"}:
            errors.append(f"événement {key!r}: section doit valoir economie, politique ou auto")
        if not ((ev or {}).get("patterns") or ev.get("patterns_cs") or ev.get("regex")):
            errors.append(f"événement {key!r}: aucun motif")
        if (ev or {}).get("requires") not in (None, "company", "institution"):
            errors.append(f"événement {key!r}: requires doit valoir company ou institution")
        for other in (ev or {}).get("overrides") or []:
            if other not in ref["events"]:
                errors.append(f"événement {key!r}: overrides inconnu {other!r}")
        for rx in (ev or {}).get("regex") or []:
            try:
                re.compile(rx)
            except re.error as exc:
                errors.append(f"événement {key!r}: regex invalide {rx!r} ({exc})")
    return errors


# ---------------------------------------------------------------- Wikidata


class WikidataClient:
    """Minimal, polite client for the Wikidata action API (batched, honours Retry-After)."""

    MAX_WAIT_SECONDS = 60

    def __init__(self, settings: dict[str, Any]):
        self.cfg = settings["wikidata"]
        self.http = settings["http"]
        self.session = requests.Session()
        self.session.headers["User-Agent"] = self.http["user_agent"]

    def _get(self, params: dict[str, Any]) -> dict[str, Any]:
        # No `maxlag`: it is meant for bots that edit, and makes read requests fail
        # whenever Wikidata replication lags a few seconds.
        params = {**params, "format": "json"}
        attempts = 1 + int(self.http.get("retries", 0)) + 2
        for attempt in range(attempts):
            resp = self.session.get(self.cfg["api_url"], params=params,
                                    timeout=self.http["timeout_seconds"])
            retry_after = resp.headers.get("Retry-After")
            if resp.status_code in (429, 503):
                wait = min(int(retry_after or 5 * (attempt + 1)), self.MAX_WAIT_SECONDS)
                log.info("Wikidata busy (HTTP %s), retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            data = resp.json()
            if "error" in data:
                raise RuntimeError(f"Wikidata: {data['error'].get('info', data['error'])}")
            return data
        raise RuntimeError("Wikidata: trop de requêtes, réessayer plus tard")

    def get_entities(self, ids: Iterable[str], props: str, sitefilter: str | None = None) -> dict[str, Any]:
        ids = sorted(set(ids))
        out: dict[str, Any] = {}
        size = self.cfg["batch_size"]
        for i in range(0, len(ids), size):
            params = {"action": "wbgetentities", "ids": "|".join(ids[i:i + size]), "props": props,
                      "languages": "|".join(self.cfg["languages"])}
            if sitefilter:
                params["sitefilter"] = sitefilter
            out.update(self._get(params).get("entities", {}))
            if i + size < len(ids):
                time.sleep(1)
        return out


def _best_claims(claims: dict[str, Any], pid: str) -> list[dict[str, Any]]:
    """Claims for a property, preferred rank first, deprecated and ended ones dropped."""
    rows = [c for c in claims.get(pid, []) if c.get("rank") != "deprecated"]
    current = [c for c in rows if "P582" not in c.get("qualifiers", {})]  # P582 = end time
    rows = current or rows
    preferred = [c for c in rows if c.get("rank") == "preferred"]
    return preferred or rows


def _value(claim: dict[str, Any]) -> Any:
    return claim.get("mainsnak", {}).get("datavalue", {}).get("value")


def _year(time_value: str | None) -> int | None:
    m = re.match(r"^[+-]?(\d{1,4})-", time_value or "")
    return int(m.group(1)) if m and int(m.group(1)) > 0 else None


def parse_claims(entity: dict[str, Any], properties: dict[str, str]) -> dict[str, Any]:
    """Extract raw facts from a wbgetentities entity. Item values stay as QIDs."""
    claims = entity.get("claims", {})
    raw: dict[str, Any] = {}
    for field, pid in properties.items():
        best = _best_claims(claims, pid)
        if not best:
            continue
        if field == "employees":
            def point_in_time(c):
                q = c.get("qualifiers", {}).get("P585", [])
                return _year(q[0].get("datavalue", {}).get("value", {}).get("time")) if q else 0
            claim = max(best, key=lambda c: point_in_time(c) or 0)
            amount = (_value(claim) or {}).get("amount")
            if amount:
                raw["employees"] = {"value": int(float(amount)), "year": point_in_time(claim) or None}
            continue
        val = _value(best[0])
        if isinstance(val, dict) and "id" in val:
            raw[field] = val["id"]
        elif isinstance(val, dict) and "time" in val:
            raw[field] = _year(val["time"])
        elif isinstance(val, str):
            raw[field] = val
    links = entity.get("sitelinks", {})
    for lang in ("fr", "en"):
        title = links.get(f"{lang}wiki", {}).get("title")
        if title:
            raw["wikipedia"] = f"https://{lang}.wikipedia.org/wiki/{quote(title.replace(' ', '_'))}"
            break
    return raw


def _label(entity: dict[str, Any], languages: list[str]) -> str | None:
    labels = entity.get("labels", {})
    for lang in languages:
        if lang in labels:
            return labels[lang]["value"]
    return None


def finalize_facts(raw: dict[str, Any], labels: dict[str, str]) -> dict[str, Any]:
    """Replace QIDs by labels and build display-ready values."""
    facts = dict(raw)
    for field in ("headquarters", "country", "ceo", "director", "parent"):
        if field in facts:
            label = labels.get(facts[field])
            if label:
                facts[field] = label
            else:
                facts.pop(field)
    if "logo" in facts:
        name = facts["logo"].replace(" ", "_")
        facts["logo"] = f"https://commons.wikimedia.org/wiki/Special:FilePath/{quote(name)}?width=160"
    return facts


def fetch_facts(client: WikidataClient, qids: list[str], settings: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cfg = settings["wikidata"]
    entities = client.get_entities(qids, "claims|sitelinks", sitefilter="frwiki|enwiki")
    raws = {qid: parse_claims(ent, cfg["properties"]) for qid, ent in entities.items() if "missing" not in ent}
    referenced = {v for raw in raws.values() for k, v in raw.items()
                  if k in ("headquarters", "country", "ceo", "director", "parent")}
    label_entities = client.get_entities(referenced, "labels") if referenced else {}
    labels = {q: _label(e, cfg["languages"]) for q, e in label_entities.items()}
    return {qid: finalize_facts(raw, {k: v for k, v in labels.items() if v}) for qid, raw in raws.items()}


# ---------------------------------------------------------------- cache


def load_cache(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"entities": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"entities": {}}


def stale_entities(ref: dict[str, Any], cache: dict[str, Any], now: datetime,
                   refresh_days: int, force: bool = False) -> list[str]:
    """Ids whose Wikidata facts are missing, outdated or tied to another QID."""
    stale = []
    for eid, e in ref["entities"].items():
        qid = e.get("wikidata")
        if not qid:
            continue
        cached = cache.get("entities", {}).get(eid)
        if force or not cached or cached.get("qid") != qid:
            stale.append(eid)
            continue
        fetched = datetime.fromisoformat(cached["fetched_at"])
        if now - fetched > timedelta(days=refresh_days):
            stale.append(eid)
    return stale


def refresh(settings: dict[str, Any], ref: dict[str, Any], force: bool = False,
            offline: bool = False, client: WikidataClient | None = None) -> dict[str, Any]:
    """Update data/entities.json. Network problems keep the previous cache."""
    tz = ZoneInfo(settings["timezone"])
    now = datetime.now(tz).replace(microsecond=0)
    path = resolve(settings["paths"]["entities"])
    cache = load_cache(path)
    cache.setdefault("entities", {})
    cache["error"] = ""

    todo = [] if offline else stale_entities(ref, cache, now, settings["wikidata"]["refresh_days"], force)
    if todo:
        qids = {ref["entities"][eid]["wikidata"]: eid for eid in todo}
        try:
            facts = fetch_facts(client or WikidataClient(settings), list(qids), settings)
        except Exception as exc:  # never break the build on Wikidata
            log.warning("Wikidata unavailable, keeping cached facts: %s", exc)
            cache["error"] = str(exc)[:200]
            facts = {}
        for qid, eid in qids.items():
            if qid in facts:
                cache["entities"][eid] = {"qid": qid, "fetched_at": now.isoformat(), "facts": facts[qid]}
        if facts:
            cache["updated_at"] = now.isoformat()

    # Drop entities removed from the config.
    cache["entities"] = {k: v for k, v in cache["entities"].items() if k in ref["entities"]}
    cache["refreshed"] = len([q for q in todo if q in cache["entities"]]) if todo else 0
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=1), encoding="utf-8")
    return cache


def merged_entities(ref: dict[str, Any], cache: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Config entities with Wikidata facts; YAML `facts` override them (null hides a field)."""
    cached = cache.get("entities", {})
    out = {}
    for eid, e in ref["entities"].items():
        facts = dict(cached.get(eid, {}).get("facts", {}))
        for key, value in (e.get("facts") or {}).items():
            if value is None:
                facts.pop(key, None)
            else:
                facts[key] = value
        out[eid] = {**e, "facts": facts,
                    "facts_overridden": sorted((e.get("facts") or {}).keys()),
                    "facts_fetched_at": cached.get(eid, {}).get("fetched_at")}
    return out


# ---------------------------------------------------------------- CLI


def print_report(ref: dict[str, Any], cache: dict[str, Any]) -> None:
    ents = merged_entities(ref, cache)
    companies = [e for e in ents.values() if e["kind"] == "company"]
    institutions = [e for e in ents.values() if e["kind"] == "institution"]
    print(f"\nRéférentiel : {len(companies)} entreprises, {len(institutions)} institutions, "
          f"{len(ref['countries'])} pays, {len(ref['events'])} types d'événements")
    print(f"Faits Wikidata rafraîchis : {cache.get('refreshed', 0)}"
          + (f"   (erreur : {cache['error']})" if cache.get("error") else ""))

    def leader(f):
        return f.get("ceo") or f.get("director") or ""

    for title, rows in (("ENTREPRISES", companies), ("INSTITUTIONS", institutions)):
        print(f"\n== {title}")
        for e in rows:
            f = e["facts"]
            emp = f"{f['employees']['value']:,}".replace(",", " ") if f.get("employees") else ""
            print(f"  {e['id']:<24} {str(e.get('wikidata') or '-'):<11} {str(f.get('founded') or ''):<5} "
                  f"{(f.get('headquarters') or '')[:18]:<18} {leader(f)[:24]:<24} {emp:>8}  "
                  f"{'web' if f.get('website') else '   '} {'logo' if f.get('logo') else ''}")
    missing = [e["id"] for e in ents.values() if e.get("wikidata") and not e["facts"]]
    if missing:
        print(f"\nSans faits Wikidata : {', '.join(missing)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate reference data and refresh Wikidata facts.")
    parser.add_argument("--force", action="store_true", help="refresh every entity")
    parser.add_argument("--offline", action="store_true", help="no network, cache only")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = load_settings()
    try:
        ref = load_reference(settings)
    except ValueError as exc:
        print(exc)
        return 1
    print_report(ref, refresh(settings, ref, force=args.force, offline=args.offline))
    return 0


if __name__ == "__main__":
    sys.exit(main())
