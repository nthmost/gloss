"""Runtime settings for the ask feature, editable from /admin.

Defaults come from the environment; anything saved from the admin page is
overlaid from a JSON file (GLOSS_SETTINGS, default data/settings.json). The
file is re-read whenever its mtime changes, so every uvicorn worker sees an
edit made through any of them.

Environment:
    GLOSS_SETTINGS              path of the JSON overlay
    GLOSS_ADMIN_PASSCODE        admin passcode, or
    GLOSS_ADMIN_PASSCODE_FILE   a file holding it (default: admin_passcode in the repo root)
    GLOSS_ASK_MODEL / GLOSS_ASK_EFFORT / GLOSS_ASK_TOKEN   defaults for the fields below
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

ROOT = Path(__file__).resolve().parent.parent
SETTINGS_PATH = Path(os.environ.get("GLOSS_SETTINGS", "data/settings.json"))

MODELS = ["claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"]


class Settings(BaseModel):
    ask_enabled: bool = True
    token_required: bool = False
    token: str = Field(default="", max_length=200)
    model: str = Field(default="claude-sonnet-5", min_length=1, max_length=80, pattern=r"^[a-z0-9.-]+$")
    effort: Literal["low", "medium", "high"] = "medium"
    rate_limit_n: int = Field(default=30, ge=1, le=10000)
    rate_limit_minutes: int = Field(default=10, ge=1, le=1440)
    max_tool_rounds: int = Field(default=8, ge=0, le=20)

    @property
    def gate_on(self) -> bool:
        return self.token_required and bool(self.token)


def _read_secret(env: str, file_env: str, default_name: str | None = None) -> str | None:
    if os.environ.get(env):
        return os.environ[env].strip()
    path = os.environ.get(file_env)
    if not path and default_name and (ROOT / default_name).exists():
        path = str(ROOT / default_name)
    if path and os.path.exists(path):
        return Path(path).read_text().strip() or None
    return None


ADMIN_PASSCODE = _read_secret("GLOSS_ADMIN_PASSCODE", "GLOSS_ADMIN_PASSCODE_FILE", "admin_passcode")


def defaults() -> Settings:
    tok = os.environ.get("GLOSS_ASK_TOKEN", "").strip()
    kw = {"token": tok, "token_required": bool(tok)}
    if os.environ.get("GLOSS_ASK_MODEL"):
        kw["model"] = os.environ["GLOSS_ASK_MODEL"]
    if os.environ.get("GLOSS_ASK_EFFORT"):
        kw["effort"] = os.environ["GLOSS_ASK_EFFORT"]
    return Settings(**kw)


_lock = threading.Lock()
_cache: tuple[int | None, Settings] | None = None


def _mtime() -> int | None:
    try:
        return SETTINGS_PATH.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def current() -> Settings:
    """Effective settings: env defaults overlaid with the saved file."""
    global _cache
    mt = _mtime()
    with _lock:
        if _cache is not None and _cache[0] == mt:
            return _cache[1]
        s = defaults()
        if mt is not None:
            try:
                data = json.loads(SETTINGS_PATH.read_text())
                s = s.model_copy(update=Settings(**{**s.model_dump(), **data}).model_dump())
            except (OSError, ValueError, ValidationError):
                pass  # a bad file falls back to defaults rather than taking the site down
        _cache = (mt, s)
        return s


def save(update: dict) -> Settings:
    """Validate `update` on top of the current settings and write it atomically."""
    global _cache
    s = Settings(**{**current().model_dump(), **update})  # raises ValidationError on bad input
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s.model_dump(), indent=2) + "\n")
    os.replace(tmp, SETTINGS_PATH)
    with _lock:
        _cache = None
    return current()
