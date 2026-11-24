# gloss

Look up any word in any language, then surf where its roots travelled.

The word you look up sits in the middle of the page. Above it: what it came
from. Below it: the words that came from it. To the sides: cognates and kin
(affix parts, doublets). An outer ring shows cousins — words in other
languages that share one of its ancestors. Click anything to make it the centre.

Live at **https://gloss.nthmost.com** (temporary home; a dedicated domain is coming).

## Data

Everything comes from the English Wiktionary via [kaikki.org](https://kaikki.org),
which publishes Tatu Ylönen's [wiktextract](https://github.com/tatuylonen/wiktextract)
parse of the whole dump as JSON Lines — about 3 GB gzipped, 27 GB raw, hundreds of
languages, refreshed at least weekly. Content is CC BY-SA 4.0.

`gloss.ingest` streams that file once and writes a single SQLite database:

| table   | what                                                                         |
|---------|------------------------------------------------------------------------------|
| `entry` | one row per Wiktionary entry (language, word, part of speech, glosses, etymology text, romanization) |
| `word`  | one row per (language, word) with a diacritic-folded key + FTS5 index for search |
| `edge`  | one row per etymological relation: `src` (the later word) ← `dst` (where it came from) |
| `lang`  | language code → name                                                         |

Edges are mined from two places in each entry:

- **`etymology_templates`** — the structured `{{inh}}`, `{{bor}}`, `{{der}}`,
  `{{cog}}`, `{{doublet}}`, `{{af}}`/`{{suffix}}`/… templates, including the
  newer compact `{{ety|…}}` chains. These give ancestors, cognates and
  morphology.
- **`descendants`** — the nested Descendants tree, which gives the other
  direction: every word Wiktionary says grew out of this one, tagged as
  inherited or borrowed.

Relation kinds: `inh` inherited, `bor` borrowed, `der` derived, `calque`,
`root`, `cog` cognate, `noncog`, `affix` formed-from, `doublet`.

Wiktionary links point at entry names which strip some marks the display form
carries (Latin macrons, Greek breves, Cyrillic stress). Lookups therefore try
an exact match first and fall back to a diacritic-folded match within the same
language — see `gloss/norm.py`.

## Running

```sh
python3 -m venv .venv && .venv/bin/pip install -e .

# dev database from the first N records (the full file is big)
curl -s https://kaikki.org/dictionary/raw-wiktextract-data.jsonl.gz | gzip -dc | head -n 250000 > dev.jsonl
.venv/bin/python -m gloss.ingest dev.jsonl data/gloss.sqlite

# full build (~3 GB download, ~10 min on a fast box)
.venv/bin/python -m gloss.ingest raw-wiktextract-data.jsonl.gz data/gloss.sqlite

GLOSS_DB=data/gloss.sqlite .venv/bin/uvicorn gloss.app:app --reload
```

API: `/api/search?q=…`, `/api/graph/{lang}/{word}`, `/api/word/{lang}/{word}`,
`/api/langs`, `/api/random`, `/api/meta`. Interactive docs at `/api/docs`.

## Deployment

Two hosts: a **build host** with RAM and disk does the weekly ingest and ships
the finished SQLite file; a **serve host** runs the read-only web app behind
Apache. The app re-opens the database whenever the file's inode changes, so
the weekly swap needs no restart.

- `deploy/deploy.sh` — code, venv, systemd unit, Apache vhosts, Let's Encrypt,
  and the DNS record at Gandi (if a key file is present).
- `deploy/refresh.sh` — the weekly cron on the build host.
- `deploy/gloss.service`, `deploy/apache/*` — the installed configs.

## Licence

Code MIT. Data from Wiktionary, CC BY-SA 4.0.
