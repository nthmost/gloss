# CLAUDE.md — gloss

Word-root explorer on Wiktionary data. Live at https://gloss.nthmost.com (interim domain).
Full narrative of the current version: `docs/versions/` (latest: v0.1).

## Layout

- `gloss/ingest.py` — kaikki.org JSONL → SQLite (entries, words + FTS5, edges, langs)
- `gloss/app.py` — FastAPI: `/api/search`, `/api/graph/{lang}/{word}`, `/api/word/…`, `/api/random`, `/api/meta`
- `gloss/norm.py` — `fold` (search key) and `light_fold` (entry-name normalization); see v0.1 doc for why two
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
