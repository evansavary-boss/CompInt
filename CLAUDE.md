# CLAUDE.md

Daily static news-watch site on geospatial & New Space (France / Europe / defence focus).
Python 3.12 pipeline → Jinja2 → `docs/` served by GitHub Pages; GitHub Actions rebuilds daily at 06:00 UTC.

## Commands

- `python -m src.build` — collect + market + render (`--skip-collect`, `--skip-market`, `--serve`, `--port`)
- `python -m src.collect [--dry-run]` — feeds only, prints a per-source report
- `python -m src.market [--dry-run]` — quotes only
- `python -m pytest -q` — tests (offline, no network)
- Windows venv: `.venv\Scripts\python.exe`

## Layout and flow

`collect.py` (fetch, clean, Europe/Paris dates) → `filter.py` (`tag_items`: URL exclusions, global exclude,
keyword tags; `dedupe`: canonical URL then similar titles) → `enrich.py` (`Enricher`, `NoopEnricher`
truncates to the excerpt length) → `store.py` (`data/items.json`, 30-day retention) → `build.py` (render).
`market.py` writes `data/market.json`. `config.py` loads YAML; `models.py` holds `Item` / `SourceStatus`.

## Rules

- No hard-coded business values: sources, keywords, tickers and tunables live in `config/*.yaml`.
- Never reproduce full articles: title, ≤200-char feed excerpt, link only.
- A failing feed or ticker must never fail the build: log, record the error, continue.
- Only use RSS URLs that were actually fetched and verified; do not invent feeds. Do not spoof browser
  User-Agents to bypass 403s.
- UI text and README in French; code, comments and this file in English.
- No framework JS: vanilla `static/app.js`. Colours are CSS custom properties with a dark-mode override.
- No secrets in the repo. A future `ClaudeEnricher` reads its API key from the environment
  (GitHub Actions secret) and registers in `ENRICHERS`; nothing else should change.
- `docs/` and `data/` are generated and committed by the workflow; edit `templates/` and `static/`, not `docs/`.
