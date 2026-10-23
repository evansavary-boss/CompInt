# CLAUDE.md

Daily static dashboard on geospatial & New Space political and economic news (France / Europe /
defence focus). Python 3.12 pipeline → Jinja2 → `docs/` served by GitHub Pages; GitHub Actions
rebuilds daily at 06:00 UTC (`.github/workflows/update.yml`).

## Commands

- `python -m src.build` — refresh Wikidata facts (weekly), collect, classify, render `docs/`
  (`--skip-collect` = no network, `--serve`, `--port`)
- `python -m src.collect` — feeds + Google News, per-source report (`--dry-run`, `--backfill`,
  `--no-gnews`, `--reclassify` = re-run classification on stored items, no network)
- `python -m src.entities` — validate reference YAML, refresh Wikidata cache (`--force`, `--offline`)
- `python -m pytest -q` — tests (offline, no network)
- Windows venv: `.venv\Scripts\python.exe`; preview config in `.claude/launch.json` (port 8000)

## Layout and flow

- `collect.py`: publisher RSS (parallel) + one Google News search per entity (sequential, delayed) →
  `filter.tag_items` (URL exclusions, `global_exclude`, domain tags) → Google News items kept only if the
  searched entity is in the title → `filter.dedupe` (canonical URL, then similar titles; publisher items
  win, `query_entities` merged) → `enrich.py` (excerpt) → exclusions + `classify.py` re-run on the whole
  history → `store.py` (`data/items.json`, 90-day retention).
- `classify.py` + `textmatch.py`: entities (longest alias wins), countries (text + entity's main
  country), regions, event types (`overrides`, `requires`, `section: auto`), tabs.
- `entities.py`: loads/validates `config/entities/*.yaml`, `geo.yaml`, `events.yaml`; Wikidata facts
  cached in `data/entities.json`; YAML `facts` override Wikidata.
- `build.py`: `SiteData` view models → `index.html` (Politique), `economie.html`, `marche.html`,
  `entite/<id>.html`. `market.py` builds the compact records the Marché page aggregates in the browser.
- Front end: `static/app.js` (feed filters, URL hash, live counts), `static/market.js` + `market.css`
  (charts, built with DOM APIs; untrusted text via `textContent` only).

## Rules

- No hard-coded business values: sources, keywords, entities, countries, events and tunables live in
  `config/*.yaml`. Validate config changes with `python -m src.entities --offline`.
- Never reproduce full articles: title, ≤200-char feed excerpt, link only.
- A failing feed, search or Wikidata call must never fail the build: log, record, keep previous data.
- Only use RSS URLs and Wikidata QIDs that were actually fetched and verified; never invent them. Do not
  spoof browser User-Agents to bypass 403s. Keep Google News requests sequential and delayed.
- YAML regexes go in single quotes (`'\b…'`); quote country codes YAML reads as booleans (`"NO"`).
- UI text and README in French; code, comments and this file in English.
- No framework JS. Colours are CSS custom properties with a dark-mode override; chart series colours
  were validated for colour-blind separation (see the comment in `static/market.css`) — re-validate if
  they change.
- No secrets in the repo. A future `ClaudeEnricher` reads its API key from the environment
  (GitHub Actions secret) and registers in `ENRICHERS`; nothing else should change.
- `docs/` and `data/` are generated and committed by the workflow; edit `templates/` and `static/`,
  not `docs/`.
