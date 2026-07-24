from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from minidex import config, db, qc


# ---------------------------------------------------------------------------
# Fixture: tmp settings + seeded DB exercising all 5 report sections
# ---------------------------------------------------------------------------

MINI_DEFS = """
buckets:
  - id: bucket_alpha
    name: Alpha bucket
    definition: alpha stuff
    includes: [a1]
    anchors: [GOOD, BAD]
  - id: bucket_beta
    name: Beta bucket
    definition: beta stuff
    includes: [b1]
    anchors: [MISSING]
  - id: bucket_gamma
    name: Gamma bucket
    definition: gamma stuff
    includes: [g1]
    anchors: []
"""


def _make_settings(tmp_path: Path):
    """Build a Settings object pointing at tmp_path for db + outputs + defs."""
    s = config.get_settings()
    defs = tmp_path / "defs.yaml"
    defs.write_text(MINI_DEFS, encoding="utf-8")
    outputs = tmp_path / "outputs"
    outputs.mkdir(parents=True, exist_ok=True)
    return replace(
        s,
        db_path=tmp_path / "minidex.db",
        definitions_path=defs,
        outputs_dir=outputs,
    )


def _insert_pair(conn, *, cik, ticker, bucket_id, run1_score, run2_score,
                 run1_conf="high", run2_conf="high",
                 prompt_version="1.0", model_version="claude-haiku-4-5",
                 fy=2024):
    for run, score, conf in (
        (1, run1_score, run1_conf),
        (2, run2_score, run2_conf),
    ):
        db.insert_score(
            conn,
            cik=cik, ticker=ticker, bucket_id=bucket_id, fy=fy, run=run,
            score=score, confidence=conf,
            rationale=f"{ticker} r{run}",
            evidence_type="segment_data", pre_revenue=0,
            prompt_version=prompt_version, model_version=model_version,
            created_at="2025-01-16T00:00:00Z",
        )


@pytest.fixture()
def seeded(tmp_path: Path):
    settings = _make_settings(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)

    # (1) Anchor PASS: GOOD on bucket_alpha, mean 0.8
    _insert_pair(conn, cik="0000010", ticker="GOOD",
                 bucket_id="bucket_alpha", run1_score=0.8, run2_score=0.8)

    # (1) Anchor FAIL: BAD on bucket_alpha, mean 0.3
    _insert_pair(conn, cik="0000011", ticker="BAD",
                 bucket_id="bucket_alpha", run1_score=0.3, run2_score=0.3)

    # (1) Anchor SKIP: MISSING has no scored rows at all

    # (2) Run disagreement: DISA on bucket_gamma, run1=0.2, run2=0.7 => delta=0.5
    _insert_pair(conn, cik="0000012", ticker="DISA",
                 bucket_id="bucket_gamma", run1_score=0.2, run2_score=0.7)

    # (3) Borderline: BORD on bucket_gamma, mean 0.15 (in [0.10, 0.30])
    _insert_pair(conn, cik="0000013", ticker="BORD",
                 bucket_id="bucket_gamma", run1_score=0.15, run2_score=0.15)

    # (4) Low-confidence high: LCH on bucket_beta, mean 0.55, confidence low
    _insert_pair(conn, cik="0000014", ticker="LCH",
                 bucket_id="bucket_beta",
                 run1_score=0.55, run2_score=0.55,
                 run1_conf="low", run2_conf="low")

    # (5) Filler: FIL on bucket_alpha, mean 0.5 — a bucket_alpha member above floor
    _insert_pair(conn, cik="0000015", ticker="FIL",
                 bucket_id="bucket_alpha", run1_score=0.5, run2_score=0.5)

    # A below-floor row that should NOT count toward bucket_gamma member count.
    _insert_pair(conn, cik="0000016", ticker="TINY",
                 bucket_id="bucket_gamma", run1_score=0.05, run2_score=0.05)

    conn.commit()
    conn.close()
    return settings


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_writes_qc_report_to_outputs_dir(seeded):
    out = qc.run(settings=seeded)
    assert out == seeded.outputs_dir / "qc_report.md"
    assert out.exists()


def test_report_contains_anchor_fail_line(seeded):
    qc.run(settings=seeded)
    text = (seeded.outputs_dir / "qc_report.md").read_text(encoding="utf-8")
    # BAD should be flagged as FAIL under bucket_alpha
    assert "FAIL BAD" in text
    assert "bucket_alpha" in text
    # GOOD should be PASS
    assert "PASS GOOD" in text
    # MISSING is an anchor with no rows at all — SKIP list
    assert "SKIP MISSING" in text


def test_report_contains_run_disagreement(seeded):
    qc.run(settings=seeded)
    text = (seeded.outputs_dir / "qc_report.md").read_text(encoding="utf-8")
    # DISA (run1=0.2, run2=0.7, delta=0.5) should be in Run disagreement section
    assert "## 2. Run disagreement" in text
    assert "DISA" in text
    assert "0.500" in text  # delta formatting


def test_report_contains_borderline(seeded):
    qc.run(settings=seeded)
    text = (seeded.outputs_dir / "qc_report.md").read_text(encoding="utf-8")
    assert "## 3. Borderline" in text
    assert "BORD" in text
    assert "0.150" in text


def test_report_contains_low_conf_high(seeded):
    qc.run(settings=seeded)
    text = (seeded.outputs_dir / "qc_report.md").read_text(encoding="utf-8")
    assert "## 4. Low-confidence highs" in text
    assert "LCH" in text
    # DISA has run1=0.2, run2=0.7, mean=0.45 with high conf, should NOT be here
    lch_section = text.split("## 4. Low-confidence highs", 1)[1].split("## 5.", 1)[0]
    assert "DISA" not in lch_section


def test_bucket_counts_use_floor(seeded):
    qc.run(settings=seeded)
    text = (seeded.outputs_dir / "qc_report.md").read_text(encoding="utf-8")
    section = text.split("## 5. Bucket member counts", 1)[1]

    # bucket_alpha: GOOD(0.8), BAD(0.3), FIL(0.5) all >= 0.10 => 3
    # bucket_beta: LCH(0.55) => 1
    # bucket_gamma: DISA(mean 0.45), BORD(0.15) >= 0.10; TINY(0.05) excluded => 2
    assert "| bucket_alpha | Alpha bucket | 3 |" in section
    assert "| bucket_beta | Beta bucket | 1 |" in section
    assert "| bucket_gamma | Gamma bucket | 2 |" in section


def test_run_disagreement_query_helper(seeded):
    conn = db.connect(seeded.db_path)
    try:
        rows = qc.run_disagreements(conn)
    finally:
        conn.close()
    # Only DISA (delta 0.5) exceeds the 0.2 threshold; others are equal-valued pairs.
    tickers = [r["ticker"] for r in rows]
    assert tickers == ["DISA"]
    assert rows[0]["delta"] == pytest.approx(0.5, abs=1e-9)


def test_anchor_scores_returns_expected(seeded):
    import yaml as _yaml
    buckets = _yaml.safe_load(seeded.definitions_path.read_text())["buckets"]
    conn = db.connect(seeded.db_path)
    try:
        results = qc.anchor_scores(conn, buckets)
    finally:
        conn.close()
    by_ticker = {r.ticker: r for r in results}
    assert by_ticker["GOOD"].mean_score == pytest.approx(0.8)
    assert by_ticker["BAD"].mean_score == pytest.approx(0.3)
    assert by_ticker["MISSING"].mean_score is None


def test_empty_db_does_not_crash(tmp_path: Path):
    settings = _make_settings(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    conn.close()

    out = qc.run(settings=settings)
    text = out.read_text(encoding="utf-8")
    # With no scores at all, every anchor is a SKIP; no borderline/disagreement rows.
    assert "SKIP GOOD" in text
    assert "SKIP BAD" in text
    assert "SKIP MISSING" in text
    # bucket_alpha count 0
    assert "| bucket_alpha | Alpha bucket | 0 |" in text
