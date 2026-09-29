"""
debug_dump.py — keep the raw HTML of pages we couldn't make sense of.

When a live run misclassifies a page (a new block-page wording, a layout the
selectors don't know), the only way to fix parser.py is to look at exactly
what Amazon sent. This module saves such pages under

    config.DEBUG_HTML_DIR/<YYYYMMDD>/<asin>_<reason>_<source>_<HHMMSS>.html

with a per-day cap so a run that goes sideways (every row blocked) can't
fill the disk, and a prune helper to drop old day folders.

Both functions are best-effort diagnostics: they never raise.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from price_verifier import config

log = logging.getLogger(__name__)

MAX_FILES_PER_DAY = 500
_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")
_DAY_DIR_RE = re.compile(r"^\d{8}$")


def _safe_part(value, default: str, max_len: int = 40) -> str:
    text = _SAFE_RE.sub("-", str(value or "")).strip("-_")
    return (text[:max_len] or default)


def save_debug_html(asin: str, html: str | None, reason: str, source: str = "http") -> Path | None:
    """Write `html` for later inspection. Returns the file path, or None if
    skipped (daily cap reached) or anything went wrong."""
    try:
        now = datetime.now()
        day_dir = Path(config.DEBUG_HTML_DIR) / now.strftime("%Y%m%d")
        day_dir.mkdir(parents=True, exist_ok=True)

        try:
            existing = sum(1 for p in day_dir.iterdir() if p.suffix == ".html")
        except OSError:
            existing = 0
        if existing >= MAX_FILES_PER_DAY:
            log.debug("debug html cap (%d) reached for %s; not saving %s", MAX_FILES_PER_DAY, day_dir, asin)
            return None

        stem = "_".join((
            _safe_part(asin, "noasin", 20),
            _safe_part(reason, "unknown"),
            _safe_part(source, "src", 16),
            now.strftime("%H%M%S"),
        ))
        path = day_dir / f"{stem}.html"
        n = 1
        while path.exists():  # same asin/reason within the same second
            path = day_dir / f"{stem}-{n}.html"
            n += 1

        body = html if html is not None else "<!-- price_verifier: no HTML body was received -->"
        path.write_text(body, encoding="utf-8", errors="replace")
        return path
    except Exception:
        log.debug("save_debug_html failed for %s", asin, exc_info=True)
        return None


MAX_TOTAL_BYTES = 200 * 1024 * 1024


def prune_debug_html(max_age_days: int = 7, max_total_bytes: int = MAX_TOTAL_BYTES) -> int:
    """Delete day folders older than `max_age_days`, then the oldest saved
    pages until the folder is under `max_total_bytes`. Called at app start
    AND at the start of every run (the app may stay open for weeks). Returns
    how many folders/files were removed. Folders not named YYYYMMDD are left
    alone."""
    removed = 0
    try:
        root = Path(config.DEBUG_HTML_DIR)
        if not root.is_dir():
            return 0
        cutoff = (datetime.now() - timedelta(days=max_age_days)).date()
        for child in root.iterdir():
            try:
                if not child.is_dir() or not _DAY_DIR_RE.match(child.name):
                    continue
                day = datetime.strptime(child.name, "%Y%m%d").date()
                if day < cutoff:
                    shutil.rmtree(child, ignore_errors=True)
                    removed += 1
            except Exception:
                log.debug("could not prune %s", child, exc_info=True)
        files = sorted((p for p in root.glob("*/*.html") if _DAY_DIR_RE.match(p.parent.name)),
                       key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in files)
        for p in files:
            if total <= max_total_bytes:
                break
            try:
                size = p.stat().st_size
                p.unlink()
                total -= size
                removed += 1
            except OSError:
                pass
    except Exception:
        log.debug("prune_debug_html failed", exc_info=True)
    return removed
