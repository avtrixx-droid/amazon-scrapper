"""
checkpoint.py — run/run_item persistence.

Every function here is a single short-lived connection + one statement, on
purpose: the pipeline calls `mark_item_result` once per completed row, from
whichever asyncio task finished it, with no batching. That per-row write IS
the crash-safety guarantee the spec asks for — there is no separate
"checkpoint every N rows" step to forget to call.

Row lifecycle: a row is 'pending' until the pipeline finalizes it exactly
once per pipeline pass with mark_item_result. While it is being retried,
record_attempt() bumps its attempt counter and note_item_attempt_failure()
stores the latest error — both leave the status alone, so a crash mid-run
still leaves a diagnostic on the row and resume still re-queues it.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
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
STATUS_NO_FEATURED_OFFER = "no_featured_offer"  # product exists, but no buy-box winner ("See All Buying Options")
STATUS_FAILED = "failed"

TERMINAL_STATUSES = {
    STATUS_MATCHED,
    STATUS_MISMATCHED,
    STATUS_OUT_OF_STOCK,
    STATUS_UNAVAILABLE,
    STATUS_NOT_FOUND,
    STATUS_NO_FEATURED_OFFER,
}

# Statuses counted in the runs.out_of_stock bucket (and the UI's "OOS /
# unavailable" tally): the listing can't be bought at a price right now.
OUT_OF_STOCK_BUCKET = (STATUS_OUT_OF_STOCK, STATUS_UNAVAILABLE, STATUS_NOT_FOUND, STATUS_NO_FEATURED_OFFER)

# Which fetch path produced a row's final answer (run_items.resolved_by).
RESOLVED_BY_HTTP = "http"
RESOLVED_BY_RECOVERY = "recovery"
RESOLVED_BY_BROWSER = "browser"

UNKNOWN_BRAND = "Unknown Brand"


def effective_brand(row: dict) -> str:
    """Brand a row is grouped under: the uploaded brand if non-blank, else
    the brand read off the product page, else "Unknown Brand". Brand is
    optional at upload, so this is what the report/sheets/downloads should
    key on. Kept consistent with _EFFECTIVE_BRAND_SQL (same trimming)."""
    for key in ("brand", "scraped_brand"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return UNKNOWN_BRAND


_EFFECTIVE_BRAND_SQL = (
    f"COALESCE(NULLIF(TRIM(brand), ''), NULLIF(TRIM(scraped_brand), ''), '{UNKNOWN_BRAND}')"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunItemRow:
    asin: str
    expected_price: float
    brand: str = ""  # uploaded brand; optional (may be blank) — see effective_brand()
    actual_price: Optional[float] = None
    mrp: Optional[float] = None
    seller: Optional[str] = None
    product_title: Optional[str] = None
    url: Optional[str] = None
    status: str = STATUS_PENDING
    error_reason: Optional[str] = None
    attempts: int = 0
    checked_at: Optional[str] = None


@dataclass
class RunConfig:
    input_filename: str
    # Informational only — the vendor confirmed price doesn't vary by
    # pincode for this catalog, so this is no longer collected from the UI.
    pincode: str = "N/A"
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
                """INSERT INTO run_items (run_id, asin, brand, expected_price, status)
                   VALUES (?, ?, ?, ?, 'pending')""",
                [(run_id, it.asin, (it.brand or "").strip(), it.expected_price) for it in items],
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

    A resume / retry is a deliberate user action each time, so a failed row
    always gets a fresh attempt budget rather than being permanently capped
    by a lifetime attempts count — `attempts` is a reporting figure, not a
    gate.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """SELECT asin, expected_price, brand, attempts FROM run_items
               WHERE run_id = ? AND status IN ('pending', 'failed')
               ORDER BY asin""",
            (run_id,),
        ).fetchall()
    return [
        RunItemRow(asin=r["asin"], expected_price=r["expected_price"], brand=r["brand"] or "", attempts=r["attempts"])
        for r in rows
    ]


def reopen_run_for_retry(run_id: str, db_path: Path = config.DB_PATH) -> list[RunItemRow]:
    """Backs a "Retry failed rows" button: flips a finished run back to
    'running' (so a crash mid-retry is picked up by find_incomplete_run) and
    returns the rows to re-process — the same pending + failed set a resume
    uses. Terminal answers are never re-fetched. Call finish_run() after the
    pipeline completes, exactly as for a fresh run."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE runs SET status='running', finished_at=NULL, phase=NULL WHERE run_id=?",
            (run_id,),
        )
    return get_pending_and_retryable_items(run_id, db_path=db_path)


def record_attempt(run_id: str, asin: str, db_path: Path = config.DB_PATH) -> None:
    """Bump the attempts counter for one fetch try. Called once per actual
    fetch attempt (success or failure alike) — separate from mark_item_result
    so a row's attempts count reflects real tries made, not just how many
    times the row transitioned status."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE run_items SET attempts = attempts + 1, last_attempt_at = ? WHERE run_id = ? AND asin = ?",
            (_now(), run_id, asin),
        )


def note_item_attempt_failure(run_id: str, asin: str, error_reason: str, db_path: Path = config.DB_PATH) -> None:
    """Record why the latest attempt failed WITHOUT finalizing the row: the
    status is left as-is, so a crash mid-run still leaves a diagnostic on
    every row that was being retried, and resume still re-queues it. Never
    touches a row that already reached a terminal status."""
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE run_items SET error_reason = ?, last_attempt_at = ?
               WHERE run_id = ? AND asin = ? AND status IN ('pending', 'failed')""",
            (error_reason, _now(), run_id, asin),
        )


def mark_item_result(
    run_id: str,
    asin: str,
    status: str,
    actual_price: Optional[float] = None,
    mrp: Optional[float] = None,
    seller: Optional[str] = None,
    product_title: Optional[str] = None,
    url: Optional[str] = None,
    error_reason: Optional[str] = None,
    db_path: Path = config.DB_PATH,
    scraped_brand: Optional[str] = None,
    resolved_by: Optional[str] = None,
) -> None:
    """Write-through the instant a row reaches its final status for this
    processing pass. Does NOT touch `attempts` — call record_attempt()
    separately for each fetch try; this only sets the final outcome.
    `resolved_by` is which fetch path produced the answer (RESOLVED_BY_*);
    `scraped_brand` is the brand read off the product page, if any."""
    with get_conn(db_path) as conn:
        conn.execute(
            """UPDATE run_items
               SET status = ?, actual_price = ?, mrp = ?, seller = ?, product_title = ?, url = ?,
                   error_reason = ?, checked_at = ?, scraped_brand = ?, resolved_by = ?
               WHERE run_id = ? AND asin = ?""",
            (status, actual_price, mrp, seller, product_title, url, error_reason, _now(),
             scraped_brand, resolved_by, run_id, asin),
        )
        # Recompute counts from run_items rather than incrementing blindly —
        # a row retried across a resume (failed -> matched) must not double-count.
        _recompute_run_counts(conn, run_id)


def _recompute_run_counts(conn, run_id: str) -> None:
    oos_placeholders = ",".join("?" * len(OUT_OF_STOCK_BUCKET))
    counts = conn.execute(
        f"""SELECT
             SUM(CASE WHEN status = 'matched' THEN 1 ELSE 0 END) AS matched,
             SUM(CASE WHEN status = 'mismatched' THEN 1 ELSE 0 END) AS mismatched,
             SUM(CASE WHEN status IN ({oos_placeholders}) THEN 1 ELSE 0 END) AS oos,
             SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END) AS failed
           FROM run_items WHERE run_id = ?""",
        (*OUT_OF_STOCK_BUCKET, run_id),
    ).fetchone()
    conn.execute(
        """UPDATE runs SET matched=?, mismatched=?, out_of_stock=?, failed=? WHERE run_id=?""",
        (counts["matched"] or 0, counts["mismatched"] or 0, counts["oos"] or 0, counts["failed"] or 0, run_id),
    )


def set_run_phase(run_id: str, phase: str, db_path: Path = config.DB_PATH) -> None:
    """Which pass the pipeline is in (fast | recovery | browser | done |
    cancelled) — lets the UI/history show it, and survives a restart."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE runs SET phase=? WHERE run_id=?", (phase, run_id))


def set_run_stats(run_id: str, stats: dict, db_path: Path = config.DB_PATH) -> None:
    """Persist the runner's RunStats (as a dict) for the report summary /
    history page. Overwritten by each pipeline invocation over the run
    (a retry pass replaces the original pass's stats)."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE runs SET stats_json=? WHERE run_id=?", (json.dumps(stats, default=str), run_id))


def get_run_stats(run_id: str, db_path: Path = config.DB_PATH) -> Optional[dict]:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT stats_json FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None or not row["stats_json"]:
        return None
    try:
        return json.loads(row["stats_json"])
    except ValueError:
        return None


def finish_run(run_id: str, output_path: Optional[str], db_path: Path = config.DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE runs SET status='completed', finished_at=?, output_path=? WHERE run_id=?",
            (_now(), output_path, run_id),
        )


def mark_run_crashed(run_id: str, db_path: Path = config.DB_PATH) -> None:
    """Also backs the "Discard" button, so it accepts a paused run too."""
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE runs SET status='crashed' WHERE run_id=? AND status IN ('running', 'paused')", (run_id,)
        )


def mark_run_paused(run_id: str, db_path: Path = config.DB_PATH) -> None:
    """The user pressed Pause: the run stops cleanly, every finished row is
    already saved, and the home page offers to resume it."""
    with get_conn(db_path) as conn:
        conn.execute("UPDATE runs SET status='paused' WHERE run_id=? AND status='running'", (run_id,))


def find_incomplete_run(db_path: Path = config.DB_PATH) -> Optional[dict]:
    """Called on app startup. A run left 'running' means the process died
    mid-run (crash, kill, power loss) without reaching finish_run — the
    resume prompt the spec asks for. A run the user paused is offered the
    same way."""
    init_db(db_path)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM runs WHERE status IN ('running', 'paused') ORDER BY started_at DESC LIMIT 1"
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
    """Row dicts, each with an extra computed "effective_brand" key (see
    effective_brand()) so callers don't re-derive the grouping."""
    with get_conn(db_path) as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? AND status = ? ORDER BY asin", (run_id, status)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM run_items WHERE run_id = ? ORDER BY asin", (run_id,)
            ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["effective_brand"] = effective_brand(d)
        out.append(d)
    return out


# Statuses that represent a genuine pricing/listing problem worth emailing a
# seller about — distinct from STATUS_FAILED, which just means "we couldn't
# check this one," not "there's a discrepancy." Excel report + per-brand
# downloads both key off this set.
ISSUE_STATUSES = (
    STATUS_MISMATCHED, STATUS_OUT_OF_STOCK, STATUS_UNAVAILABLE, STATUS_NOT_FOUND, STATUS_NO_FEATURED_OFFER,
)


def get_brands_with_issues(run_id: str, db_path: Path = config.DB_PATH) -> list[str]:
    """Distinct EFFECTIVE brands (see effective_brand()) that have at least
    one issue row, alphabetical — drives the per-brand download links on the
    results page."""
    placeholders = ",".join("?" * len(ISSUE_STATUSES))
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT {_EFFECTIVE_BRAND_SQL} AS eb FROM run_items
                WHERE run_id = ? AND status IN ({placeholders})
                ORDER BY eb COLLATE NOCASE""",
            (run_id, *ISSUE_STATUSES),
        ).fetchall()
    return [r["eb"] for r in rows]


def get_brand_issue_counts(run_id: str, db_path: Path = config.DB_PATH) -> list[dict]:
    """[{"brand": ..., "issues": n}] per effective brand with at least one
    issue row, alphabetical — the results page's per-brand download list."""
    placeholders = ",".join("?" * len(ISSUE_STATUSES))
    with get_conn(db_path) as conn:
        rows = conn.execute(
            f"""SELECT {_EFFECTIVE_BRAND_SQL} AS eb, COUNT(*) AS n FROM run_items
                WHERE run_id = ? AND status IN ({placeholders})
                GROUP BY eb ORDER BY eb COLLATE NOCASE""",
            (run_id, *ISSUE_STATUSES),
        ).fetchall()
    return [{"brand": r["eb"], "issues": r["n"]} for r in rows]
