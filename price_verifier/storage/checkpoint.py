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
from collections import Counter
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
RESOLVED_BY_OFFERS = "offers"   # Amazon's "all offers" page, once validated for the run

UNKNOWN_BRAND = "Unknown Brand"


def effective_brand(row: dict) -> str:
    """Brand a row is grouped under: the uploaded brand if non-blank, else
    the brand read off the product page, else "Unknown Brand". Brand is
    optional at upload, so this is what the report/sheets/downloads should
    key on."""
    for key in ("brand", "scraped_brand"):
        val = row.get(key)
        if val is not None and str(val).strip():
            return str(val).strip()
    return UNKNOWN_BRAND



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
    use_browser: bool = True


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
                    tolerance_abs, tolerance_pct, concurrency, total_rows, use_browser)
                   VALUES (?, ?, 'running', ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    run_id, _now(), run_cfg.input_filename, run_cfg.pincode,
                    run_cfg.price_source, run_cfg.tolerance_abs, run_cfg.tolerance_pct,
                    run_cfg.concurrency, len(items), 1 if run_cfg.use_browser else 0,
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
        # One IMMEDIATE transaction (takes the write lock up front): the row
        # update and the recount must not interleave with another row's, or
        # a recount that read the table before the other row's update could
        # be written AFTER it — leaving stale totals (e.g. failed=1 when
        # every row matched, which also mislabels the Retry button).
        conn.execute("BEGIN IMMEDIATE")
        try:
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
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def mark_items_failed(
    run_id: str, rows: list[tuple[str, str, str, Optional[str]]], db_path: Path = config.DB_PATH
) -> None:
    """Batch form of mark_item_result(status=FAILED) for many rows in one
    transaction: rows are (asin, error_reason, url, resolved_by)."""
    if not rows:
        return
    now = _now()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany(
                """UPDATE run_items
                   SET status = 'failed', actual_price = NULL, mrp = NULL, seller = NULL, product_title = NULL,
                       url = ?, error_reason = ?, checked_at = ?, scraped_brand = NULL, resolved_by = ?
                   WHERE run_id = ? AND asin = ?""",
                [(url, reason, now, rb, run_id, asin) for asin, reason, url, rb in rows],
            )
            _recompute_run_counts(conn, run_id)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


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
    _canonicalize_brands(out)
    return out


def _brand_key(name: str) -> str:
    return " ".join(name.split()).casefold()


def _canonicalize_brands(rows: list[dict]) -> None:
    """"Lapcare", "LAPCARE" and "lapcare " are one brand — one sheet, one
    download, one email. Every spelling of a brand is replaced by its most
    common spelling in this run (ties: the one with the most capitals
    variety, i.e. "Lapcare" over "LAPCARE"/"lapcare", then alphabetical).
    Only valid for grouping over a WHOLE run's rows, which is why it is
    applied here and not in effective_brand()."""
    spellings: dict[str, Counter] = {}
    for d in rows:
        name = d["effective_brand"]
        spellings.setdefault(_brand_key(name), Counter())[" ".join(name.split())] += 1
    canon = {}
    for key, counts in spellings.items():
        canon[key] = sorted(
            counts.items(),
            key=lambda kv: (-kv[1], not (kv[0] != kv[0].upper() and kv[0] != kv[0].lower()), kv[0]),
        )[0][0]
    for d in rows:
        d["effective_brand"] = canon[_brand_key(d["effective_brand"])]


# Statuses that represent a genuine pricing/listing problem worth emailing a
# seller about — distinct from STATUS_FAILED, which just means "we couldn't
# check this one," not "there's a discrepancy." Excel report + per-brand
# downloads both key off this set.
ISSUE_STATUSES = (
    STATUS_MISMATCHED, STATUS_OUT_OF_STOCK, STATUS_UNAVAILABLE, STATUS_NOT_FOUND, STATUS_NO_FEATURED_OFFER,
)


def get_brands_with_issues(run_id: str, db_path: Path = config.DB_PATH) -> list[str]:
    """Distinct EFFECTIVE brands (see effective_brand(), case-insensitively
    merged) that have at least one issue row, alphabetical — drives the
    per-brand download links on the results page."""
    return [b["brand"] for b in get_brand_issue_counts(run_id, db_path=db_path)]


def get_brand_issue_counts(run_id: str, db_path: Path = config.DB_PATH) -> list[dict]:
    """[{"brand": ..., "issues": n}] per effective brand with at least one
    issue row, alphabetical — the results page's per-brand download list."""
    counts: Counter = Counter(
        r["effective_brand"] for r in get_run_items(run_id, db_path=db_path) if r["status"] in ISSUE_STATUSES
    )
    return [{"brand": b, "issues": n} for b, n in sorted(counts.items(), key=lambda kv: kv[0].casefold())]
