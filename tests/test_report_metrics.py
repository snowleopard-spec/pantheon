"""Unit tests for scripts/report_metrics.py — the pure-compute metrics
module behind the v2.1 report (Sharpe, daily bucket series, capped
weights, median constituent returns, and the config.json `report` block).
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# Load the module directly from scripts/ — it's not a package.
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import report_metrics as rm  # noqa: E402


# ---------------------------------------------------------------------------
# sharpe
# ---------------------------------------------------------------------------

def test_sharpe_hand_computed():
    # 10 obs: five at +1%, five at +3%. mean = 0.02; sample std (ddof=1)
    # = sqrt(10 * 0.01^2 / 9) = 0.01 * sqrt(10/9). Sharpe = mean/std * sqrt(252)
    # = 2 * sqrt(0.9) * sqrt(252).
    series = pd.Series([0.01] * 5 + [0.03] * 5)
    expected = 2 * math.sqrt(0.9) * math.sqrt(252)
    assert rm.sharpe(series) == pytest.approx(expected, rel=1e-12)


def test_sharpe_annualisation_factor():
    rng = np.random.default_rng(7)
    series = pd.Series(rng.normal(0.001, 0.01, size=60))
    raw = series.mean() / series.std(ddof=1)  # pandas std is ddof=1
    assert rm.sharpe(series) == pytest.approx(raw * math.sqrt(252), rel=1e-12)


def test_sharpe_fewer_than_10_obs_is_none():
    assert rm.sharpe(pd.Series([0.01, -0.02, 0.005] * 3)) is None  # 9 obs
    # NaNs don't count as observations.
    padded = pd.Series([0.01] * 9 + [np.nan] * 5)
    assert rm.sharpe(padded) is None


def test_sharpe_zero_std_is_none():
    assert rm.sharpe(pd.Series([0.01] * 20)) is None


def test_sharpe_accepts_plain_sequence():
    assert rm.sharpe([0.01] * 5 + [0.03] * 5) == pytest.approx(
        2 * math.sqrt(0.9) * math.sqrt(252)
    )


# ---------------------------------------------------------------------------
# daily_returns
# ---------------------------------------------------------------------------

def _prices_frame(series_by_ticker: dict[str, list[float]], dates: list[str]) -> pd.DataFrame:
    rows = []
    for ticker, closes in series_by_ticker.items():
        for d, c in zip(dates, closes):
            if c is not None:
                rows.append({"date": d, "ticker": ticker, "close": c})
    return pd.DataFrame(rows)


def test_daily_returns_basic_and_shape():
    # Latest bar 2026-08-05, 30-day window → target 2026-07-06; the
    # 2026-07-01 bar (last at/before target) anchors the window.
    dates = ["2026-07-01", "2026-08-04", "2026-08-05"]
    prices = _prices_frame({"AAA": [100.0, 110.0, 121.0]}, dates)
    out = rm.daily_returns(prices, ["AAA"], window_days=30)
    assert list(out.columns) == ["AAA"]
    assert len(out) == 2  # the anchor bar itself yields no return
    assert out["AAA"].tolist() == pytest.approx([0.10, 0.10])


def test_daily_returns_penny_filter_excludes_whole_window():
    dates = ["2026-07-01", "2026-08-04", "2026-08-05"]
    prices = _prices_frame(
        {"OK": [10.0, 11.0, 12.0], "PNY": [0.50, 5.0, 50.0]}, dates
    )
    out = rm.daily_returns(prices, ["OK", "PNY"], window_days=30, min_start_price=1.0)
    # PNY started the window below $1 → excluded entirely, despite huge moves.
    assert list(out.columns) == ["OK"]


def test_daily_returns_window_start_snaps_to_prior_bar():
    # Latest bar 2026-08-05, window 7 calendar days → target 2026-07-29.
    # AAA has no bar on the 29th; the 28th's close anchors the window.
    prices = _prices_frame(
        {"AAA": [100.0, 110.0, 121.0]},
        ["2026-07-28", "2026-08-01", "2026-08-05"],
    )
    out = rm.daily_returns(prices, ["AAA"], window_days=7)
    assert len(out) == 2
    assert out["AAA"].iloc[0] == pytest.approx(0.10)  # 100 → 110


def test_daily_returns_no_window_start_bar_excluded():
    # BBB's first bar is inside the window → no window-start price → excluded.
    prices = _prices_frame(
        {"AAA": [100.0, 105.0, 110.25], "BBB": [None, 50.0, 55.0]},
        ["2026-07-01", "2026-08-04", "2026-08-05"],
    )
    out = rm.daily_returns(prices, ["AAA", "BBB"], window_days=7)
    assert list(out.columns) == ["AAA"]


def test_daily_returns_missing_bar_is_nan():
    prices = _prices_frame(
        {"AAA": [100.0, 100.0, 110.0, 121.0], "BBB": [50.0, 50.0, None, 60.0]},
        ["2026-07-01", "2026-08-03", "2026-08-04", "2026-08-05"],
    )
    out = rm.daily_returns(prices, ["AAA", "BBB"], window_days=30)
    assert np.isnan(out.loc["2026-08-04", "BBB"])
    # BBB's last return spans its own bars: 50 → 60.
    assert out.loc["2026-08-05", "BBB"] == pytest.approx(0.20)


# ---------------------------------------------------------------------------
# bucket_daily_series
# ---------------------------------------------------------------------------

def _weights_frame(rows: list[tuple[str, str, float]], col: str = "weight_score") -> pd.DataFrame:
    return pd.DataFrame(
        [{"bucket_id": b, "bucket_name": b.title(), "ticker": t, col: w} for b, t, w in rows]
    )


def test_bucket_daily_series_weighted_sum_and_daily_renormalisation():
    idx = pd.to_datetime(["2026-08-04", "2026-08-05"])
    daily = pd.DataFrame({"AAA": [0.10, 0.02], "BBB": [np.nan, -0.01]}, index=idx)
    weights = _weights_frame([("b1", "AAA", 0.6), ("b1", "BBB", 0.4)])
    out = rm.bucket_daily_series(weights, daily, "weight_score")
    # Day 1: BBB has no bar → weights renormalise to AAA alone.
    assert out.loc[idx[0], "b1"] == pytest.approx(0.10)
    # Day 2: both price → 0.6*0.02 + 0.4*(-0.01).
    assert out.loc[idx[1], "b1"] == pytest.approx(0.6 * 0.02 + 0.4 * -0.01)


def test_bucket_daily_series_no_members_priced_is_nan():
    idx = pd.to_datetime(["2026-08-04", "2026-08-05"])
    daily = pd.DataFrame({"AAA": [0.10, 0.02]}, index=idx)
    weights = _weights_frame([("b1", "AAA", 0.6), ("b2", "ZZZ", 1.0)])
    out = rm.bucket_daily_series(weights, daily, "weight_score")
    assert out["b2"].isna().all()


def test_bucket_daily_series_cap_aug_uses_capped_weight_cap_score():
    idx = pd.to_datetime(["2026-08-05"])
    daily = pd.DataFrame({"AAA": [0.10], "BBB": [0.0], "CCC": [0.0]}, index=idx)
    weights = _weights_frame(
        [("b1", "AAA", 0.8), ("b1", "BBB", 0.1), ("b1", "CCC", 0.1)],
        col="weight_cap_score",
    )
    # m=1, N=3 → every name capped at 1/3; AAA's 0.8 raw weight is cut.
    out = rm.bucket_daily_series(weights, daily, "weight_cap_score_aug", weight_cap_m=1)
    assert out.loc[idx[0], "b1"] == pytest.approx(0.10 / 3)


def test_bucket_daily_series_missing_weight_column_raises():
    daily = pd.DataFrame({"AAA": [0.1]}, index=pd.to_datetime(["2026-08-05"]))
    weights = _weights_frame([("b1", "AAA", 1.0)])
    with pytest.raises(ValueError, match="weight_equal"):
        rm.bucket_daily_series(weights, daily, "weight_equal")


# ---------------------------------------------------------------------------
# capped_weights
# ---------------------------------------------------------------------------

def test_capped_weights_respects_cap_and_sums_to_one():
    w = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    out = rm.capped_weights(w, m=2)  # cap = 2/3, not binding
    assert out.sum() == pytest.approx(1.0)
    assert out.tolist() == pytest.approx([0.5, 0.3, 0.2])

    out2 = rm.capped_weights(pd.Series({"A": 0.7, "B": 0.2, "C": 0.1}), m=1.5)
    cap = 1.5 / 3
    assert out2.sum() == pytest.approx(1.0)
    assert (out2 <= cap + 1e-9).all()
    assert out2["A"] == pytest.approx(cap)


def test_capped_weights_iterative_second_name_pushed_over():
    # N=4, m=1.2 → cap 0.3. Capping A redistributes 0.1 proportionally,
    # pushing B (0.28 → 0.3267) over the cap; a second pass is required.
    w = pd.Series({"A": 0.40, "B": 0.28, "C": 0.20, "D": 0.12})
    out = rm.capped_weights(w, m=1.2)
    assert out.sum() == pytest.approx(1.0)
    assert out["A"] == pytest.approx(0.30)
    assert out["B"] == pytest.approx(0.30)
    assert out["C"] == pytest.approx(0.25)
    assert out["D"] == pytest.approx(0.15)


def test_capped_weights_m_equals_one_gives_equal_weights():
    w = pd.Series({"A": 0.5, "B": 0.3, "C": 0.2})
    out = rm.capped_weights(w, m=1)
    assert out.tolist() == pytest.approx([1 / 3, 1 / 3, 1 / 3])
    assert out.sum() == pytest.approx(1.0)


def test_capped_weights_normalises_unnormalised_input():
    out = rm.capped_weights(pd.Series({"A": 5.0, "B": 3.0, "C": 2.0}), m=2)
    assert out.sum() == pytest.approx(1.0)
    assert out["A"] == pytest.approx(0.5)


def test_capped_weights_m_below_one_raises():
    with pytest.raises(ValueError, match=">= 1"):
        rm.capped_weights(pd.Series([0.6, 0.4]), m=0.5)


# ---------------------------------------------------------------------------
# median_constituent_returns
# ---------------------------------------------------------------------------

def test_median_per_window_membership_differences():
    weights = _weights_frame([("b1", "AAA", 0.4), ("b1", "BBB", 0.3), ("b1", "CCC", 0.3)])
    ticker_returns = {
        "AAA": {"1y": 0.10, "1m": 0.05},
        "BBB": {"1y": 0.30, "1m": None},  # penny-filtered / no price in 1m
        "CCC": {"1y": 0.20, "1m": 0.01},
    }
    out = rm.median_constituent_returns(weights, ticker_returns)
    # 1y: median of three (median stock = CCC); 1m: median of two (BBB
    # excluded) → the median stock differs between windows. Expected.
    assert out["b1"]["1y"] == pytest.approx(0.20)
    assert out["b1"]["1m"] == pytest.approx((0.05 + 0.01) / 2)


def test_median_no_valid_constituents_is_none():
    weights = _weights_frame([("b1", "AAA", 1.0)])
    out = rm.median_constituent_returns(weights, {"AAA": {"1w": None}})
    assert out["b1"]["1w"] is None


# ---------------------------------------------------------------------------
# load_report_config
# ---------------------------------------------------------------------------

DEFAULTS = {
    "sharpe_window": "3m",
    "benchmark_ticker": "QQQ",
    "weight_col": "weight_score",
    "weight_cap_m": 3,
}


def _write_config(tmp_path: Path, report: dict | None) -> Path:
    cfg: dict = {"batch_model": "claude-haiku-4-5"}
    if report is not None:
        cfg["report"] = report
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg), encoding="utf-8")
    return p


def test_config_defaults_when_report_block_absent(tmp_path: Path):
    p = _write_config(tmp_path, report=None)
    assert rm.load_report_config(p) == DEFAULTS


def test_config_defaults_when_file_absent(tmp_path: Path):
    assert rm.load_report_config(tmp_path / "nope.json") == DEFAULTS


def test_config_underscore_keys_ignored(tmp_path: Path):
    p = _write_config(
        tmp_path,
        report={
            "sharpe_window": "1m",
            "_valid_values": {"sharpe_window": ["bogus"]},
            "_note": "documentation only",
        },
    )
    cfg = rm.load_report_config(p)
    assert cfg["sharpe_window"] == "1m"
    assert "_valid_values" not in cfg
    assert "_note" not in cfg


def test_config_partial_block_merges_defaults(tmp_path: Path):
    p = _write_config(tmp_path, report={"weight_col": "weight_cap_score_aug"})
    cfg = rm.load_report_config(p)
    assert cfg["weight_col"] == "weight_cap_score_aug"
    assert cfg["sharpe_window"] == "3m"
    assert cfg["benchmark_ticker"] == "QQQ"
    assert cfg["weight_cap_m"] == 3


def test_config_invalid_sharpe_window_raises(tmp_path: Path):
    p = _write_config(tmp_path, report={"sharpe_window": "2y"})
    with pytest.raises(ValueError, match=r"sharpe_window.*'2y'.*1y, 6m, 3m, 1m, 1w"):
        rm.load_report_config(p)


def test_config_invalid_weight_col_raises(tmp_path: Path):
    p = _write_config(tmp_path, report={"weight_col": "weight_bogus"})
    with pytest.raises(ValueError, match=r"weight_col.*'weight_bogus'.*weight_score"):
        rm.load_report_config(p)


@pytest.mark.parametrize("bad_m", [0.5, 0, -3, "3", True, None])
def test_config_invalid_weight_cap_m_raises(tmp_path: Path, bad_m):
    p = _write_config(tmp_path, report={"weight_cap_m": bad_m})
    with pytest.raises(ValueError, match=r"weight_cap_m"):
        rm.load_report_config(p)


def test_config_float_weight_cap_m_accepted(tmp_path: Path):
    p = _write_config(tmp_path, report={"weight_cap_m": 1.5})
    assert rm.load_report_config(p)["weight_cap_m"] == 1.5
