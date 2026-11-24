"""Ask Claude about the word on screen.

POST /api/ask   {"lang", "word", "messages": [{"role", "content", "page"?}, …]}
    Streams newline-delimited JSON events:
        {"type": "text", "text": "…"}                     a piece of the answer
        {"type": "tool", "name": "…", "input": {…}}        Claude is consulting the database
        {"type": "done", "usage": {…}}
        {"type": "error", "message": "…"}
GET  /api/ask    {"enabled": bool, "token_required": bool, "model": "…"}

The whole page the user is looking at — the centre word's entries and every
relative the graph endpoint returned for it — is rendered into the system
prompt, so questions can refer to "that Latin word above" without restating
it. Claude also gets two tools over the same database (lookup_word,
search_words) so it can walk further along a chain than one hop.

The server is stateless: the browser keeps the transcript and sends it back
each turn. Only text is kept between turns (tool traces are dropped).

Environment:
    ANTHROPIC_API_KEY           the key, or
    GLOSS_ANTHROPIC_KEY_FILE    a file holding it (default: claude_api_key* in the repo root)
Everything else (on/off, passphrase, model, effort, rate limit) lives in
gloss/settings.py and is editable at /admin.
"""

from __future__ import annotations

import asyncio
import glob
import hmac
import json
import logging
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any, AsyncIterator

import anthropic
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from . import settings

log = logging.getLogger("gloss.ask")
router = APIRouter()

ROOT = Path(__file__).resolve().parent.parent
MAX_TOKENS = 8192
MAX_HISTORY = 40           # messages the client may send back
MAX_MESSAGE_CHARS = 6000


def _read_secret(env: str, file_env: str, default_glob: str | None = None) -> str | None:
    if os.environ.get(env):
        return os.environ[env].strip()
    path = os.environ.get(file_env)
    if not path and default_glob:
        found = sorted(glob.glob(str(ROOT / default_glob)))
        path = found[0] if found else None
    if path and os.path.exists(path):
        return Path(path).read_text().strip() or None
    return None


API_KEY = _read_secret("ANTHROPIC_API_KEY", "GLOSS_ANTHROPIC_KEY_FILE", "claude_api_key*")

_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=API_KEY)
    return _client


# ---------------------------------------------------------------------------
# rendering the database into text Claude can read
# ---------------------------------------------------------------------------

SYSTEM = """You are the resident etymologist of gloss, a word-root explorer built on English Wiktionary data (via kaikki.org). The user is looking at a page centred on one word, with its relatives arranged around it: ancestors above, descendants below, cognates and kin at the sides, and an outer ring of "cousins" (other words descended from the same immediate ancestor). Everything on their screen is reproduced below under ON SCREEN, so they will ask about "that Greek one" or "the second cognate" and expect you to know what they mean.

What you are for: explaining the history, meanings, sound changes, borrowing routes and relationships of the words on the page; reading the notation for them; answering follow-ups; and going further along a chain when asked. Use the tools whenever a question needs a word that is not on screen, a word's own entry text, or the next hop up or down a chain — the database is the source of truth for what Wiktionary says. Distinguish clearly between what the database says and what you add from your own knowledge, and say so when a Wiktionary etymology looks doubtful, folk-etymological or disputed. Never invent a word, form or language that you cannot back up.

Notation: an asterisk marks a reconstructed (unattested) form. Language names are Wiktionary's; codes in parentheses are Wiktionary codes (en, la, grc, ine-pro …) and are what the tools take. Relation words are Wiktionary's templates — inherited (native descent), borrowed, derived (descent of unspecified kind), calque, root (built on a PIE-style root), cognate, doublet (same origin, different route), formed from (affix/compound parts).

Style: plain, warm, precise prose, as if talking to a curious friend who is already looking at the page — do not read the page back to them. Keep answers short unless asked for depth; one to three short paragraphs is typical. Light markdown only: **bold** for the occasional key word, hyphen bullets for genuine lists, no headers, no tables. Never use single-asterisk italics — asterisks mean reconstruction here and would be misread. Write non-Latin scripts in the original script with a romanization in parentheses the first time. When you mention a word that is in the database, give it as `word (language)` so the user can look it up."""

GROUPS = [
    ("ancestors", "CAME FROM (ancestors)"),
    ("descendants", "WENT ON TO (descendants)"),
    ("cognates", "COGNATES"),
    ("components", "FORMED FROM (components)"),
    ("formations", "FORMS PART OF (formations)"),
    ("doublets", "DOUBLETS"),
    ("cousins", "SHARES AN ANCESTOR WITH (cousins)"),
]


def _line(n: dict, with_rel: bool = True) -> str:
    bits = [n["word"]]
    if n.get("roman"):
        bits.append(f"[{n['roman']}]")
    bits.append(f"({n.get('lang_name') or n['lang']}, {n['lang']})")
    s = " ".join(bits)
    if with_rel and n.get("rel_label"):
        s += f" — {n['rel_label']}"
    if n.get("gloss"):
        s += f" — {n['gloss'][:200]}"
    if n.get("known") is False:
        s += " — (no entry of its own)"
    return "- " + s


def render_graph(g: dict, *, limit: int | None = None, cousins: bool = True) -> str:
    c = g["center"]
    out = [f"CENTRE: {c['word']}" + (f" [{c['roman']}]" if c.get("roman") else "") + f" ({c.get('lang_name') or c['lang']}, {c['lang']})"]
    if c.get("entries"):
        for e in c["entries"]:
            head = f"  - {e.get('pos') or 'entry'}" + (f" (etymology {e['etym_no']})" if e.get("etym_no") else "")
            out.append(head + ": " + (e.get("gloss") or "—"))
            if e.get("etymology"):
                out.append("    etymology: " + e["etymology"].strip()[:1500])
    else:
        out.append("  (no entry of its own in Wiktionary — only mentioned in other words' etymologies)")
    totals = g.get("totals", {})
    for key, label in GROUPS:
        items = g.get(key) or []
        if key == "cousins" and not cousins:
            continue
        if not items:
            continue
        shown = items[:limit] if limit else items
        total = totals.get(key, len(items))
        out.append(f"{label}: {total}" + (f" (showing {len(shown)})" if len(shown) < total else ""))
        for n in shown:
            line = _line(n, with_rel=key not in ("cousins",))
            if key == "cousins" and n.get("via"):
                line += f" — via {n['via']['word']} ({n['via'].get('lang_name') or n['via']['lang']})"
            out.append(line)
    return "\n".join(out)


def page_context(lang: str, word: str) -> str:
    from .app import graph  # lazy: app imports this module

    g = graph(lang, word)
    return "ON SCREEN (the page the user is looking at right now):\n\n" + render_graph(g)


# ---------------------------------------------------------------------------
# tools
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "name": "lookup_word",
        "description": (
            "Look up a word in the gloss database (English Wiktionary): its entries with glosses and "
            "etymology text, and everything one hop away — ancestors, descendants, cognates, components, "
            "doublets. Use a Wiktionary language code (en, la, grc, fro, ang, ine-pro, …). Reconstructed "
            "forms may be given with or without the asterisk. Diacritics are matched loosely."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "lang": {"type": "string", "description": "Wiktionary language code"},
                "word": {"type": "string", "description": "the word as Wiktionary spells it"},
            },
            "required": ["lang", "word"],
            "additionalProperties": False,
        },
        "strict": True,
        "eager_input_streaming": True,
    },
    {
        "name": "search_words",
        "description": (
            "Search the database for words by spelling, in any script, optionally restricted to one "
            "language. Returns matches with language and gloss. Use it to find the right language code "
            "or spelling before lookup_word, or to find a word the user half-remembers."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "q": {"type": "string", "description": "spelling to search for (exact, diacritic-folded, then prefix)"},
                "lang": {"type": "string", "description": "optional Wiktionary language code to restrict to"},
            },
            "required": ["q"],
            "additionalProperties": False,
        },
        "strict": True,
        "eager_input_streaming": True,
    },
]


def run_tool(name: str, args: dict) -> str:
    from .app import graph, search  # lazy

    if name == "lookup_word":
        lang, word = args["lang"].strip(), args["word"].strip()
        try:
            g = graph(lang, word, limit=25)
        except HTTPException as e:
            return f"error: {e.detail}"
        if not g["center"]["known"] and not any(g.get(k) for k, _ in GROUPS):
            return f"nothing in the database for {word!r} in language {lang!r}. Try search_words to check the spelling or code."
        return render_graph(g, limit=25, cousins=False)
    if name == "search_words":
        q = args["q"].strip()
        lang = (args.get("lang") or "").strip() or None
        if not q:
            return "error: empty query"
        res = search(q=q, lang=lang, limit=15)
        if not res["results"]:
            return f"no matches for {q!r}" + (f" in {lang}" if lang else "")
        return "\n".join(
            f"- {r['word']}" + (f" [{r['roman']}]" if r.get("roman") else "") + f" ({r['lang_name']}, {r['lang']})"
            + (f" — {r['gloss'][:160]}" if r.get("gloss") else "") + f"  [{r['match']} match]"
            for r in res["results"]
        )
    return f"error: unknown tool {name}"


def valid_input(name: str, args: Any) -> bool:
    if not isinstance(args, dict):
        return False
    if name == "lookup_word":
        return isinstance(args.get("lang"), str) and isinstance(args.get("word"), str)
    if name == "search_words":
        return isinstance(args.get("q"), str) and (args.get("lang") is None or isinstance(args["lang"], str))
    return False


# ---------------------------------------------------------------------------
# request handling
# ---------------------------------------------------------------------------


class Page(BaseModel):
    lang: str = Field(min_length=1, max_length=40)
    word: str = Field(min_length=1, max_length=200)


class Msg(BaseModel):
    role: str = Field(pattern="^(user|assistant)$")
    content: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)
    page: Page | None = None


class AskBody(BaseModel):
    lang: str = Field(min_length=1, max_length=40)
    word: str = Field(min_length=1, max_length=200)
    messages: list[Msg] = Field(min_length=1, max_length=MAX_HISTORY)


_hits: dict[str, deque] = defaultdict(deque)


def rate_limited(ip: str, n: int, window: float) -> bool:
    """Sliding window per IP, per worker process: a backstop, not accounting."""
    now = time.monotonic()
    q = _hits[ip]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= n:
        return True
    q.append(now)
    return False


def bearer(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    return (auth[7:] if auth.lower().startswith("bearer ") else request.headers.get("x-gloss-token", "")).strip()


def check_token(request: Request, cfg: settings.Settings) -> None:
    if not cfg.gate_on:
        return
    if not hmac.compare_digest(bearer(request), cfg.token):
        raise HTTPException(401, "ask token required")


@router.get("/api/ask")
def ask_config():
    cfg = settings.current()
    return {"enabled": bool(API_KEY) and cfg.ask_enabled, "token_required": cfg.gate_on, "model": cfg.model}


@router.post("/api/ask")
async def ask(body: AskBody, request: Request):
    cfg = settings.current()
    if not API_KEY:
        raise HTTPException(503, "ask is not configured on this server")
    if not cfg.ask_enabled:
        raise HTTPException(503, "ask is switched off right now")
    check_token(request, cfg)
    ip = request.client.host if request.client else "?"
    if rate_limited(ip, cfg.rate_limit_n, cfg.rate_limit_minutes * 60):
        raise HTTPException(429, "slow down a little")
    if body.messages[-1].role != "user":
        raise HTTPException(400, "last message must be from the user")

    # the page is rendered in a worker thread: it is a handful of sqlite queries
    try:
        context = await asyncio.to_thread(page_context, body.lang, body.word)
    except HTTPException as e:
        raise HTTPException(e.status_code, f"could not load the page context: {e.detail}")

    messages = []
    for m in body.messages:
        text = m.content
        if m.role == "user" and m.page and (m.page.lang, m.page.word) != (body.lang, body.word):
            text = f"(asked while viewing {m.page.word} in {m.page.lang}) {text}"
        messages.append({"role": m.role, "content": text})

    async def gen() -> AsyncIterator[bytes]:
        async for ev in converse(context, messages, cfg):
            yield (json.dumps(ev, ensure_ascii=False) + "\n").encode()

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


async def converse(context: str, messages: list[dict], cfg: settings.Settings) -> AsyncIterator[dict]:
    """Drive one user turn to completion, streaming text and running tools."""
    system = [
        {"type": "text", "text": SYSTEM},
        # the page is the same for every follow-up question: cache it
        {"type": "text", "text": context, "cache_control": {"type": "ephemeral"}},
    ]
    json_retries = 0
    for _round in range(cfg.max_tool_rounds + 1):
        try:
            async with client().messages.stream(
                model=cfg.model,
                max_tokens=MAX_TOKENS,
                system=system,
                tools=TOOLS,
                thinking={"type": "adaptive"},
                output_config={"effort": cfg.effort},
                messages=messages,
            ) as stream:
                async for event in stream:
                    if event.type == "text":
                        yield {"type": "text", "text": event.text}
                final = await stream.get_final_message()
            json_retries = 0
        except ValueError:
            # the SDK could not parse a streamed tool input at all; re-issue the turn
            json_retries += 1
            if json_retries > 2:
                yield {"type": "error", "message": "Claude sent a malformed tool call twice; please ask again."}
                return
            continue
        except anthropic.AuthenticationError:
            yield {"type": "error", "message": "The server's Anthropic API key was rejected."}
            return
        except anthropic.RateLimitError:
            yield {"type": "error", "message": "Anthropic is rate-limiting us; try again in a minute."}
            return
        except anthropic.APIStatusError as e:
            log.warning("ask: API error %s: %s", e.status_code, e.message)
            yield {"type": "error", "message": f"Anthropic API error ({e.status_code})."}
            return
        except anthropic.APIConnectionError:
            yield {"type": "error", "message": "Could not reach Anthropic."}
            return

        if final.stop_reason == "refusal":
            yield {"type": "error", "message": "Claude declined to answer that."}
            return
        tool_uses = [b for b in final.content if b.type == "tool_use"]
        if not tool_uses:
            yield {"type": "done", "stop": final.stop_reason, "usage": _usage(final)}
            return
        if final.stop_reason == "max_tokens":
            yield {"type": "error", "message": "The answer ran too long and was cut off."}
            return
        if _round == cfg.max_tool_rounds:
            yield {"type": "error", "message": "Too many database lookups for one question; try a narrower one."}
            return

        results = []
        for tu in tool_uses:
            if not valid_input(tu.name, tu.input):
                results.append({"type": "tool_result", "tool_use_id": tu.id, "is_error": True,
                                "content": json.dumps({"INVALID_JSON": json.dumps(tu.input)})})
                continue
            yield {"type": "tool", "name": tu.name, "input": tu.input}
            try:
                out = await asyncio.to_thread(run_tool, tu.name, tu.input)
            except Exception as e:  # noqa: BLE001 — surface to the model, keep the stream alive
                log.exception("ask: tool %s failed", tu.name)
                out, err = f"error: {e}", True
            else:
                err = out.startswith("error:")
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out, "is_error": err})
        messages.append({"role": "assistant", "content": final.content})
        messages.append({"role": "user", "content": results})


def _usage(msg) -> dict:
    u = msg.usage
    return {
        "input": u.input_tokens, "output": u.output_tokens,
        "cache_read": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_write": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


# ---------------------------------------------------------------------------
# admin: /admin page + /api/admin/settings, guarded by the admin passcode
# ---------------------------------------------------------------------------

ADMIN_FAILS = (10, 600)    # wrong passcodes per IP per window before we stop checking
_admin_fails: dict[str, deque] = defaultdict(deque)


def check_admin(request: Request) -> None:
    if not settings.ADMIN_PASSCODE:
        raise HTTPException(503, "no admin passcode configured on this server")
    ip = request.client.host if request.client else "?"
    n, window = ADMIN_FAILS
    now = time.monotonic()
    q = _admin_fails[ip]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= n:
        raise HTTPException(429, "too many wrong passcodes; wait a few minutes")
    if not hmac.compare_digest(bearer(request), settings.ADMIN_PASSCODE):
        q.append(now)
        raise HTTPException(401, "wrong passcode")


def _admin_view() -> dict:
    cfg = settings.current()
    return {
        "settings": cfg.model_dump(),
        "defaults": settings.defaults().model_dump(),
        "models": settings.MODELS,
        "api_key_present": bool(API_KEY),
        "settings_path": str(settings.SETTINGS_PATH),
    }


@router.get("/api/admin/settings")
def admin_get(request: Request):
    check_admin(request)
    return _admin_view()


@router.put("/api/admin/settings")
def admin_put(update: dict, request: Request):
    check_admin(request)
    try:
        settings.save(update)
    except ValidationError as e:
        raise HTTPException(422, "; ".join(f"{'.'.join(map(str, err['loc']))}: {err['msg']}" for err in e.errors()))
    except OSError as e:
        raise HTTPException(500, f"could not write settings: {e}")
    return _admin_view()
