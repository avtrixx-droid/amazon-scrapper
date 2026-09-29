"""
db.py — SQLite schema, connection management, and in-place schema migration.

WAL mode is enabled so the pipeline's many concurrent row-completion writes
don't serialize behind a single lock the way rollback-journal mode would.
Each write is its own transaction (autocommit via context manager) so a
crash loses at most the one in-flight row per worker — this IS the
checkpoint, not a separate mechanism bolted on afterward.

Migration: vendors already have databases created by older builds of this
tool (the first cut had no brand/mrp/seller columns at all; the next one had
no scraped_brand/resolved_by/phase). `CREATE TABLE IF NOT EXISTS` does
nothing for an existing table, so `init_db` compares each table's actual
columns (`PRAGMA table_info`) against `_COLUMNS` below and `ALTER TABLE ...
ADD COLUMN`s whatever is missing. SQLite only allows adding a NOT NULL column
if it has a non-NULL DEFAULT, which is why every NOT NULL column in
`_COLUMNS` carries one. Indexes are created only AFTER the migration, since
an index on a column an old table doesn't have yet would fail the whole
script.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from price_verifier import config

# (column name, column definition) — the single source of truth for both the
# CREATE TABLE statement and the add-missing-columns migration. Order matters
# only for fresh databases (it's the physical column order).
_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "runs": [
        ("run_id", "TEXT PRIMARY KEY"),
        ("started_at", "TEXT NOT NULL"),
        ("finished_at", "TEXT"),
        ("status", "TEXT NOT NULL"),                        # running | completed | crashed | cancelled
        ("input_filename", "TEXT NOT NULL"),
        ("pincode", "TEXT NOT NULL DEFAULT 'N/A'"),          # informational only
        ("price_source", "TEXT NOT NULL DEFAULT 'buybox'"),  # buybox | lowest
        ("tolerance_abs", "REAL NOT NULL DEFAULT 1.0"),
        ("tolerance_pct", "REAL NOT NULL DEFAULT 0.0"),
        ("concurrency", "INTEGER NOT NULL DEFAULT 15"),
        ("total_rows", "INTEGER NOT NULL DEFAULT 0"),
        ("matched", "INTEGER NOT NULL DEFAULT 0"),
        ("mismatched", "INTEGER NOT NULL DEFAULT 0"),
        ("out_of_stock", "INTEGER NOT NULL DEFAULT 0"),      # OOS + unavailable + not found + no featured offer
        ("failed", "INTEGER NOT NULL DEFAULT 0"),
        ("output_path", "TEXT"),
        ("phase", "TEXT"),                                   # fast | recovery | browser | done | cancelled
        ("stats_json", "TEXT"),                              # runner.RunStats.as_dict() of the latest pass
    ],
    "run_items": [
        ("run_id", "TEXT NOT NULL REFERENCES runs(run_id)"),
        ("asin", "TEXT NOT NULL"),
        ("brand", "TEXT NOT NULL DEFAULT ''"),               # uploaded brand; may be blank (optional at upload)
        ("expected_price", "REAL NOT NULL"),
        ("actual_price", "REAL"),
        ("mrp", "REAL"),
        ("seller", "TEXT"),                                  # who to email about a discrepancy
        ("product_title", "TEXT"),
        ("url", "TEXT"),
        ("status", "TEXT NOT NULL DEFAULT 'pending'"),
        ("error_reason", "TEXT"),                            # final reason, or last attempt error while pending
        ("attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("checked_at", "TEXT"),
        ("scraped_brand", "TEXT"),                           # brand read off the product page
        ("resolved_by", "TEXT"),                             # http | recovery | browser
        ("last_attempt_at", "TEXT"),
    ],
}

_TABLE_CONSTRAINTS = {
    "runs": [],
    "run_items": ["PRIMARY KEY (run_id, asin)"],
}

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_run_items_run_status ON run_items(run_id, status);
CREATE INDEX IF NOT EXISTS idx_run_items_run_brand ON run_items(run_id, brand);
"""


def _create_table_sql(table: str) -> str:
    parts = [f"    {name} {definition}" for name, definition in _COLUMNS[table]]
    parts += [f"    {c}" for c in _TABLE_CONSTRAINTS[table]]
    return f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(parts) + "\n);\n"


SCHEMA = "".join(_create_table_sql(t) for t in _COLUMNS) + _INDEXES


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _existing_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def migrate(conn: sqlite3.Connection) -> list[str]:
    """Add any column in `_COLUMNS` that an existing table lacks. Returns the
    list of "table.column" names added (empty for an up-to-date DB). Only
    ever ADDs columns — never drops, renames, or rewrites data — so it is
    safe to run on every startup and on a DB another build is also using."""
    added: list[str] = []
    for table, columns in _COLUMNS.items():
        existing = _existing_columns(conn, table)
        for name, definition in columns:
            if name in existing:
                continue
            # PRIMARY KEY / REFERENCES can't be added via ALTER; those columns
            # exist in every schema version this tool ever shipped anyway.
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
            added.append(f"{table}.{name}")
    return added


def init_db(db_path: Path = config.DB_PATH) -> list[str]:
    """Create tables if absent, migrate older schemas forward, then ensure
    indexes. Idempotent. Returns the columns the migration added."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(db_path)
    try:
        conn.executescript("".join(_create_table_sql(t) for t in _COLUMNS))
        conn.execute("BEGIN IMMEDIATE")
        try:
            added = migrate(conn)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        conn.executescript(_INDEXES)
        return added
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
