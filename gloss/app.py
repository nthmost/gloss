"""gloss web service — FastAPI JSON API + static frontend.

Environment:
    GLOSS_DB      path to the sqlite built by gloss.ingest (default ./data/gloss.sqlite)
    GLOSS_STATIC  path to the static/ directory (default ../static relative to this file)
    (see gloss/ask.py for the Claude "ask about this page" settings)

The database is opened read-only. If the file is replaced on disk (weekly
refresh does an atomic rename), the next request notices the new inode and
reopens — no restart needed.
"""

from __future__ import annotations

import os
import random
import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, ORJSONResponse
from fastapi.staticfiles import StaticFiles

from .norm import fold, light_fold, nfc

DB_PATH = os.environ.get("GLOSS_DB", "data/gloss.sqlite")
STATIC = Path(os.environ.get("GLOSS_STATIC", Path(__file__).resolve().parent.parent / "static"))

# Relations grouped the way the UI lays them out around the centre word.
ANCESTRY = ("inh", "bor", "der", "calque", "root")
REL_LABEL = {
    "inh": "inherited from", "bor": "borrowed from", "der": "derived from",
    "calque": "calqued on", "root": "from the root", "cog": "cognate with",
    "noncog": "not cognate with", "affix": "formed from", "doublet": "doublet of",
}

MAX_PER_GROUP = 400  # hard cap on rows returned per relation group


class DB:
    """Thread-local read-only connections that follow the file's inode."""

    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()

    def _stat(self):
        try:
            st = os.stat(self.path)
            return (st.st_ino, st.st_mtime_ns)
        except FileNotFoundError:
            return None

    def conn(self) -> sqlite3.Connection:
        key = self._stat()
        if key is None:
            raise HTTPException(503, "database not available")
        cur = getattr(self._local, "key", None)
        if cur != key:
            old = getattr(self._local, "con", None)
            if old is not None:
                old.close()
            con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True, check_same_thread=False)
            con.row_factory = sqlite3.Row
            con.create_function("fold", 1, fold, deterministic=True)
            self._local.con = con
            self._local.key = key
        return self._local.con


db = DB(DB_PATH)
app = FastAPI(title="gloss", default_response_class=ORJSONResponse, docs_url="/api/docs", openapi_url="/api/openapi.json")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def lang_names(con) -> dict[str, str]:
    cache = getattr(db._local, "langs", None)
    if cache is None:
        cache = {r["code"]: r["name"] for r in con.execute("SELECT code, name FROM lang")}
        db._local.langs = cache
    return cache


def lang_family(con, lang: str) -> list[str]:
    """`lang` plus its etymology-only variants: codes like la-lat / la-med /
    grc-koi that Wiktionary uses in etymologies but never has entries for.
    Those are recognised as 'base-xxx' codes with zero entries (proto-languages
    excluded — they are real languages with their own entries)."""
    fams = getattr(db._local, "fams", None)
    if fams is None:
        fams = {}
        for r in con.execute("SELECT code, n_words FROM lang"):
            code = r["code"]
            if "-" in code and not code.endswith("-pro") and r["n_words"] == 0:
                fams.setdefault(code.split("-", 1)[0], []).append(code)
        db._local.fams = fams
    return [lang] + fams.get(lang, [])


def base_lang(con, lang: str) -> str:
    """Map an etymology-only code to the language that holds the entries."""
    if "-" in lang and not lang.endswith("-pro"):
        base = lang.split("-", 1)[0]
        if lang in lang_family(con, base):
            return base
    return lang


def resolve(con, lang: str, word: str):
    """Find the word-table row for (lang, word).

    Order: exact → exact without/with the reconstruction asterisk (Wiktionary
    entry names for proto-languages omit it, etymology templates include it)
    → diacritic-folded within the language. Folding is a last resort because
    proto-language notation uses diacritics phonemically (ǵ vs g).
    Etymology-only codes (la-lat, grc-koi …) are looked up under their base language.
    """
    lang = base_lang(con, lang)
    candidates = [word]
    if word.startswith("*"):
        candidates.append(word[1:])
    else:
        candidates.append("*" + word)
    for w in candidates:
        r = con.execute("SELECT * FROM word WHERE lang_code=? AND word=?", (lang, w)).fetchone()
        if r:
            return r
    rows = con.execute(
        "SELECT * FROM word WHERE lang_code=? AND folded=? ORDER BY n_entries DESC, word LIMIT 20",
        (lang, fold(word)),
    ).fetchall()
    if not rows:
        return None
    # prefer the row that matches once only entry-name marks are removed
    lf = light_fold(word)
    for r in rows:
        if light_fold(r["word"]) == lf:
            return r
    # anything left differs in a mark that may be phonemic (ǵ vs g, ş vs s):
    # better an "unknown" node than a confidently wrong one
    return None


def display_word(lang: str, word: str) -> str:
    """Reconstructed forms are conventionally shown with a leading asterisk."""
    if lang.endswith("-pro") and word and not word.startswith("*"):
        return "*" + word
    return word


def node(con, names, lang, word, gloss=None, roman=None):
    """Build a graph node, filling gloss/roman from the word table when known."""
    w = resolve(con, lang, word)
    return {
        "lang": lang,
        "lang_name": names.get(lang, lang),
        "word": display_word(lang, w["word"] if w else word),
        "known": bool(w),
        "gloss": (w["gloss"] if w and w["gloss"] else gloss) or "",
        "roman": (w["roman"] if w and w["roman"] else roman) or "",
    }


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------


@app.get("/api/meta")
def meta():
    con = db.conn()
    return {r["key"]: r["value"] for r in con.execute("SELECT key, value FROM meta")}


@app.get("/api/langs")
def langs(min_words: int = 1):
    con = db.conn()
    rows = con.execute("SELECT code, name, n_words FROM lang WHERE n_words >= ? ORDER BY n_words DESC", (min_words,))
    return [dict(r) for r in rows]


@app.get("/api/search")
def search(q: str = Query(..., min_length=1, max_length=200), lang: str | None = None, limit: int = 40):
    """Find words. Exact matches first, then diacritic-insensitive, then prefix."""
    con = db.conn()
    names = lang_names(con)
    q = nfc(q)
    fq = fold(q)
    limit = max(1, min(limit, 200))
    seen = set()
    out = []

    def add(rows, kind):
        for r in rows:
            k = (r["lang_code"], r["word"])
            if k in seen:
                continue
            seen.add(k)
            out.append({
                "lang": r["lang_code"], "lang_name": names.get(r["lang_code"], r["lang_code"]),
                "word": r["word"], "gloss": r["gloss"] or "", "roman": r["roman"] or "",
                "n_entries": r["n_entries"], "match": kind,
            })

    lang_sql = " AND lang_code=?" if lang else ""
    lp = (lang,) if lang else ()

    # there is no index on word alone (PK is lang+word), so go through the folded
    # index and filter: fold(q) is always a superset of the exact match
    add(con.execute(f"SELECT * FROM word WHERE folded=? AND word=?{lang_sql} ORDER BY n_entries DESC LIMIT ?", (fq, q, *lp, limit)), "exact")
    if len(out) < limit:
        add(con.execute(f"SELECT * FROM word WHERE folded=?{lang_sql} ORDER BY n_entries DESC LIMIT ?", (fq, *lp, limit)), "folded")
    if len(out) < limit and len(fq) >= 2:
        # FTS5 prefix query on the folded column; quote to neutralise operators
        term = fq.replace('"', '""')
        try:
            # take a bounded slice of full-text matches first, then rank that
            # slice (shortest words first); a prefix like "un" matches 100k+ rows
            rows = con.execute(
                f"""SELECT w.* FROM word w
                    WHERE w.rowid IN (SELECT rowid FROM word_fts WHERE word_fts MATCH ? LIMIT 3000)
                    {('AND w.lang_code=?' if lang else '')}
                    ORDER BY length(w.word), w.n_entries DESC LIMIT ?""",
                (f'folded:"{term}"*', *lp, limit),
            )
            add(rows, "prefix")
        except sqlite3.OperationalError:
            pass
    return {"q": q, "results": out[:limit]}


@app.get("/api/word/{lang}/{word:path}")
def word_detail(lang: str, word: str):
    con = db.conn()
    names = lang_names(con)
    w = resolve(con, lang, nfc(word))
    if not w:
        raise HTTPException(404, "word not found")
    entries = con.execute(
        "SELECT pos, etym_no, gloss, etymology, roman FROM entry WHERE lang_code=? AND word=? ORDER BY id",
        (lang, w["word"]),
    )
    return {
        "lang": lang, "lang_name": names.get(lang, lang), "word": w["word"], "roman": w["roman"] or "",
        "entries": [dict(e) for e in entries],
    }


@app.get("/api/graph/{lang}/{word:path}")
def graph(lang: str, word: str, limit: int = 60):
    """Everything one hop from (lang, word), grouped by relation direction.

    ancestors   — what this word came from (inh/bor/der/calque/root edges out of it)
    descendants — words that came from this one (same rels, pointing at it)
    cognates    — cog edges either direction
    components  — affix parts this word was formed from
    formations  — words formed from this one (reverse affix)
    doublets    — doublets either direction
    cousins     — other words sharing one of this word's immediate ancestors
    """
    con = db.conn()
    names = lang_names(con)
    word = nfc(word)
    w = resolve(con, lang, word)
    # an etymology-only code (la-lat) centres on the real language (la); edges
    # written against any of the family's codes are gathered together
    lang = base_lang(con, lang)
    fam = lang_family(con, lang)
    fam_ph = ",".join("?" * len(fam))
    center_word = w["word"] if w else word
    fw = fold(center_word)
    word = center_word
    limit = max(5, min(limit, MAX_PER_GROUP))

    def rows_out(rels):
        ph = ",".join("?" * len(rels))
        return con.execute(
            f"""SELECT dst_lang AS lang, dst_word AS word, rel, via, gloss, roman FROM edge
                WHERE src_lang IN ({fam_ph}) AND src_folded=? AND rel IN ({ph})""",
            (*fam, fw, *rels),
        ).fetchall()

    def rows_in(rels):
        ph = ",".join("?" * len(rels))
        return con.execute(
            f"""SELECT src_lang AS lang, src_word AS word, rel, via, NULL AS gloss, NULL AS roman FROM edge
                WHERE dst_lang IN ({fam_ph}) AND dst_folded=? AND rel IN ({ph}) LIMIT ?""",
            (*fam, fw, *rels, MAX_PER_GROUP),
        ).fetchall()

    def dedup(rows):
        best = {}
        for r in rows:
            k = (base_lang(con, r["lang"]), fold(r["word"]))
            if k == (lang, fw):
                continue
            if k not in best or (not best[k]["gloss"] and r["gloss"]):
                best[k] = dict(r)
        return list(best.values())

    def nodes(rows):
        out = []
        for r in rows[:limit]:
            n = node(con, names, r["lang"], r["word"], r.get("gloss"), r.get("roman"))
            n["rel"] = r["rel"]
            n["rel_label"] = REL_LABEL.get(r["rel"], r["rel"])
            out.append(n)
        return out

    anc_rows = dedup(rows_out(ANCESTRY))
    desc_rows = dedup(rows_in(ANCESTRY))
    cog_rows = dedup(rows_out(("cog",)) + rows_in(("cog",)))
    comp_rows = dedup(rows_out(("affix",)))
    form_rows = dedup(rows_in(("affix",)))
    dbl_rows = dedup(rows_out(("doublet",)) + rows_in(("doublet",)))

    # cousins: siblings through each immediate ancestor (other descendants of A)
    cousins = []
    seen = {(lang, fw)} | {(base_lang(con, r["lang"]), fold(r["word"])) for r in desc_rows + cog_rows}
    for a in anc_rows[:6]:
        a_fam = lang_family(con, base_lang(con, a["lang"]))
        sib = con.execute(
            f"""SELECT src_lang AS lang, src_word AS word, rel FROM edge
               WHERE dst_lang IN ({",".join("?" * len(a_fam))}) AND dst_folded=? AND rel IN ('inh','bor','der','calque') LIMIT 80""",
            (*a_fam, fold(a["word"])),
        ).fetchall()
        for s in sib:
            k = (base_lang(con, s["lang"]), fold(s["word"]))
            if k in seen:
                continue
            seen.add(k)
            n = node(con, names, s["lang"], s["word"])
            n["rel"] = "cousin"
            n["via"] = {"lang": a["lang"], "word": a["word"], "lang_name": names.get(a["lang"], a["lang"])}
            n["rel_label"] = f"also from {names.get(a['lang'], a['lang'])} {a['word']}"
            cousins.append(n)
            if len(cousins) >= limit:
                break
        if len(cousins) >= limit:
            break

    center = node(con, names, lang, center_word)
    if w:
        center["entries"] = [
            dict(e) for e in con.execute(
                "SELECT pos, etym_no, gloss, etymology FROM entry WHERE lang_code=? AND word=? ORDER BY id",
                (lang, center_word),
            )
        ]
    else:
        center["entries"] = []

    return {
        "center": center,
        "ancestors": nodes(anc_rows),
        "descendants": nodes(desc_rows),
        "cognates": nodes(cog_rows),
        "components": nodes(comp_rows),
        "formations": nodes(form_rows),
        "doublets": nodes(dbl_rows),
        "cousins": cousins,
        "totals": {
            "ancestors": len(anc_rows), "descendants": len(desc_rows), "cognates": len(cog_rows),
            "components": len(comp_rows), "formations": len(form_rows), "doublets": len(dbl_rows),
            "cousins": len(cousins),
        },
    }


@app.get("/api/random")
def random_word(min_edges: int = 6):
    """A random reasonably-connected word, for the 'surprise me' button."""
    con = db.conn()
    names = lang_names(con)
    hubs = ("la", "grc", "fro", "ang", "enm", "ine-pro", "gem-pro", "ar", "sa", "fa", "zh", "ja", "fr", "de", "ru")
    for _ in range(15):
        hub = random.choice(hubs)
        cnt = con.execute("SELECT COUNT(*) FROM edge WHERE dst_lang=?", (hub,)).fetchone()[0]
        if not cnt:
            continue
        r = con.execute("SELECT dst_word AS word FROM edge WHERE dst_lang=? LIMIT 1 OFFSET ?", (hub, random.randrange(cnt))).fetchone()
        n = con.execute("SELECT COUNT(*) FROM edge WHERE dst_lang=? AND dst_folded=?", (hub, fold(r["word"]))).fetchone()[0]
        if n >= min_edges:
            return {"lang": hub, "lang_name": names.get(hub, hub), "word": r["word"]}
    raise HTTPException(404, "no suitable word")


# ---------------------------------------------------------------------------
# ask Claude about the page (gloss/ask.py)
# ---------------------------------------------------------------------------

from .ask import router as ask_router  # noqa: E402 — needs graph/search defined above

app.include_router(ask_router)


# ---------------------------------------------------------------------------
# static frontend
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")


@app.get("/")
@app.get("/w/{lang}/{word:path}")
def index(lang: str | None = None, word: str | None = None):
    return FileResponse(STATIC / "index.html")
