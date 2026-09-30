"""
session_store.py — carry one known-good browser identity over to the next run.

Amazon trusts a returning visitor more than a brand-new one. Instead of
starting every run with an empty cookie jar, the pipeline saves the cookies
(and the matching Chrome fingerprint) of a session that successfully loaded
product pages, and the next run starts from it.

Rules that keep this safe:
  * only a session that actually served product pages is saved (never one
    that was blocked — a blocked identity is rotated away before it can be);
  * take() consumes the file: the saved identity is offered to exactly one
    session, and if Amazon blocks it the normal rotation replaces it with a
    fresh one and nothing stale is ever re-used;
  * it expires after MAX_AGE_HOURS, and only applies to the same marketplace
    URL and HTTP backend it was saved from.

Best effort: every function swallows its own errors.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

from price_verifier import config

log = logging.getLogger(__name__)

MAX_AGE_HOURS = 12.0
STATE_VERSION = 1


def _default_path() -> Path:
    return Path(config.DATA_DIR) / "session_state.json"


def save(state: Optional[dict], path: Optional[Path] = None) -> bool:
    if not state:
        return False
    path = Path(path or _default_path())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = dict(state, version=STATE_VERSION, saved_at=time.time())
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)  # atomic: a crash never leaves half a file
        return True
    except Exception:
        log.debug("could not save session state", exc_info=True)
        return False


def take(path: Optional[Path] = None, *, base_url: Optional[str] = None,
         max_age_hours: float = MAX_AGE_HOURS) -> Optional[dict]:
    """Return the saved state (and delete it), or None if absent, expired,
    unreadable or saved for a different marketplace URL."""
    path = Path(path or _default_path())
    try:
        if not path.exists():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
            return None
        if time.time() - float(state.get("saved_at", 0)) > max_age_hours * 3600:
            return None
        want = (base_url or config.MARKETPLACE_BASE_URL).rstrip("/")
        if state.get("base_url") != want:
            return None
        if not isinstance(state.get("cookies"), list) or not state["cookies"]:
            return None
        return state
    except Exception:
        log.debug("could not read session state", exc_info=True)
        return None
