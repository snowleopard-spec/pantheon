from __future__ import annotations

from pathlib import Path

import pytest

from minidex import db


@pytest.fixture()
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "test.db")
    db.init_schema(c)
    yield c
    c.close()


def test_schema_creates_all_tables(conn):
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert {"companies", "filings", "shortlist", "scores", "batches"} <= tables


def test_latest_scores_view_exists(conn):
    views = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='view'"
        ).fetchall()
    }
    assert "latest_scores" in views


def test_init_schema_is_idempotent(tmp_path: Path):
    p = tmp_path / "x.db"
    c1 = db.connect(p)
    db.init_schema(c1)
    db.init_schema(c1)
    c1.close()


def test_upserts_and_latest_scores_view(conn):
    db.upsert_company(
        conn, cik="0000001", ticker="AAA", name="Alpha", sic="7372", is_candidate=1,
        market_cap=1_000_000_000.0, market_cap_asof="2025-01-01",
    )
    db.upsert_filing(
        conn, cik="0000001", accession="acc1", fy=2024, filed_date="2025-01-15",
        item1_path="raw/acc1.txt", item1_chars=10_000, segments_json="[]",
    )
    db.upsert_shortlist(conn, cik="0000001", bucket_id="b1", similarity=0.72, source="embed")

    for run in (1, 2):
        db.insert_score(
            conn, cik="0000001", ticker="AAA", bucket_id="b1", fy=2024, run=run,
            score=0.6 if run == 1 else 0.8, confidence="high" if run == 1 else "medium",
            rationale=f"run{run} rationale", evidence_type="segment_data", pre_revenue=0,
            prompt_version="1.0", model_version="claude-haiku-4-5",
            created_at="2025-01-16T00:00:00Z",
        )
    conn.commit()

    row = conn.execute(
        "SELECT ticker, score, confidence, rationale_run1, rationale_run2 "
        "FROM latest_scores WHERE cik='0000001' AND bucket_id='b1'"
    ).fetchone()
    assert row is not None
    assert row["ticker"] == "AAA"
    assert abs(row["score"] - 0.7) < 1e-9
    assert row["confidence"] == "medium"
    assert row["rationale_run1"] == "run1 rationale"
    assert row["rationale_run2"] == "run2 rationale"


def test_scores_pk_prevents_duplicate_run(conn):
    kwargs = dict(
        cik="0000002", ticker="BBB", bucket_id="b1", fy=2024, run=1,
        score=0.5, confidence="high", rationale="r", evidence_type="segment_data",
        pre_revenue=0, prompt_version="1.0", model_version="claude-haiku-4-5",
        created_at="2025-01-16T00:00:00Z",
    )
    db.insert_score(conn, **kwargs)
    db.insert_score(conn, **kwargs)  # INSERT OR IGNORE
    conn.commit()
    n = conn.execute("SELECT COUNT(*) FROM scores WHERE cik='0000002'").fetchone()[0]
    assert n == 1
