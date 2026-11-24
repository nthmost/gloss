# CLAUDE.md — gloss

Word-root explorer on Wiktionary data. Live at https://gloss.nthmost.com (interim domain).
Full narrative of the current version: `docs/versions/` (latest: v0.1).

## Layout

- `gloss/ingest.py` — kaikki.org JSONL → SQLite (entries, words + FTS5, edges, langs)
- `gloss/app.py` — FastAPI: `/api/search`, `/api/graph/{lang}/{word}`, `/api/word/…`, `/api/random`, `/api/meta`
- `gloss/norm.py` — `fold` (search key) and `light_fold` (entry-name normalization); see v0.1 doc for why two
- `gloss/ask.py` — `POST /api/ask`: streams Claude (Sonnet) answers about the loaded page; the whole graph is
  rendered into the system prompt and Claude gets `lookup_word`/`search_words` tools over the DB. Key from
  `ANTHROPIC_API_KEY` or `claude_api_key*` in the repo root (gitignored).
- `gloss/settings.py` + `static/admin.html` — `/admin` (passcode from `GLOSS_ADMIN_PASSCODE` or `admin_passcode`
  in the repo root, gitignored) edits the ask settings: on/off, visitor passphrase, model, effort, rate limit.
  Saved to `GLOSS_SETTINGS` (`/opt/gloss/data/settings.json` on zephyr); env vars are only the defaults.
  Production secrets: `~/projects/nthmost-systems/.secrets/gloss-ask.env` → `zephyr:/opt/gloss/ask.env` via deploy.sh.
- `static/` — vanilla JS + SVG radial UI, no build step
- `deploy/` — `deploy.sh` (zephyr: code/venv/systemd/Apache/certbot/Gandi DNS), `refresh.sh` (loki weekly cron)

## Hosts

| | host | path |
|---|---|---|
| build (weekly, Sun 05:00) | loki | `/opt/gloss/{app,data,build}` |
| serve | zephyr | `/opt/gloss/{app,data}`, `gloss.service` → uvicorn :8902, Apache vhost |

Push to GitHub first, then `deploy/deploy.sh`. `refresh.sh` does **not** git pull — after ingest changes run
`ssh loki git -C /opt/gloss/app pull` (and `FORCE=1 /opt/gloss/app/deploy/refresh.sh` to rebuild now).
The Mac has little free disk: never download the 3 GB dump locally. Dev DB:
`curl -s https://kaikki.org/dictionary/raw-wiktextract-data.jsonl.gz | gzip -dc | head -n 250000 > dev.jsonl`.

## Conventions

- **Crystallize** = freeze an iteration: write `docs/versions/vX.Y.md` (header table with closing commit),
  add the row in `docs/versions/README.md`, commit, `git tag -a vX.Y`, `gh release create vX.Y --notes-file …`.
- Commit and push after changes; deploy when the change is user-visible.
- Ops stub with host details lives in `~/projects/nthmost-systems/gloss/` (synced across hosts).
- In ad-hoc SQL against the DB, use single quotes for strings: `"gloss"` is a column name in `entry`/`edge`.
