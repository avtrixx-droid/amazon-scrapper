"""
db.py — SQLite schema and connection management.

WAL mode is enabled so the pipeline's many concurrent row-completion writes
don't serialize behind a single lock the way rollback-journal mode would.
Each write is its own transaction (autocommit via context manager) so a
crash loses at most the one in-flight row per worker — this IS the
checkpoint, not a separate mechanism bolted on afterward.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from price_verifier import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,      -- running | completed | crashed | cancelled
    input_filename  TEXT NOT NULL,
    pincode         TEXT NOT NULL DEFAULT 'N/A',  -- informational only — price doesn't vary by pincode for this catalog
    price_source    TEXT NOT NULL,      -- buybox | lowest
    tolerance_abs   REAL NOT NULL,
    tolerance_pct   REAL NOT NULL,
    concurrency     INTEGER NOT NULL,
    total_rows      INTEGER NOT NULL,
    matched         INTEGER NOT NULL DEFAULT 0,
    mismatched      INTEGER NOT NULL DEFAULT 0,
    out_of_stock    INTEGER NOT NULL DEFAULT 0,
    failed          INTEGER NOT NULL DEFAULT 0,
    output_path     TEXT
);

CREATE TABLE IF NOT EXISTS run_items (
    run_id          TEXT NOT NULL REFERENCES runs(run_id),
    asin            TEXT NOT NULL,
    brand           TEXT NOT NULL,      -- groups the Excel output into one sheet per brand
    expected_price  REAL NOT NULL,
    actual_price    REAL,
    mrp             REAL,
    seller          TEXT,               -- who to email about a discrepancy (e.g. "Coco Blue Retail")
    product_title   TEXT,
    url             TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
                    -- pending | matched | mismatched | out_of_stock | unavailable | not_found | failed
    error_reason    TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    checked_at      TEXT,
    PRIMARY KEY (run_id, asin)
);

CREATE INDEX IF NOT EXISTS idx_run_items_run_status ON run_items(run_id, status);
CREATE INDEX IF NOT EXISTS idx_run_items_run_brand ON run_items(run_id, brand);
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path = config.DB_PATH) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def get_conn(db_path: Path = config.DB_PATH) -> Iterator[sqlite3.Connection]:
    """One connection per call — sqlite3 connections aren't safe to share
    across asyncio tasks/threads, and WAL mode makes short-lived connections
    cheap enough that this isn't a bottleneck at this run's scale."""
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()
