"""Word normalization helpers shared by ingest and the web app.

Wiktionary links point at *entry names*, which strip certain marks the display
form carries (Latin macrons, Greek breves, Cyrillic stress accents, ...). The
exact rules are per-language and live in Lua modules we don't ship, so we
resolve links with a two-step lookup: exact match first, then a diacritic-folded
match within the same language. `fold()` is that second key.
"""

import unicodedata

_STRIP_CATEGORIES = {"Mn", "Me"}  # nonspacing / enclosing combining marks


def fold(word: str) -> str:
    """Case-fold, strip combining marks, drop reconstruction asterisk.

    Keeps base letters in every script untouched (CJK, Arabic, Devanagari all
    pass through), so it is safe to apply to any orthography. Hangul and
    precomposed forms are recomposed with NFC at the end so folded keys stay
    comparable to NFC input.
    """
    if not word:
        return ""
    w = word.strip().lstrip("*")
    w = unicodedata.normalize("NFKD", w)
    w = "".join(ch for ch in w if unicodedata.category(ch) not in _STRIP_CATEGORIES)
    w = unicodedata.normalize("NFC", w)
    return w.casefold()


def nfc(word: str) -> str:
    return unicodedata.normalize("NFC", word.strip()) if word else ""
