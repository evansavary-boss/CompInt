"""Excerpts and story analysis.

- `truncate` turns feed text into the short excerpt shown for every article.
- Story analysers implement `StoryAnalyzer`. `ClaudeCliAnalyzer` runs Claude Code in print mode
  (`claude -p`) with the user's subscription: in GitHub Actions the long-lived token from
  `claude setup-token` is read from CLAUDE_CODE_OAUTH_TOKEN. `NoopAnalyzer` leaves stories to the
  rule-based classification. Any failure (no CLI, no token, usage limit, timeout) is logged and the
  build goes on without analysis.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import resolve
from .models import Item

log = logging.getLogger("enrich")


def truncate(text: str, max_chars: int) -> str:
    """Cut `text` at a word boundary so the result is at most `max_chars` long."""
    text = (text or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    if " " in cut:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(" ,;:.-–—") + "…"


# ---------------------------------------------------------------- analysis contract

ANALYSIS_FIELDS = ("relevant", "title_fr", "summary", "why", "tabs", "themes", "industries", "countries", "impact")


def build_schema(acfg: dict[str, Any]) -> dict[str, Any]:
    """JSON schema of Claude's answer; enumerations come from config/analysis.yaml."""
    def enum_list(keys, max_items=None):
        schema = {"type": "array", "items": {"type": "string", "enum": list(keys)}}
        if max_items:
            schema["maxItems"] = max_items
        return schema

    story = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "relevant": {"type": "boolean"},
            "duplicate_of": {"type": "string"},
            "title_fr": {"type": "string", "maxLength": 120},
            "summary": {"type": "string", "maxLength": 400},
            "why": {"type": "string", "maxLength": 240},
            "tabs": enum_list(acfg["tabs"], 2),
            "themes": enum_list(acfg["themes"], 3),
            "industries": enum_list(acfg["industries"]),
            "countries": {"type": "array", "items": {"type": "string", "pattern": "^[A-Z]{2}$"}},
            "impact": {"type": "string", "enum": list(acfg["impact"])},
        },
        "required": ["id", "duplicate_of", *ANALYSIS_FIELDS],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {"stories": {"type": "array", "items": story}},
        "required": ["stories"],
        "additionalProperties": False,
    }


def build_system_prompt(acfg: dict[str, Any]) -> str:
    """Prompt file followed by the taxonomies, so prompt and schema never drift apart."""
    text = resolve(acfg["analysis"]["prompt_file"]).read_text(encoding="utf-8").strip()
    lines = [text, "", "## Onglets"]
    lines += [f"- `{k}` ({v['label']}) : {v['description']}" for k, v in acfg["tabs"].items()]
    lines += ["", "## Thèmes"]
    lines += [f"- `{k}` : {v['label']} (onglet {v['tab']})" for k, v in acfg["themes"].items()]
    lines += ["", "## Industries clientes"]
    lines += [f"- `{k}` : {v}" for k, v in acfg["industries"].items()]
    lines += ["", "## Niveaux d'impact"]
    lines += [f"- `{k}` : {v['description']}" for k, v in acfg["impact"].items()]
    return "\n".join(lines) + "\n"


def validate(entry: dict[str, Any], acfg: dict[str, Any], known_ids: set[str]) -> dict[str, Any] | None:
    """Keep only well-formed answers; drop unknown keys and enum values."""
    if not isinstance(entry, dict) or entry.get("id") not in known_ids:
        return None
    out: dict[str, Any] = {
        "relevant": bool(entry.get("relevant", True)),
        "title_fr": str(entry.get("title_fr", "")).strip()[:120],
        "summary": str(entry.get("summary", "")).strip()[:400],
        "why": str(entry.get("why", "")).strip()[:240],
        "tabs": [t for t in entry.get("tabs") or [] if t in acfg["tabs"]][:2],
        "themes": [t for t in entry.get("themes") or [] if t in acfg["themes"]][:3],
        "industries": [t for t in entry.get("industries") or [] if t in acfg["industries"]],
        "countries": [c for c in entry.get("countries") or [] if isinstance(c, str) and len(c) == 2 and c.isupper()],
        "impact": entry.get("impact") if entry.get("impact") in acfg["impact"] else "faible",
    }
    dup = entry.get("duplicate_of") or ""
    out["duplicate_of"] = dup if dup in known_ids and dup != entry["id"] else ""
    if out["relevant"] and not out["title_fr"]:
        return None
    return out


# ---------------------------------------------------------------- analysers


class AnalysisError(Exception):
    pass


@dataclass
class BatchResult:
    entries: list[dict[str, Any]]
    cost_usd: float | None = None


class StoryAnalyzer(ABC):
    name = "abstract"

    def __init__(self, acfg: dict[str, Any]):
        self.acfg = acfg

    def available(self) -> tuple[bool, str]:
        return True, ""

    @abstractmethod
    def analyse(self, stories: list[dict[str, Any]], recent: list[dict[str, Any]]) -> BatchResult:
        ...


class NoopAnalyzer(StoryAnalyzer):
    name = "noop"

    def available(self) -> tuple[bool, str]:
        return False, "analyse désactivée (engine: noop)"

    def analyse(self, stories, recent):
        return BatchResult([])


class ClaudeCliAnalyzer(StoryAnalyzer):
    """Claude Code in print mode, no tools, JSON answer constrained by a schema."""

    name = "claude_cli"

    def __init__(self, acfg: dict[str, Any]):
        super().__init__(acfg)
        self.cfg = acfg["analysis"]
        self.schema = json.dumps(build_schema(acfg), ensure_ascii=False)
        self.system_prompt = build_system_prompt(acfg)
        self.binary = shutil.which(self.cfg["command"])

    def available(self) -> tuple[bool, str]:
        if not self.binary:
            return False, f"Claude Code introuvable (commande « {self.cfg['command']} »)"
        return True, ""

    def command(self, prompt_file: str) -> list[str]:
        return [
            self.binary, "-p", "Analyse les sujets fournis en entrée (JSON) selon tes instructions.",
            "--output-format", "json",
            "--json-schema", self.schema,
            "--model", self.cfg["model"],
            "--tools", "",
            "--disallowedTools", "mcp__*",
            "--system-prompt-file", prompt_file,
            "--no-session-persistence",
        ]

    def analyse(self, stories, recent):
        payload = json.dumps({"sujets": stories, "sujets_recents": recent}, ensure_ascii=False)
        # Run outside the project so no CLAUDE.md, hook or MCP config of the repo is loaded.
        with tempfile.TemporaryDirectory() as tmp:
            prompt_file = Path(tmp) / "system.md"
            prompt_file.write_text(self.system_prompt, encoding="utf-8")
            try:
                proc = subprocess.run(self.command(str(prompt_file)), input=payload, cwd=tmp,
                                      capture_output=True, text=True, encoding="utf-8",
                                      timeout=self.cfg["timeout_seconds"], env=os.environ.copy())
            except subprocess.TimeoutExpired:
                raise AnalysisError("délai dépassé")
        return parse_cli_output(proc.stdout, proc.returncode, proc.stderr)


def parse_cli_output(stdout: str, returncode: int, stderr: str = "") -> BatchResult:
    try:
        out = json.loads(stdout)
    except json.JSONDecodeError:
        detail = (stderr or stdout or "").strip().splitlines()[-1:] or ["sortie vide"]
        raise AnalysisError(f"réponse illisible (code {returncode}) : {detail[0][:200]}")
    if out.get("is_error") or returncode != 0:
        raise AnalysisError(str(out.get("result") or out.get("subtype") or f"code {returncode}")[:200])
    structured = out.get("structured_output")
    if structured is None:
        try:
            structured = json.loads(out.get("result") or "")
        except (TypeError, json.JSONDecodeError):
            raise AnalysisError("pas de sortie structurée")
    return BatchResult(structured.get("stories", []), out.get("total_cost_usd"))


ANALYZERS: dict[str, type[StoryAnalyzer]] = {
    "noop": NoopAnalyzer,
    "claude_cli": ClaudeCliAnalyzer,
}


def get_analyzer(acfg: dict[str, Any]) -> StoryAnalyzer:
    engine = acfg["analysis"].get("engine", "noop")
    try:
        return ANALYZERS[engine](acfg)
    except KeyError:
        raise ValueError(f"Moteur d'analyse inconnu '{engine}'. Disponibles : {', '.join(ANALYZERS)}")


# ---------------------------------------------------------------- orchestration


def story_payload(view: dict[str, Any], ref: dict[str, Any]) -> dict[str, Any]:
    head = view["headline"]
    others = []
    for it in view["items"]:
        if it is not head and it.title not in others and it.title != head.title:
            others.append(it.title)
    names = {k: v["name"] for k, v in ref["entities"].items()}
    return {
        "id": view["story"].id,
        "titre": head.title,
        "autres_titres": others[:4],
        "sources": len(view["sources"]),
        "date": head.published[:10],
        "extrait": truncate(head.summary, 300),
        "entites": [names.get(e, e) for e in view["entities"]][:8],
        "pays_detectes": view["countries"][:8],
    }


def select(book, by_id: dict[str, Item], acfg: dict[str, Any], now: datetime, cap: int) -> list:
    """Stories to analyse now, most covered and most recent first."""
    from .stories import aggregate

    cfg = acfg["analysis"]
    max_age = timedelta(days=cfg["max_age_days"])
    re_age = timedelta(days=cfg["reanalyse_max_age_days"])
    todo = []
    for story in book.stories.values():
        view = aggregate(story, by_id)
        age = now - datetime.fromisoformat(view["last_published"])
        if not story.analysis:
            due = age <= max_age
        else:
            grew = len(story.item_ids) >= cfg["reanalyse_growth"] * max(story.analysed_size, 1)
            due = grew and age <= re_age
        if due:
            todo.append((view, len(view["sources"]), view["last_published"]))
    todo.sort(key=lambda t: (t[1], datetime.fromisoformat(t[2])), reverse=True)
    return [t[0] for t in todo[:cap]]


def recent_analysed(book, by_id: dict[str, Item], now: datetime, exclude: set[str], limit: int) -> list[dict]:
    from .stories import aggregate

    rows = []
    for story in book.stories.values():
        if story.id in exclude or not story.analysis or not story.analysis.get("relevant", True):
            continue
        last = aggregate(story, by_id)["last_published"]
        if now - datetime.fromisoformat(last) <= timedelta(hours=72):
            rows.append((last, {"id": story.id, "titre": story.analysis.get("title_fr", "")}))
    rows.sort(key=lambda r: r[0], reverse=True)
    return [r[1] for r in rows[:limit]]


def analyse_stories(book, items: list[Item], acfg: dict[str, Any], ref: dict[str, Any],
                    now: datetime, max_stories: int | None = None) -> dict[str, Any]:
    """Analyse due stories in batches; stop at the first failure. Returns a run report."""
    report: dict[str, Any] = {"analysed": 0, "merged": 0, "error": "", "cost_usd": None}
    analyzer = get_analyzer(acfg)
    ok, why = analyzer.available()
    if not ok:
        report["error"] = why
        log.warning("analysis skipped: %s", why)
        return report

    cfg = acfg["analysis"]
    by_id = {i.id: i for i in items}
    due = select(book, by_id, acfg, now, max_stories or cfg["max_stories_per_run"])
    cost = 0.0
    for start in range(0, len(due), cfg["batch_size"]):
        batch = due[start:start + cfg["batch_size"]]
        ids = {v["story"].id for v in batch}
        recent = recent_analysed(book, by_id, now, ids, cfg["recent_for_duplicates"])
        try:
            result = analyzer.analyse([story_payload(v, ref) for v in batch], recent)
        except AnalysisError as exc:
            report["error"] = f"analyse interrompue : {exc}"
            log.warning("analysis stopped: %s", exc)
            break
        if result.cost_usd is not None:
            cost += result.cost_usd
        known = set(book.stories)
        for entry in result.entries:
            clean = validate(entry, acfg, known)
            if clean is None or entry["id"] not in ids:
                continue
            target = clean.pop("duplicate_of")
            if target and book.merge(entry["id"], target):
                report["merged"] += 1
                continue
            story = book.stories[entry["id"]]
            story.analysis = clean
            story.analysed_at = now.isoformat()
            story.analysed_size = len(story.item_ids)
            story.analysis_model = f"{analyzer.name}:{cfg.get('model', '')}"
            report["analysed"] += 1
    report["cost_usd"] = round(cost, 4) if cost else None
    return report
