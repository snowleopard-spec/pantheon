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
        "is_override",
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


def test_anchor_floor_bumps_below_floor_anchor(tmp_path: Path):
    """AAA anchor with tiny cap should get floored to anchor_min_weight."""
    settings = replace(_settings_for(tmp_path), anchor_min_weight=0.10)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    # AAA is the anchor for bucket_a. Tiny cap makes weight_cap_score ~ 0.
    _seed(conn, cik="cik_a", ticker="AAA", bucket_id="bucket_a",
          scores=(0.5, 0.5), market_cap=1.0)
    # Big non-anchor member.
    _seed(conn, cik="cik_z", ticker="ZZZ", bucket_id="bucket_a",
          scores=(0.9, 0.9), market_cap=10_000.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    df_a = df[df["bucket_id"] == "bucket_a"].sort_values("ticker")
    weights = dict(zip(df_a["ticker"], df_a["weight_cap_score"]))
    # AAA should hit the 0.10 floor.
    assert weights["AAA"] == pytest.approx(0.10, abs=1e-9)
    # ZZZ absorbs the remainder.
    assert weights["ZZZ"] == pytest.approx(0.90, abs=1e-9)
    # Bucket still sums to 1.
    assert (df_a["weight_cap_score"].sum()) == pytest.approx(1.0, abs=1e-9)


def test_anchor_floor_includes_below_score_floor_anchor(tmp_path: Path):
    """BBB anchor scoring 0.00 (below 0.10 score floor) still lands in bucket_b."""
    settings = replace(_settings_for(tmp_path), anchor_min_weight=0.10)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed(conn, cik="cik_b", ticker="BBB", bucket_id="bucket_b",
          scores=(0.0, 0.0), market_cap=100.0)
    _seed(conn, cik="cik_x", ticker="XXX", bucket_id="bucket_b",
          scores=(0.8, 0.8), market_cap=1000.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    df_b = df[df["bucket_id"] == "bucket_b"]
    assert set(df_b["ticker"]) == {"BBB", "XXX"}
    bbb = df_b[df_b["ticker"] == "BBB"].iloc[0]
    assert bbb["weight_cap_score"] == pytest.approx(0.10, abs=1e-9)


def test_anchor_floor_all_anchors_below_floor_shrinks_above_floor_anchors_only(capsys):
    """Below-floor anchors hit exactly the floor; above-floor anchors shrink
    proportionally to absorb the shortfall.

    Mirrors the pilot's `power_generation` bucket: 5 anchors, 3 above-floor
    and 2 below-floor (pre-floor weight 0.0). No non-anchors in the bucket.
    """
    # Pre-floor weights sum to 1.0.
    tickers = ["CEG", "TLN", "VST", "OKLO", "SMR"]
    pre = pd.Series(
        [0.4430, 0.3033, 0.2537, 0.0, 0.0],
        index=tickers,
        name="weight_cap_score",
    )
    is_anchor = pd.Series([True] * 5, index=tickers)
    min_weight = 0.05

    out = indices._apply_anchor_floor(pre, is_anchor, min_weight)

    # Below-floor anchors land at EXACTLY the floor.
    assert out["OKLO"] == pytest.approx(min_weight, abs=1e-12)
    assert out["SMR"] == pytest.approx(min_weight, abs=1e-12)

    # Above-floor anchors shrink proportionally into the remaining 0.9 pool.
    above = ["CEG", "TLN", "VST"]
    above_pre_sum = pre[above].sum()
    remaining_pool = 1.0 - 2 * min_weight
    scale = remaining_pool / above_pre_sum
    for t in above:
        assert out[t] == pytest.approx(pre[t] * scale, abs=1e-12)

    # Overall bucket still sums to 1.0.
    assert out.sum() == pytest.approx(1.0, abs=1e-9)

    # No warning printed on the well-defined case.
    assert "WARNING" not in capsys.readouterr().out


def test_anchor_floor_overconstrained_bucket(capsys):
    """3 anchors, each with a 40% floor => reserved 120% > 100%.

    Result: equal 1/3 split across the fixed anchors; a WARNING is printed.
    """
    tickers = ["A", "B", "C"]
    # All three anchors sit below the 0.40 floor pre-flooring.
    pre = pd.Series([0.35, 0.30, 0.35], index=tickers, name="weight_cap_score")
    is_anchor = pd.Series([True, True, True], index=tickers)
    min_weight = 0.40  # 3 * 0.40 = 1.20 > 1.0

    out = indices._apply_anchor_floor(pre, is_anchor, min_weight)

    for t in tickers:
        assert out[t] == pytest.approx(1.0 / 3.0, abs=1e-12)
    assert out.sum() == pytest.approx(1.0, abs=1e-9)

    captured = capsys.readouterr().out
    assert "WARNING" in captured


def test_anchor_floor_zero_is_backwards_compat(tmp_path: Path):
    """anchor_min_weight=0.0 gives identical results to the old behavior."""
    settings = replace(_settings_for(tmp_path), anchor_min_weight=0.0)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    # bucket_a: AAA cap*score = 100*0.8 = 80; CCC = 50*0.4 = 20; totals 100.
    df_a = df[df["bucket_id"] == "bucket_a"].sort_values("ticker")
    weights = dict(zip(df_a["ticker"], df_a["weight_cap_score"]))
    assert weights["AAA"] == pytest.approx(0.80, abs=1e-9)
    assert weights["CCC"] == pytest.approx(0.20, abs=1e-9)


def test_manifest_records_anchor_min_weight(tmp_path: Path):
    settings = replace(_settings_for(tmp_path), anchor_min_weight=0.07)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed(conn, cik="cik_a", ticker="AAA", bucket_id="bucket_a",
          scores=(0.5, 0.5), market_cap=100.0)
    _seed(conn, cik="cik_b", ticker="BBB", bucket_id="bucket_b",
          scores=(0.5, 0.5), market_cap=100.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    manifest = json.loads((settings.outputs_dir / "2025-01-15" / "manifest.json").read_text())
    assert manifest["anchor_min_weight"] == 0.07


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


# ---------------------------------------------------------------- overrides


DEFS_YAML_OVERRIDE = DEFS_YAML + """
overrides:
  - ticker: DDD
    bucket: bucket_a
    action: include
    reason: >
      real but sub-floor exposure; bucket is incomplete without it
"""

DEFS_YAML_EXCLUDE = DEFS_YAML + """
overrides:
  - ticker: CCC
    bucket: bucket_a
    action: exclude
    reason: >
      clears the floor but the definition is too loose to have caught it
"""


def _settings_with_overrides(tmp_path: Path, defs_yaml: str = DEFS_YAML_OVERRIDE):
    settings = _settings_for(tmp_path)
    settings.definitions_path.write_text(defs_yaml, encoding="utf-8")
    return settings


def test_override_admits_below_floor_member(tmp_path: Path):
    """An overridden pair below the score floor still lands in the bucket."""
    settings = _settings_with_overrides(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    # DDD scores 0.04, well under the 0.10 floor for this fixture.
    _seed(conn, cik="cik_d", ticker="DDD", bucket_id="bucket_a",
          scores=(0.04, 0.04), market_cap=10.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")

    bucket_a = df[df["bucket_id"] == "bucket_a"]
    assert "DDD" in set(bucket_a["ticker"])
    row = bucket_a[bucket_a["ticker"] == "DDD"].iloc[0]
    assert bool(row["is_override"]) is True
    # Enters at its actual score, not a promoted one.
    assert row["score"] == pytest.approx(0.04)


def test_override_gets_no_weight_floor(tmp_path: Path):
    """Unlike an anchor, an override is weighted proportionally to its score."""
    settings = replace(_settings_with_overrides(tmp_path), anchor_min_weight=0.25)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    _seed(conn, cik="cik_d", ticker="DDD", bucket_id="bucket_a",
          scores=(0.04, 0.04), market_cap=10.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    row = df[(df["bucket_id"] == "bucket_a") & (df["ticker"] == "DDD")].iloc[0]

    # AAA is the anchor and does get floored to 0.25; DDD must not.
    assert row["weight_cap_score"] < 0.25
    # score-normalised: 0.04 / (0.8 + 0.4 + 0.04)
    assert row["weight_score"] == pytest.approx(0.04 / 1.24)


def test_override_does_not_leak_into_other_buckets(tmp_path: Path):
    """The override is scoped to (ticker, bucket), not to the ticker."""
    settings = _settings_with_overrides(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    _seed(conn, cik="cik_d", ticker="DDD", bucket_id="bucket_a",
          scores=(0.04, 0.04), market_cap=10.0)
    _seed(conn, cik="cik_d2", ticker="DDD", bucket_id="bucket_b",
          scores=(0.04, 0.04), market_cap=10.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")

    assert "DDD" in set(df[df["bucket_id"] == "bucket_a"]["ticker"])
    assert "DDD" not in set(df[df["bucket_id"] == "bucket_b"]["ticker"])


def test_manifest_records_applied_override(tmp_path: Path):
    settings = _settings_with_overrides(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    _seed(conn, cik="cik_d", ticker="DDD", bucket_id="bucket_a",
          scores=(0.04, 0.04), market_cap=10.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )
    applied = m["overrides_applied"]
    assert len(applied) == 1
    assert applied[0]["ticker"] == "DDD"
    assert applied[0]["bucket"] == "bucket_a"
    assert applied[0]["action"] == "include"
    assert "incomplete" in applied[0]["reason"]


def test_redundant_override_is_flagged_and_not_recorded(tmp_path: Path, capsys):
    """An override whose score now clears the floor is reported as removable."""
    settings = _settings_with_overrides(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    # 0.5 is comfortably above the 0.10 fixture floor.
    _seed(conn, cik="cik_d", ticker="DDD", bucket_id="bucket_a",
          scores=(0.5, 0.5), market_cap=10.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    out = capsys.readouterr().out
    assert "redundant" in out

    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )
    assert m["overrides_applied"] == []


def test_unmatched_override_warns(tmp_path: Path, capsys):
    """An override naming a pair that was never scored is surfaced, not silent."""
    settings = _settings_with_overrides(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)  # no DDD at all
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    out = capsys.readouterr().out
    assert "matched no scored pair" in out


def test_override_naming_unknown_bucket_warns(tmp_path: Path, capsys):
    bad = DEFS_YAML + """
overrides:
  - ticker: AAA
    bucket: bucket_does_not_exist
    reason: typo
"""
    settings = _settings_with_overrides(tmp_path, defs_yaml=bad)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    out = capsys.readouterr().out
    assert "does not exist in the definitions" in out


def test_no_overrides_block_is_backwards_compat(tmp_path: Path):
    """A definitions file with no `overrides:` key behaves exactly as before."""
    settings = _settings_for(tmp_path)  # DEFS_YAML, no overrides block
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    assert len(df) == 4
    assert not df["is_override"].any()
    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )
    assert m["overrides_applied"] == []


def test_exclude_override_drops_qualifying_member(tmp_path: Path):
    """A pair that clears the floor is removed when excluded."""
    settings = _settings_with_overrides(tmp_path, defs_yaml=DEFS_YAML_EXCLUDE)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)  # CCC scores 0.4 in bucket_a, well over the floor
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")

    bucket_a = df[df["bucket_id"] == "bucket_a"]
    assert "CCC" not in set(bucket_a["ticker"])
    # AAA is now alone in bucket_a and absorbs the whole weight.
    assert bucket_a["weight_score"].sum() == pytest.approx(1.0)
    # The exclusion is scoped to the pair: CCC survives in bucket_b.
    assert "CCC" in set(df[df["bucket_id"] == "bucket_b"]["ticker"])


def test_exclude_override_beats_anchor_status(tmp_path: Path, capsys):
    """Excluding a declared anchor works, but warns about the contradiction."""
    defs = DEFS_YAML + """
overrides:
  - ticker: AAA
    bucket: bucket_a
    action: exclude
    reason: contradicts the anchor declaration on purpose
"""
    settings = _settings_with_overrides(tmp_path, defs_yaml=defs)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    out = capsys.readouterr().out
    assert "contradict" in out

    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    assert "AAA" not in set(df[df["bucket_id"] == "bucket_a"]["ticker"])


def test_manifest_records_exclusion(tmp_path: Path):
    settings = _settings_with_overrides(tmp_path, defs_yaml=DEFS_YAML_EXCLUDE)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )
    applied = m["overrides_applied"]
    assert len(applied) == 1
    assert applied[0]["ticker"] == "CCC"
    assert applied[0]["action"] == "exclude"
    assert applied[0]["score"] == "0.400"


def test_redundant_exclusion_is_flagged(tmp_path: Path, capsys):
    """Excluding a pair already below the floor is reported as removable."""
    defs = DEFS_YAML + """
overrides:
  - ticker: DDD
    bucket: bucket_a
    action: exclude
    reason: already below the floor
"""
    settings = _settings_with_overrides(tmp_path, defs_yaml=defs)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    _seed(conn, cik="cik_d", ticker="DDD", bucket_id="bucket_a",
          scores=(0.04, 0.04), market_cap=10.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    out = capsys.readouterr().out
    assert "would be dropped anyway" in out

    m = json.loads(
        (settings.outputs_dir / "2025-01-15" / "manifest.json").read_text()
    )
    assert m["overrides_applied"] == []


def test_unknown_override_action_is_skipped(tmp_path: Path, capsys):
    defs = DEFS_YAML + """
overrides:
  - ticker: CCC
    bucket: bucket_a
    action: banish
    reason: typo in the action field
"""
    settings = _settings_with_overrides(tmp_path, defs_yaml=defs)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    out = capsys.readouterr().out
    assert "unknown action" in out

    # Skipped entirely: CCC keeps its earned membership.
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    assert "CCC" in set(df[df["bucket_id"] == "bucket_a"]["ticker"])


def test_action_defaults_to_include(tmp_path: Path):
    """Omitting `action` keeps the original include-only behaviour."""
    defs = DEFS_YAML + """
overrides:
  - ticker: DDD
    bucket: bucket_a
    reason: no action field at all
"""
    settings = _settings_with_overrides(tmp_path, defs_yaml=defs)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_two_buckets(conn)
    _seed(conn, cik="cik_d", ticker="DDD", bucket_id="bucket_a",
          scores=(0.04, 0.04), market_cap=10.0)
    conn.commit()
    conn.close()

    indices.build("2025-01-15", settings=settings)
    df = pd.read_csv(settings.outputs_dir / "2025-01-15" / "minidex_weights.csv")
    row = df[(df["bucket_id"] == "bucket_a") & (df["ticker"] == "DDD")].iloc[0]
    assert bool(row["is_override"]) is True
