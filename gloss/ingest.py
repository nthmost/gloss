#!/usr/bin/env python3
"""Build the gloss SQLite database from a kaikki.org wiktextract JSONL dump.

Usage:
    python -m gloss.ingest raw-wiktextract-data.jsonl.gz out.sqlite [--limit N]
    cat some.jsonl | python -m gloss.ingest - out.sqlite

Input may be .jsonl or .jsonl.gz (decompressed via pigz/gzip subprocess). The
output file is written fresh (an existing file is removed first); callers that
need atomic swaps should build to a temp path and rename.

Schema (see README for the data model):
    lang   (code, name, n_words)
    entry  (id, lang_code, word, pos, etym_no, gloss, etymology, roman)
    word   (lang_code, word, folded, roman, gloss, n_entries)        + FTS5
    edge   (src_lang, src_word, src_folded, dst_lang, dst_word, dst_folded,
            rel, via, gloss, roman)

Edge direction: src is the *later* word, dst is where it came from. So
"English gloss ← Late Latin glossa" is src=(en, gloss) dst=(la-lat, glossa).
`cog` edges are symmetric in meaning but stored once from the entry's side.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter

try:
    import orjson as _json

    def loads(b):
        return _json.loads(b)
except ImportError:  # pragma: no cover
    import json as _json

    def loads(b):
        return _json.loads(b)

from .norm import fold, nfc

# ---------------------------------------------------------------------------
# Template vocabulary
# ---------------------------------------------------------------------------

# "this word comes from X" templates: args 1=this lang, 2=source lang,
# 3=source word, 4=alt display, 5=gloss, t=gloss, tr=romanization.
DERIVATION = {
    "inh": "inh", "inh+": "inh", "inherited": "inh",
    "bor": "bor", "bor+": "bor", "borrowed": "bor",
    "lbor": "bor", "learned borrowing": "bor", "slbor": "bor", "obor": "bor",
    "ubor": "bor", "orthographic borrowing": "bor", "unadapted borrowing": "bor",
    "der": "der", "der+": "der", "derived": "der",
    "calque": "calque", "cal": "calque", "clq": "calque",
    "semantic loan": "calque", "sl": "calque", "psm": "calque",
    "translit": "bor", "transliteration": "bor",
    "root": "root",
}

# "compare X" templates: args 1=lang, 2=word, 3=alt, 4=gloss, t=gloss.
COGNATE = {"cog": "cog", "cognate": "cog", "noncog": "noncog", "noncognate": "noncog", "ncog": "noncog"}

# same-language composition: args 1=lang, 2..n=parts (named args ignored).
MORPHOLOGY = {
    "af": "affix", "affix": "affix", "compound": "affix", "com": "affix", "com+": "affix",
    "suffix": "affix", "suf": "affix", "prefix": "affix", "pre": "affix",
    "confix": "affix", "con": "affix", "circumfix": "affix", "infix": "affix",
    "univerbation": "affix", "univ": "affix", "blend": "affix", "surf": "affix",
    "surface analysis": "affix",
}

DOUBLET = {"doublet", "dbt", "piecewise doublet"}

# compact chain template {{ety|lang|:inh|lang:word|:bor|lang:word|...}}
ETY_REL = {
    ":inh": "inh", ":bor": "bor", ":der": "der", ":lbor": "bor", ":slbor": "bor",
    ":obor": "bor", ":ubor": "bor", ":cal": "calque", ":clq": "calque", ":calque": "calque",
    ":sl": "calque", ":psm": "calque", ":af": "affix", ":afeq": "affix", ":cog": "cog",
    ":dbt": "doublet", ":root": "root",
}

SKIP_WORDS = {"", "-", "—", "?", "??"}

# Wiktionary language codes: 'en', 'grc', 'la-lat', 'ine-pro', 'gmw-msc', 'roa-opt' ...
_LANG_RE = re.compile(r"^[a-z][a-z0-9]{1,3}(-[a-z0-9]{1,8}){0,3}$")
# legacy abbreviations still used in some etymology templates
LANG_ALIASES = {"LL.": "la-lat", "ML.": "la-med", "NL.": "la-new", "VL.": "la-vul", "EL.": "la-ecc", "RL.": "la-ren"}
# {{ety}} inline morphology markers: '+af', '+suf', '+com', '+univ', '+clipping' ...
ETY_PLUS_REL = "affix"


def valid_lang(code: str) -> str | None:
    """Return a cleaned language code or None if it is junk (wiki markup, etc.)."""
    if not code:
        return None
    code = LANG_ALIASES.get(code, code.strip())
    return code if _LANG_RE.match(code) else None

GLOSS_MAX = 160
ETYM_MAX = 2500
N_GLOSSES = 3


def _positional(args: dict) -> list[str]:
    """Positional template args in order, '' for gaps."""
    out = []
    i = 1
    while str(i) in args:
        out.append(args[str(i)] or "")
        i += 1
    return out


def _clean_word(w: str) -> str:
    w = nfc(w or "")
    # strip wiki-link brackets and senseid anchors that occasionally leak through
    if w.startswith("[[") and w.endswith("]]"):
        w = w[2:-2]
    return w.split("#", 1)[0].strip() if "#" in w else w


def _lang_from_expansion(expansion: str, word: str, alt: str) -> tuple[str, int] | None:
    """Recover a language *name* from a template's expanded text.

    e.g. expansion 'Late Latin glōssa (“obsolete ...”)' with word 'glōssa' → 'Late Latin'.
    Only used to name codes that never appear as entry languages (gmq, la-lat, ...).
    Returns (name, confidence) — a literal match of the word scores higher than
    the capitalized-prefix fallback.
    """
    if not expansion:
        return None
    for needle in (alt, word):
        if needle and needle in expansion:
            head = expansion.split(needle, 1)[0].strip()
            if 0 < len(head) <= 60 and not head.endswith(("from", "of", ",")):
                return head, 3
    # fallback: leading run of Capitalized tokens (stop at '*', '(', lowercase)
    toks = expansion.split()
    head = []
    for tok in toks[:4]:
        if tok[:1].isupper() and "(" not in tok and "“" not in tok:
            head.append(tok)
        else:
            break
    if head and len(head) < len(toks):
        return " ".join(head), 1
    return None


# ---------------------------------------------------------------------------
# Per-entry extraction
# ---------------------------------------------------------------------------


class Extractor:
    def __init__(self):
        self.lang_names: dict[str, str] = {}
        self.lang_votes: dict[str, Counter] = {}  # from expansions; lower trust
        self.stats = Counter()

    def note_lang(self, code: str, name, weak=False):
        """Record a language name. `name` is a str (authoritative) or a
        (name, confidence) tuple from `_lang_from_expansion` when weak=True."""
        if not code or not name:
            return
        if weak:
            n, conf = name
            self.lang_votes.setdefault(code, Counter())[n] += conf
        else:
            self.lang_names.setdefault(code, name)

    def weak_names(self) -> dict[str, str]:
        return {code: votes.most_common(1)[0][0] for code, votes in self.lang_votes.items() if votes}

    def entry_rows(self, d: dict):
        """Yield (entry_tuple, [edge_tuples]) for one JSONL record, or None."""
        word = _clean_word(d.get("word"))
        lang = d.get("lang_code")
        if not word or not lang or word in SKIP_WORDS:
            return None
        self.note_lang(lang, d.get("lang"))
        pos = d.get("pos") or ""
        etym_no = d.get("etymology_number") or 0

        glosses = []
        for s in d.get("senses") or []:
            tags = s.get("tags") or []
            if "form-of" in tags or "alt-of" in tags:
                continue
            g = (s.get("glosses") or s.get("raw_glosses") or [None])[0]
            if g:
                glosses.append(g[:GLOSS_MAX])
            if len(glosses) >= N_GLOSSES:
                break
        if not glosses:  # fall back to form-of glosses rather than nothing
            for s in d.get("senses") or []:
                g = (s.get("glosses") or [None])[0]
                if g:
                    glosses.append(g[:GLOSS_MAX])
                    break
        gloss = " · ".join(glosses)

        roman = None
        for f in d.get("forms") or []:
            if "romanization" in (f.get("tags") or []):
                roman = f.get("form")
                break

        etym = (d.get("etymology_text") or "")[:ETYM_MAX]
        entry = (lang, word, pos, etym_no, gloss, etym, roman)

        raw_edges = []
        self._etymology_edges(lang, word, d.get("etymology_templates") or [], raw_edges)
        self._descendant_edges(lang, word, d.get("descendants") or [], raw_edges)
        edges = []
        for e in raw_edges:
            s_lang, d_lang = valid_lang(e[0]), valid_lang(e[2])
            if not s_lang or not d_lang:
                self.stats["skip_bad_lang"] += 1
                continue
            edges.append((s_lang, e[1], d_lang, *e[3:]))
        return entry, edges

    # -- etymology section templates ---------------------------------------

    def _etymology_edges(self, lang, word, templates, edges):
        for t in templates:
            name = t.get("name") or ""
            args = t.get("args") or {}
            exp = t.get("expansion") or ""
            pos = _positional(args)

            if name in DERIVATION:
                rel = DERIVATION[name]
                if len(pos) < 2:
                    continue
                src_lang = pos[1]
                w = _clean_word(pos[2] if len(pos) > 2 else "")
                alt = _clean_word(pos[3] if len(pos) > 3 else "")
                if w in SKIP_WORDS:
                    w = alt
                if not src_lang:
                    continue
                if w in SKIP_WORDS:
                    # language-only mention, e.g. {{der|en|gmq|-}} → "North Germanic"
                    if exp and len(exp) <= 60:
                        self.note_lang(src_lang, (exp.strip(), 2), weak=True)
                    self.stats["skip_lang_only"] += 1
                    continue
                g = args.get("t") or args.get("gloss") or (pos[4] if len(pos) > 4 else "")
                self.note_lang(src_lang, _lang_from_expansion(exp, w, alt), weak=True)
                edges.append((lang, word, src_lang, w, rel, "etym", g[:GLOSS_MAX] if g else None, args.get("tr")))

            elif name in COGNATE:
                rel = COGNATE[name]
                if len(pos) < 2:
                    continue
                c_lang = pos[0]
                w = _clean_word(pos[1])
                alt = _clean_word(pos[2] if len(pos) > 2 else "")
                if w in SKIP_WORDS:
                    w = alt
                if not c_lang or w in SKIP_WORDS:
                    continue
                g = args.get("t") or args.get("gloss") or (pos[3] if len(pos) > 3 else "")
                self.note_lang(c_lang, _lang_from_expansion(exp, w, alt), weak=True)
                edges.append((lang, word, c_lang, w, rel, "etym", g[:GLOSS_MAX] if g else None, args.get("tr")))

            elif name in MORPHOLOGY:
                if len(pos) < 2:
                    continue
                for i, part in enumerate(pos[1:]):
                    w = _clean_word(part)
                    if w in SKIP_WORDS:
                        continue
                    # per-part language override: lang1=, lang2=...
                    p_lang = args.get(f"lang{i + 1}") or pos[0] or lang
                    g = args.get(f"t{i + 1}") or args.get(f"gloss{i + 1}")
                    edges.append((lang, word, p_lang, w, "affix", "etym", g[:GLOSS_MAX] if g else None, args.get(f"tr{i + 1}")))

            elif name in DOUBLET:
                if len(pos) < 2:
                    continue
                for part in pos[1:]:
                    w = _clean_word(part)
                    if w in SKIP_WORDS:
                        continue
                    edges.append((lang, word, pos[0] or lang, w, "doublet", "etym", None, None))

            elif name == "ety":
                self._ety_chain(lang, word, pos, edges)

    def _ety_chain(self, lang, word, pos, edges):
        """{{ety|en|:inh|enm:X|:inh|ang:Y|:der|gem-pro:*Z}} → chained edges."""
        if len(pos) < 3:
            return
        head_lang, head_word = lang, word
        rel = None
        for tok in pos[1:]:
            if not tok:
                continue
            if tok.startswith(":"):
                rel = ETY_REL.get(tok.split("<", 1)[0])
                continue
            if tok.startswith("+"):
                rel = ETY_PLUS_REL
                continue
            if rel is None:
                continue
            tok = tok.split("<", 1)[0]  # strip inline modifiers <t:...>
            if ":" in tok:
                t_lang, t_word = tok.split(":", 1)
            else:
                if rel == "affix" and tok == lang:
                    continue  # bare language code preceding affix parts
                t_lang, t_word = (lang if rel == "affix" else head_lang), tok
            t_word = _clean_word(t_word)
            if not t_lang or t_word in SKIP_WORDS:
                continue
            if rel in ("inh", "bor", "der", "calque", "root"):
                edges.append((head_lang, head_word, t_lang, t_word, rel, "etym", None, None))
                if rel != "root":
                    head_lang, head_word = t_lang, t_word  # chain continues from here
            else:
                # affix / cognate / doublet items describe the entry itself
                edges.append((lang, word, t_lang, t_word, rel, "etym", None, None))

    # -- descendants tree ---------------------------------------------------

    def _descendant_edges(self, lang, word, nodes, edges, depth=0):
        for n in nodes:
            if depth > 12:
                return
            n_lang = n.get("lang_code")
            n_word = _clean_word(n.get("word") or "")
            children = n.get("descendants") or []
            if n_lang and n_lang != "unknown" and n_word not in SKIP_WORDS:
                self.note_lang(n_lang, n.get("lang"))
                tags = n.get("raw_tags") or n.get("tags") or []
                rel = "bor" if any("borrow" in t for t in tags) else (
                    "calque" if any("calque" in t for t in tags) else "inh")
                edges.append((n_lang, n_word, lang, word, rel, "desc", None, n.get("roman")))
                self._descendant_edges(n_lang, n_word, children, edges, depth + 1)
            else:
                # grouping node ("Italo-Romance") — attach children to us
                self._descendant_edges(lang, word, children, edges, depth + 1)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE lang (code TEXT PRIMARY KEY, name TEXT, n_words INTEGER DEFAULT 0);
CREATE TABLE entry (
    id INTEGER PRIMARY KEY,
    lang_code TEXT NOT NULL,
    word TEXT NOT NULL,
    pos TEXT,
    etym_no INTEGER,
    gloss TEXT,
    etymology TEXT,
    roman TEXT
);
CREATE TABLE edge_raw (
    src_lang TEXT, src_word TEXT, dst_lang TEXT, dst_word TEXT,
    rel TEXT, via TEXT, gloss TEXT, roman TEXT
);
"""

POST_SCHEMA = """
CREATE INDEX entry_lw ON entry(lang_code, word);

CREATE TABLE word (
    lang_code TEXT NOT NULL,
    word TEXT NOT NULL,
    folded TEXT NOT NULL,
    roman TEXT,
    gloss TEXT,
    n_entries INTEGER,
    PRIMARY KEY (lang_code, word)
);
INSERT INTO word (lang_code, word, folded, roman, gloss, n_entries)
    SELECT lang_code, word, '', roman, gloss, COUNT(*)
    FROM (SELECT * FROM entry ORDER BY id)
    GROUP BY lang_code, word;
CREATE INDEX word_folded ON word(lang_code, folded);
CREATE INDEX word_folded_all ON word(folded);

CREATE TABLE edge (
    src_lang TEXT NOT NULL, src_word TEXT NOT NULL, src_folded TEXT NOT NULL,
    dst_lang TEXT NOT NULL, dst_word TEXT NOT NULL, dst_folded TEXT NOT NULL,
    rel TEXT NOT NULL, via TEXT NOT NULL,
    gloss TEXT, roman TEXT,
    PRIMARY KEY (src_lang, src_word, dst_lang, dst_word, rel)
) WITHOUT ROWID;
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE word_fts USING fts5(
    word, folded, lang_code UNINDEXED,
    content='word', content_rowid='rowid',
    tokenize="unicode61 remove_diacritics 2"
);
INSERT INTO word_fts(rowid, word, folded, lang_code)
    SELECT rowid, word, folded, lang_code FROM word;
"""


def open_input(path: str):
    if path == "-":
        return sys.stdin.buffer, None
    if path.endswith(".gz"):
        exe = shutil.which("pigz") or shutil.which("gzip")
        proc = subprocess.Popen([exe, "-dc", path], stdout=subprocess.PIPE, bufsize=1 << 20)
        return proc.stdout, proc
    return open(path, "rb"), None


def build(src: str, out: str, limit: int | None = None, log=print):
    t0 = time.time()
    tmp = out + ".building"
    for p in (tmp, tmp + "-journal"):
        if os.path.exists(p):
            os.remove(p)
    con = sqlite3.connect(tmp)
    con.executescript("PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA cache_size=-512000; PRAGMA temp_store=FILE;")
    con.executescript(SCHEMA)

    ex = Extractor()
    fh, proc = open_input(src)
    entries, edges = [], []
    n = 0
    n_edges = 0

    def flush():
        nonlocal entries, edges, n_edges
        if entries:
            con.executemany("INSERT INTO entry (lang_code, word, pos, etym_no, gloss, etymology, roman) VALUES (?,?,?,?,?,?,?)", entries)
        if edges:
            con.executemany("INSERT INTO edge_raw VALUES (?,?,?,?,?,?,?,?)", edges)
            n_edges += len(edges)
        entries, edges = [], []

    for line in fh:
        if limit and n >= limit:
            break
        if not line.strip():
            continue
        try:
            d = loads(line)
        except Exception:
            ex.stats["bad_json"] += 1
            continue
        n += 1
        r = ex.entry_rows(d)
        if r is None:
            ex.stats["skip_entry"] += 1
            continue
        entry, e_edges = r
        entries.append(entry)
        edges.extend(e_edges)
        if len(entries) >= 20000:
            flush()
        if n % 500000 == 0:
            log(f"  {n:,} records  {n_edges:,} edges  {time.time() - t0:,.0f}s")
    flush()
    if proc:
        proc.stdout.close()
        proc.wait()
    log(f"parsed {n:,} records, {n_edges:,} raw edges in {time.time() - t0:,.0f}s; stats={dict(ex.stats)}")

    # -- derived tables -------------------------------------------------
    log("building word table + indexes ...")
    con.executescript(POST_SCHEMA)
    con.create_function("fold", 1, fold, deterministic=True)
    con.execute("UPDATE word SET folded = fold(word)")
    log(f"  words: {con.execute('SELECT COUNT(*) FROM word').fetchone()[0]:,}  {time.time() - t0:,.0f}s")

    log("building edge table ...")
    con.execute(
        """INSERT OR IGNORE INTO edge
           SELECT src_lang, src_word, fold(src_word), dst_lang, dst_word, fold(dst_word),
                  rel, via, gloss, roman
           FROM edge_raw
           WHERE src_word != '' AND dst_word != ''
             AND NOT (src_lang = dst_lang AND src_word = dst_word)"""
    )
    con.execute("DROP TABLE edge_raw")
    con.execute("CREATE INDEX edge_dst ON edge(dst_lang, dst_folded)")
    con.execute("CREATE INDEX edge_src_folded ON edge(src_lang, src_folded)")
    log(f"  edges: {con.execute('SELECT COUNT(*) FROM edge').fetchone()[0]:,}  {time.time() - t0:,.0f}s")

    log("building language table ...")
    names = {c: n for c, n in ex.weak_names().items() if valid_lang(c) == c}
    names.update({c: n for c, n in ex.lang_names.items() if valid_lang(c) == c})
    # any code referenced by an edge but never named gets its code as a name
    for (code,) in con.execute("SELECT DISTINCT dst_lang FROM edge UNION SELECT DISTINCT src_lang FROM edge"):
        names.setdefault(code, code)
    con.executemany("INSERT OR REPLACE INTO lang (code, name) VALUES (?,?)", names.items())
    con.execute("UPDATE lang SET n_words = (SELECT COUNT(*) FROM word WHERE word.lang_code = lang.code)")

    log("building full-text index ...")
    con.executescript(FTS_SCHEMA)

    con.execute("INSERT INTO meta VALUES ('built_at', ?)", (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),))
    con.execute("INSERT INTO meta VALUES ('source', ?)", (os.path.basename(src),))
    con.execute("INSERT INTO meta VALUES ('n_entries', ?)", (str(con.execute("SELECT COUNT(*) FROM entry").fetchone()[0]),))
    con.execute("INSERT INTO meta VALUES ('n_words', ?)", (str(con.execute("SELECT COUNT(*) FROM word").fetchone()[0]),))
    con.execute("INSERT INTO meta VALUES ('n_edges', ?)", (str(con.execute("SELECT COUNT(*) FROM edge").fetchone()[0]),))
    con.execute("INSERT INTO meta VALUES ('n_langs', ?)", (str(con.execute("SELECT COUNT(*) FROM lang").fetchone()[0]),))
    con.commit()
    log("vacuum ...")
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, out)
    log(f"done → {out} ({os.path.getsize(out) / 1e9:.2f} GB) in {time.time() - t0:,.0f}s")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="raw-wiktextract-data.jsonl[.gz] or - for stdin")
    ap.add_argument("out", help="output sqlite path")
    ap.add_argument("--limit", type=int, default=None, help="stop after N records (dev builds)")
    a = ap.parse_args()
    build(a.src, a.out, a.limit, log=lambda m: print(m, flush=True))


if __name__ == "__main__":
    main()
