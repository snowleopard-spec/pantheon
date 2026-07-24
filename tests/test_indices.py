from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pandas as pd
import pytest

from minidex import config, db, indices


DEFS_YAML = """
buckets:
  - id: bucket_a
    name: Bucket Alpha
    definition: definition-a
    includes: [a1]
    anchors: [AAA]
  - id: bucket_b
    name: Bucket Beta
    definition: definition-b
    includes: [b1]
    anchors: [BBB]
"""


def _settings_for(tmp_path: Path):
    s = config.get_settings()
    defs_path = tmp_path / "defs.yaml"
    defs_path.write_text(DEFS_YAML, encoding="utf-8")
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("# fake prompt v1.0\n", encoding="utf-8")
    outputs_dir = tmp_path / "outputs"
    outputs_dir.mkdir(exist_ok=True)
    return replace(
        s,
        db_path=tmp_path / "minidex.db",
        outputs_dir=outputs_dir,
        definitions_path=defs_path,
        prompt_path=prompt_path,
        score_floor=0.10,
    )


def _seed(
    conn,
    *,
    cik: str,
    ticker: str,
    bucket_id: str,
    scores: tuple[float, float],
    market_cap: float | None,
    fy: int = 2024,
    prompt_version: str = "1.0",
    model_version: str = "claude-haiku-4-5",
):
    """Insert a company + a 2-run scoring pair for one (cik, bucket)."""
    db.upsert_company(
        conn,
        cik=cik,
        ticker=ticker,
        name=ticker,
        sic="7372",
        is_candidate=1,
        market_cap=market_cap,
        market_cap_asof="2025-01-01",
    )
    db.upsert_filing(
        conn,
        cik=cik,
        accession=f"acc-{cik}-{bucket_id}",
        fy=fy,
        filed_date="2025-01-15",
        item1_path=f"raw/{cik}.txt",
        item1_chars=5000,
        segments_json="[]",
    )
    for run_idx, s in enumerate(scores, start=1):
        db.insert_score(
            conn,
            cik=cik,
            ticker=ticker,
            bucket_id=bucket_id,
            fy=fy,
            run=run_idx,
            score=s,
            confidence="high" if s > 0.5 else "medium",
            rationale=f"rationale-run{run_idx}",
            evidence_type="segment_data",
            pre_revenue=0,
            prompt_version=prompt_version,
            model_version=model_version,
            created_at="2025-01-16T00:00:00Z",
        )


def _seed_two_buckets(conn):
    # bucket_a: AAA (0.8), CCC (0.4). Cap AAA=100, CCC=50.
    _seed(conn, cik="cik_a", ticker="AAA", bucket_id="bucket_a",
          scores=(0.7, 0.9), market_cap=100.0)
    _seed(conn, cik="cik_c", ticker="CCC", bucket_id="bucket_a",
          scores=(0.4, 0.4), market_cap=50.0)
    # bucket_b: BBB (0.6), CCC (0.2). Caps 200 and 50.
    _seed(conn, cik="cik_b", ticker="BBB", bucket_id="bucket_b",
          scores=(0.5, 0.7), market_cap=200.0)
    _seed(conn, cik="cik_c", ticker="CCC", bucket_id="bucket_b",
          scores=(0.2, 0.2), market_cap=50.0)


def test_build_writes_csv_parquet_manifest(tmp_path: Path):
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    result = indices.build("2025-01-15", settings=settings)
    out_dir = settings.outputs_dir / "2025-01-15"

    assert (out_dir / "minidex_weights.csv").exists()
    assert (out_dir / "minidex_weights.parquet").exists()
    assert (out_dir / "manifest.json").exists()

    csv_df = pd.read_csv(out_dir / "minidex_weights.csv")
    parquet_df = pd.read_parquet(out_dir / "minidex_weights.parquet")

    # Column order preserved exactly.
    expected_cols = [
        "bucket_id", "bucket_name", "ticker", "cik", "score", "confidence",
        "market_cap", "weight_cap_score", "weight_equal", "weight_score",
        "rationale_run1", "rationale_run2", "fy", "prompt_version", "model_version",
    ]
    assert list(csv_df.columns) == expected_cols
    assert list(parquet_df.columns) == expected_cols

    # 4 rows across 2 buckets.
    assert len(csv_df) == 4
    assert result["n_rows"] == 4
    assert result["n_buckets"] == 2

    # Bucket names populated from YAML.
    names = set(zip(csv_df["bucket_id"], csv_df["bucket_name"]))
    assert ("bucket_a", "Bucket Alpha") in names
    assert ("bucket_b", "Bucket Beta") in names


def test_build_weights_sum_to_one_per_bucket(tmp_path: Path):
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")

    for wcol in ("weight_cap_score", "weight_equal", "weight_score"):
        sums = df.groupby("bucket_id")[wcol].sum()
        for bucket_id, total in sums.items():
            assert abs(total - 1.0) < 1e-6, f"{wcol} in {bucket_id} = {total}"


def test_build_weights_specific_values(tmp_path: Path):
    """Verify actual weight math on a known-input bucket."""
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")

    a = df[df["bucket_id"] == "bucket_a"].set_index("ticker")
    # Mean scores: AAA=0.8, CCC=0.4.
    assert a.loc["AAA", "score"] == pytest.approx(0.8)
    assert a.loc["CCC", "score"] == pytest.approx(0.4)
    # weight_equal: 0.5 each.
    assert a["weight_equal"].tolist() == [0.5, 0.5]
    # weight_score: 0.8/1.2, 0.4/1.2.
    assert a.loc["AAA", "weight_score"] == pytest.approx(0.8 / 1.2)
    assert a.loc["CCC", "weight_score"] == pytest.approx(0.4 / 1.2)
    # weight_cap_score: 0.8*100 / (0.8*100 + 0.4*50) = 80/100
    assert a.loc["AAA", "weight_cap_score"] == pytest.approx(80.0 / 100.0)
    assert a.loc["CCC", "weight_cap_score"] == pytest.approx(20.0 / 100.0)


def test_manifest_fields(tmp_path: Path):
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )

    assert m["asof"] == "2025-01-15"
    # 3 distinct ciks scored, 4 pairs total.
    assert m["n_companies_scored"] == 3
    assert m["n_pairs_scored"] == 4
    assert m["prompt_version"] == "1.0"
    assert m["model_version"] == "claude-haiku-4-5"
    assert m["embedding_model"] == settings.embedding_model
    assert m["similarity_threshold"] == pytest.approx(settings.similarity_threshold)
    assert m["score_floor"] == pytest.approx(0.10)
    # sha256 hex is 64 chars
    assert len(m["definitions_sha256"]) == 64
    assert len(m["prompt_sha256"]) == 64
    # filing_fy_histogram: seeded filings across 3 ciks all with fy=2024. Note
    # that we insert one filing per (cik, bucket) so cik_c has 2 filings.
    assert m["filing_fy_histogram"] == {"2024": 4}
    # run_date is a valid ISO date.
    assert len(m["run_date"]) == 10 and m["run_date"][4] == "-"


def test_score_floor_excludes_low(tmp_path: Path):
    """Rows with mean score < floor are excluded."""
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    # bucket_a: AAA(0.8) qualifies; ZZZ mean=0.05 excluded.
    _seed(conn, cik="cik_a", ticker="AAA", bucket_id="bucket_a",
          scores=(0.7, 0.9), market_cap=100.0)
    _seed(conn, cik="cik_z", ticker="ZZZ", bucket_id="bucket_a",
          scores=(0.05, 0.05), market_cap=10.0)
    # bucket_b: need >=1 row for a bucket to appear.
    _seed(conn, cik="cik_b", ticker="BBB", bucket_id="bucket_b",
          scores=(0.5, 0.5), market_cap=100.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    tickers = set(df["ticker"].tolist())
    assert "AAA" in tickers
    assert "BBB" in tickers
    assert "ZZZ" not in tickers


def test_all_missing_market_caps_falls_back_to_equal(tmp_path: Path, capsys):
    """If every member of a bucket has NULL market_cap, weight_cap_score
    should equal weight_equal and a warning must be printed."""
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    # Both members of bucket_a lack market_cap.
    _seed(conn, cik="cik_a", ticker="AAA", bucket_id="bucket_a",
          scores=(0.7, 0.9), market_cap=None)
    _seed(conn, cik="cik_c", ticker="CCC", bucket_id="bucket_a",
          scores=(0.4, 0.4), market_cap=None)
    _seed(conn, cik="cik_b", ticker="BBB", bucket_id="bucket_b",
          scores=(0.6, 0.6), market_cap=200.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    out = capsys.readouterr().out
    assert "WARNING" in out
    assert "bucket_a" in out

    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    a = df[df["bucket_id"] == "bucket_a"]
    # Both weight_cap_score values should be equal (= weight_equal = 0.5).
    assert a["weight_cap_score"].tolist() == [0.5, 0.5]
    assert a["weight_equal"].tolist() == [0.5, 0.5]


def test_partial_missing_market_cap_still_normalises(tmp_path: Path):
    """One member missing market_cap gets weight 0; the others still sum to 1."""
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed(conn, cik="cik_a", ticker="AAA", bucket_id="bucket_a",
          scores=(0.7, 0.9), market_cap=100.0)
    _seed(conn, cik="cik_c", ticker="CCC", bucket_id="bucket_a",
          scores=(0.4, 0.4), market_cap=None)
    _seed(conn, cik="cik_b", ticker="BBB", bucket_id="bucket_b",
          scores=(0.6, 0.6), market_cap=200.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")

    a = df[df["bucket_id"] == "bucket_a"].set_index("ticker")
    assert a.loc["CCC", "weight_cap_score"] == pytest.approx(0.0)
    assert a.loc["AAA", "weight_cap_score"] == pytest.approx(1.0)
    # Sum still 1 per bucket.
    for wcol in ("weight_cap_score", "weight_equal", "weight_score"):
        assert df.groupby("bucket_id")[wcol].sum().sub(1.0).abs().max() < 1e-6


def test_empty_scores_writes_empty_outputs(tmp_path: Path):
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    conn.commit()
    conn.close()

    result = indices.build("2025-01-15", settings=settings)
    assert result["n_rows"] == 0
    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )
    assert m["n_pairs_scored"] == 0
    assert m["n_companies_scored"] == 0
    assert m["filing_fy_histogram"] == {}


def test_manifest_prompt_version_mixed(tmp_path: Path):
    """Two different prompt_versions -> manifest['prompt_version'] == 'mixed'."""
    settings = _settings_for(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed(conn, cik="cik_a", ticker="AAA", bucket_id="bucket_a",
          scores=(0.7, 0.9), market_cap=100.0, prompt_version="1.0")
    _seed(conn, cik="cik_b", ticker="BBB", bucket_id="bucket_b",
          scores=(0.6, 0.6), market_cap=200.0, prompt_version="1.1")
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )
    assert m["prompt_version"] == "mixed"
