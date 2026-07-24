from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS companies (
      cik TEXT PRIMARY KEY,
      ticker TEXT NOT NULL,
      name TEXT,
      sic TEXT,
      exchange TEXT,
      is_candidate INTEGER DEFAULT 0,
      market_cap REAL,
      market_cap_asof TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS filings (
      cik TEXT,
      accession TEXT,
      fy INTEGER,
      filed_date TEXT,
      item1_path TEXT,
      item1_chars INTEGER,
      segments_json TEXT,
      PRIMARY KEY (cik, accession)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS shortlist (
      cik TEXT,
      bucket_id TEXT,
      similarity REAL,
      source TEXT,
      PRIMARY KEY (cik, bucket_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scores (
      cik TEXT,
      ticker TEXT,
      bucket_id TEXT,
      fy INTEGER,
      run INTEGER,
      score REAL,
      confidence TEXT,
      rationale TEXT,
      evidence_type TEXT,
      pre_revenue INTEGER,
      prompt_version TEXT,
      model_version TEXT,
      created_at TEXT,
      PRIMARY KEY (cik, bucket_id, fy, run, prompt_version, model_version)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS batches (
      batch_id TEXT PRIMARY KEY,
      submitted_at TEXT,
      status TEXT,
      meta_json TEXT
    )
    """,
]

LATEST_SCORES_VIEW = """
CREATE VIEW IF NOT EXISTS latest_scores AS
WITH newest AS (
  SELECT cik, bucket_id,
         MAX(fy || '|' || prompt_version || '|' || model_version) AS key
  FROM scores
  GROUP BY cik, bucket_id
),
picked AS (
  SELECT s.*
  FROM scores s
  JOIN newest n
    ON s.cik = n.cik
   AND s.bucket_id = n.bucket_id
   AND (s.fy || '|' || s.prompt_version || '|' || s.model_version) = n.key
)
SELECT
  cik,
  bucket_id,
  MAX(ticker) AS ticker,
  MAX(fy) AS fy,
  MAX(prompt_version) AS prompt_version,
  MAX(model_version) AS model_version,
  AVG(score) AS score,
  CASE
    WHEN MIN(CASE confidence WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END) = 3 THEN 'high'
    WHEN MIN(CASE confidence WHEN 'high' THEN 3 WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END) = 2 THEN 'medium'
    ELSE 'low'
  END AS confidence,
  MAX(CASE WHEN run = 1 THEN rationale END) AS rationale_run1,
  MAX(CASE WHEN run = 2 THEN rationale END) AS rationale_run2,
  MAX(pre_revenue) AS pre_revenue
FROM picked
GROUP BY cik, bucket_id
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    for stmt in SCHEMA:
        conn.execute(stmt)
    conn.execute(LATEST_SCORES_VIEW)
    conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def upsert_company(conn: sqlite3.Connection, **row) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    updates = ", ".join(f"{k}=excluded.{k}" for k in row.keys() if k != "cik")
    conn.execute(
        f"INSERT INTO companies ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(cik) DO UPDATE SET {updates}",
        row,
    )


def upsert_filing(conn: sqlite3.Connection, **row) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in row.keys() if k not in ("cik", "accession")
    )
    conn.execute(
        f"INSERT INTO filings ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(cik, accession) DO UPDATE SET {updates}",
        row,
    )


def upsert_shortlist(conn: sqlite3.Connection, **row) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    updates = ", ".join(
        f"{k}=excluded.{k}" for k in row.keys() if k not in ("cik", "bucket_id")
    )
    conn.execute(
        f"INSERT INTO shortlist ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(cik, bucket_id) DO UPDATE SET {updates}",
        row,
    )


def insert_score(conn: sqlite3.Connection, **row) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    conn.execute(
        f"INSERT OR IGNORE INTO scores ({cols}) VALUES ({placeholders})",
        row,
    )


def upsert_batch(conn: sqlite3.Connection, **row) -> None:
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    updates = ", ".join(f"{k}=excluded.{k}" for k in row.keys() if k != "batch_id")
    conn.execute(
        f"INSERT INTO batches ({cols}) VALUES ({placeholders}) "
        f"ON CONFLICT(batch_id) DO UPDATE SET {updates}",
        row,
    )
