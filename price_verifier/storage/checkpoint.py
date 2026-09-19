"""
checkpoint.py — run/run_item persistence.

Every function here is a single short-lived connection + one statement, on
purpose: the pipeline calls `mark_item_result` once per completed row, from
whichever asyncio task finished it, with no batching. That per-row write IS
the crash-safety guarantee the spec asks for — there is no separate
"checkpoint every N rows" step to forget to call.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from price_verifier import config
from price_verifier.storage.db import get_conn, init_db

STATUS_PENDING = "pending"
STATUS_MATCHED = "matched"
STATUS_MISMATCHED = "mismatched"
STATUS_OUT_OF_STOCK = "out_of_stock"
STATUS_UNAVAILABLE = "unavailable"
STATUS_NOT_FOUND = "not_found"
STATUS_FAILED = "failed"

TERMINAL_STATUSES = {
    STATUS_MATCHED,
    STATUS_MISMATCHED,
    STATUS_OUT_OF_STOCK,
    STATUS_UNAVAILABLE,
    STATUS_NOT_FOUND,
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunItemRow:
    asin: str
    expected_price: float
    actual_price: Optional[float] = None
    product_title: Optional[str] = None
    url: Optional[str] = None
    status: str = STATUS_PENDING
    error_reason: Optional[str] = None
    attempts: int = 0
    checked_at: Optional[str] = None


@dataclass
class RunConfig:
    input_filename: str
    pincode: str
    price_source: str = config.DEFAULT_PRICE_SOURCE
    tolerance_abs: float = config.DEFAULT_TOLERANCE_ABS
    tolerance_pct: float = config.DEFAULT_TOLERANCE_PCT
    concurrency: int = config.DEFAULT_CONCURRENCY


def create_run(run_cfg: RunConfig, items: list[RunItemRow], db_path: Path = config.DB_PATH) -> str:
    """Create a new run and its pending rows in one transaction. Returns run_id."""
    init_db(db_path)
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    with get_conn(db_path) as conn:
        conn.execute("BEGIN")
        try:
            conn.execute(
                """INSERT INTO runs
                   (run_id, started_at, status, input_filename, pincode, price_source,
                    tolerance_abs, tolerance_pct, concurrency, total_rows)
                   VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, _now(), run_cfg.input_filename, run_cfg.pincode,
                    run_cfg.price_source, run_cfg.tolerance_abs, run_cfg.tolerance_pct,
                    run_cfg.concurrency, len(items),
                ),
            )
            conn.executemany(
                """INSERT INTO run_items (run_id, asin, expected_price, status)
                   VALUES (?, ?, ?, 'pending')""",
                [(run_id, it.asin, it.expected_price) for it in items],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return run_id


def get_pending_and_retryable_items(
    run_id: str, db_path: Path = config.DB_PATH
) -> list[RunItemRow]:
    """Rows to (re)process on a fresh start or a resume: still pending, or
    already failed. Matched/mismatched/OOS/etc. rows are terminal and are
    never re-queued — that's what makes resume "only pending and failed
    rows" per the spec, not a full re-run.

    Unlike the in-run retry cap (MAX_ATTEMPTS, enforced by the runner's own
    loop within a single processing pass), a resume is a deliberate user
    action each time, so a failed row always gets a fresh attempt budget on
    resume rather than being permanently capped by a lifetime attempts count
    — `attempts` here is a reporting figure (spec's Failed-sheet column),
    not a gate.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT asin, expected_price, attempts FROM run_items
               WHERE run_id = ? AND status IN ('pending', 'failed')
               ORDER BY asin""",
            (run_id,),
        ).fetchall()
    return [RunItemRow(asin=r["asin"], expected_price=r["expected_price"], attempts=r["attempts"]) for r in rows]


def record_attempt(run_id: str, asin: str, db_path: Path = config.DB_PATH) -> None:
    """Bump the attempts counter for one fetch try. Called once per actual
    HTTP attempt (success or failure alike) — separate from mark_item_result
    so a row's attempts count reflects real tries made, not just how many
    times the row transitioned status."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE run_items SET attempts = attempts + 1 WHERE run_id = ? AND asin = ?",
            (run_id, asin),
        )


def mark_item_result(
    run_id: str,
    asin: str,
    status: str,
    actual_price: Optional[float] = None,
    product_title: Optional[str] = None,
    url: Optional[str] = None,
    error_reason: Optional[str] = None,
    db_path: Path = config.DB_PATH,
) -> None:
    """Write-through the instant a row reaches a terminal status for this
    processing pass. Does NOT touch `attempts` — call record_attempt()
    separately for each fetch try; this only sets the final outcome."""
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE run_items
               SET status = ?, actual_price = ?, product_title = ?, url = ?,
                   error_reason = ?, checked_at = ?
               WHERE run_id = ? AND asin = ?""",
            (status, actual_price, product_title, url, error_reason, _now(), run_id, asin),
        )
        # Recompute counts from run_items rather than incrementing blindly —
        # a row retried across a resume (failed -> matched) must not double-count.
        _recompute_run_counts(conn, run_id)


def _recompute_run_counts(conn, run_id: str) -> None:
    counts = conn.execute(
        """SELECT
             SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) AS matched,
             SUM(CASE WHEN status = 'mismatched' THEN 1 ELSE 0 END) AS mismatched,
             SUM(CASE WHEN status IN ('out_of_stock','unavailable','not_found') THEN 1 ELSE 0 END) AS oos,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
           FROM run_items WHERE run_id = ?""",
        (run_id,),
    ).fetchone()
    conn.execute(
        """UPDATE runs SET matched=?, mismatched=?, out_of_stock=?, failed=? WHERE run_id=?""",
        (counts["matched"] or 0, counts["mismatched"] or 0, counts["oos"] or 0, counts["failed"] or 0, run_id),
    )


def finish_run(run_id: str, output_path: Optional[str], db_path: Path = config.DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE runs SET status='completed', finished_at=?, output_path=? WHERE run_id=?",
            (_now(), output_path, run_id),
        )


def mark_run_crashed(run_id: str, db_path: Path = config.DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute("UPDATE runs SET status='crashed' WHERE run_id=? AND status='running'", (run_id,))


def find_incomplete_run(db_path: Path = config.DB_PATH) -> Optional[dict]:
    """Called on app startup. A run left 'running' means the process died
    mid-run (crash, kill, power loss) without reaching finish_run — the
    resume prompt the spec asks for."""
    init_db(db_path)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def list_runs(limit: int = 50, db_path: Path = config.DB_PATH) -> list[dict]:
    init_db(db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM runs ORDER BY started_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_run(run_id: str, db_path: Path = config.DB_PATH) -> Optional[dict]:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
    return dict(row) if row else None


def get_run_items(run_id: str, status: Optional[str] = None, db_path: Path = config.DB_PATH) -> list[dict]:
    with get_conn(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? AND status = ? ORDER BY asin", (run_id, status)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? ORDER BY asin", (run_id,)
            ).fetchall()
    return [dict(r) for r in rows]
