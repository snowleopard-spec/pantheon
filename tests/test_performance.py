from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

# Load the module directly from scripts/ — it's not a package.
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import performance  # noqa: E402


def _write_weights(path: Path) -> None:
    df = pd.DataFrame(
        [
            # bucket_a: AAA 60%, BBB 40%
            dict(bucket_id="bucket_a", bucket_name="Bucket A", ticker="AAA",
                 cik="c1", score=0.8, confidence="high", market_cap=100.0,
                 weight_cap_score=0.6, weight_equal=0.5, weight_score=0.6,
                 rationale_run1="r1", rationale_run2="r2",
                 fy=2024, prompt_version="1.0", model_version="claude-haiku-4-5"),
            dict(bucket_id="bucket_a", bucket_name="Bucket A", ticker="BBB",
                 cik="c2", score=0.4, confidence="medium", market_cap=50.0,
                 weight_cap_score=0.4, weight_equal=0.5, weight_score=0.4,
                 rationale_run1="r1", rationale_run2="r2",
                 fy=2024, prompt_version="1.0", model_version="claude-haiku-4-5"),
            # bucket_b: CCC 100%
            dict(bucket_id="bucket_b", bucket_name="Bucket B", ticker="CCC",
                 cik="c3", score=0.7, confidence="high", market_cap=200.0,
                 weight_cap_score=1.0, weight_equal=1.0, weight_score=1.0,
                 rationale_run1="r1", rationale_run2="r2",
                 fy=2024, prompt_version="1.0", model_version="claude-haiku-4-5"),
        ]
    )
    df.to_csv(path, index=False)


def _write_prices(path: Path) -> None:
    """Simple price history: 3 tickers over 3 days.
    AAA: 100 -> 110 -> 121 (+10% per day, cumret at end = 1.21)
    BBB: 100 -> 100 -> 100 (flat, cumret = 1.0)
    CCC: 200 -> 220 -> 242 (+10% per day, cumret = 1.21)
    """
    rows = []
    for ticker, series in [
        ("AAA", [100.0, 110.0, 121.0]),
        ("BBB", [100.0, 100.0, 100.0]),
        ("CCC", [200.0, 220.0, 242.0]),
    ]:
        for d, c in zip(["2025-01-01", "2025-01-02", "2025-01-03"], series):
            rows.append({"date": d, "ticker": ticker, "close": c})
    pd.DataFrame(rows).to_csv(path, index=False)


def test_cli_basic_output(tmp_path: Path):
    weights = tmp_path / "minidex_weights.csv"
    prices = tmp_path / "prices.csv"
    _write_weights(weights)
    _write_prices(prices)

    r = CliRunner().invoke(
        performance.app,
        [
            "--weights", str(weights),
            "--prices", str(prices),
            "--weight-col", "weight_cap_score",
        ],
    )
    assert r.exit_code == 0, r.output
    assert "Cumulative returns" in r.output
    assert "bucket_a" in r.output
    assert "bucket_b" in r.output

    out_csv = weights.parent / "bucket_returns_weight_cap_score.csv"
    assert out_csv.exists()

    ret_df = pd.read_csv(out_csv)
    assert "date" in ret_df.columns
    assert set(ret_df.columns) >= {"date", "bucket_a", "bucket_b"}
    assert len(ret_df) == 3

    # bucket_a final: 0.6 * 1.21 + 0.4 * 1.0 = 1.126
    final = ret_df.iloc[-1]
    assert float(final["bucket_a"]) == pytest.approx(1.126, abs=1e-6)
    # bucket_b final: 1.0 * 1.21 = 1.21
    assert float(final["bucket_b"]) == pytest.approx(1.21, abs=1e-6)


def test_cli_with_benchmark(tmp_path: Path):
    weights = tmp_path / "minidex_weights.csv"
    prices = tmp_path / "prices.csv"
    _write_weights(weights)
    _write_prices(prices)

    # Add SPY prices to the file.
    px = pd.read_csv(prices)
    spy = pd.DataFrame(
        [
            {"date": "2025-01-01", "ticker": "SPY", "close": 500.0},
            {"date": "2025-01-02", "ticker": "SPY", "close": 505.0},
            {"date": "2025-01-03", "ticker": "SPY", "close": 510.0},
        ]
    )
    pd.concat([px, spy], ignore_index=True).to_csv(prices, index=False)

    r = CliRunner().invoke(
        performance.app,
        [
            "--weights", str(weights),
            "--prices", str(prices),
            "--weight-col", "weight_equal",
            "--benchmark", "SPY",
        ],
    )
    assert r.exit_code == 0, r.output

    out_csv = weights.parent / "bucket_returns_weight_equal.csv"
    ret_df = pd.read_csv(out_csv)
    assert "SPY" in ret_df.columns
    # SPY final: 510/500 = 1.02
    assert float(ret_df.iloc[-1]["SPY"]) == pytest.approx(1.02, abs=1e-6)


def test_delisted_ticker_holds_terminal(tmp_path: Path):
    """A member ticker present in weights but with only partial price history
    (delisted mid-window) should hold its terminal price forward."""
    weights = tmp_path / "minidex_weights.csv"
    prices = tmp_path / "prices.csv"
    _write_weights(weights)

    # BBB stops after day 1 (last close = 100 -> held); AAA and CCC continue.
    rows = []
    aaa = [("2025-01-01", 100.0), ("2025-01-02", 110.0), ("2025-01-03", 121.0)]
    bbb = [("2025-01-01", 100.0), ("2025-01-02", 100.0)]  # NO day 3
    ccc = [("2025-01-01", 200.0), ("2025-01-02", 220.0), ("2025-01-03", 242.0)]
    for tkr, series in [("AAA", aaa), ("BBB", bbb), ("CCC", ccc)]:
        for d, c in series:
            rows.append({"date": d, "ticker": tkr, "close": c})
    pd.DataFrame(rows).to_csv(prices, index=False)

    r = CliRunner().invoke(
        performance.app,
        [
            "--weights", str(weights),
            "--prices", str(prices),
            "--weight-col", "weight_cap_score",
        ],
    )
    assert r.exit_code == 0, r.output

    out_csv = weights.parent / "bucket_returns_weight_cap_score.csv"
    ret_df = pd.read_csv(out_csv)
    # bucket_a final: 0.6 * 1.21 + 0.4 * 1.0 = 1.126 (BBB held at terminal 100)
    assert float(ret_df.iloc[-1]["bucket_a"]) == pytest.approx(1.126, abs=1e-6)


def test_missing_ticker_dropped_and_renormalised(tmp_path: Path):
    """A weights entry with no prices at all is dropped; other members
    are renormalised to sum to 1 within the bucket."""
    weights = tmp_path / "minidex_weights.csv"
    prices = tmp_path / "prices.csv"
    _write_weights(weights)

    # BBB missing entirely.
    rows = []
    for tkr, series in [
        ("AAA", [100.0, 110.0, 121.0]),
        ("CCC", [200.0, 220.0, 242.0]),
    ]:
        for d, c in zip(["2025-01-01", "2025-01-02", "2025-01-03"], series):
            rows.append({"date": d, "ticker": tkr, "close": c})
    pd.DataFrame(rows).to_csv(prices, index=False)

    r = CliRunner().invoke(
        performance.app,
        [
            "--weights", str(weights),
            "--prices", str(prices),
            "--weight-col", "weight_cap_score",
        ],
    )
    assert r.exit_code == 0, r.output

    ret_df = pd.read_csv(weights.parent / "bucket_returns_weight_cap_score.csv")
    # With BBB dropped, AAA is renormalised to weight=1 in bucket_a: cumret = 1.21.
    assert float(ret_df.iloc[-1]["bucket_a"]) == pytest.approx(1.21, abs=1e-6)


def test_bad_weight_col_errors(tmp_path: Path):
    weights = tmp_path / "minidex_weights.csv"
    prices = tmp_path / "prices.csv"
    _write_weights(weights)
    _write_prices(prices)

    r = CliRunner().invoke(
        performance.app,
        [
            "--weights", str(weights),
            "--prices", str(prices),
            "--weight-col", "not_a_column",
        ],
    )
    assert r.exit_code != 0
