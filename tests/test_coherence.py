"""Offline math tests for scripts/coherence.py (spec §9 — no mocks, no I/O
beyond tmp_path)."""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

# Load the module directly from scripts/ — it's not a package.
ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import coherence  # noqa: E402

BENCH = "QQQ"


def _prices_from_returns(rets: dict[str, np.ndarray], dates: pd.DatetimeIndex) -> pd.DataFrame:
    """Long (date, ticker, close) frame from per-ticker daily return arrays."""
    rows = []
    for t, r in rets.items():
        closes = 100.0 * np.exp(np.cumsum(r))
        for d, c in zip(dates, closes):
            rows.append({"date": d.date().isoformat(), "ticker": t, "close": float(c)})
    return pd.DataFrame(rows)


def _weights(buckets: dict[str, list[str]]) -> pd.DataFrame:
    rows = []
    for b, tickers in buckets.items():
        for i, t in enumerate(tickers):
            rows.append({"bucket_id": b, "bucket_name": b.replace("_", " ").title(),
                         "ticker": t, "score": 1.0 - 0.01 * i})
    return pd.DataFrame(rows)


def _planted_panel(seed: int = 7):
    """40-ticker synthetic universe: two planted coherent blocks + noise names.

    Every ticker has beta 1 on the market; blocks A and B share strong block
    factors on top. 260 business days.
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2025-01-02", periods=260)
    n = len(dates)
    market = rng.normal(0, 0.010, n)
    f_a = rng.normal(0, 0.012, n)
    f_b = rng.normal(0, 0.012, n)

    rets: dict[str, np.ndarray] = {BENCH: market}
    tickers = [f"T{i:02d}" for i in range(40)]
    for i, t in enumerate(tickers):
        eps = rng.normal(0, 0.008, n)
        if i < 8:        # block A
            rets[t] = market + f_a + eps * 0.5
        elif i < 16:     # block B
            rets[t] = market + f_b + eps * 0.5
        else:            # idiosyncratic
            rets[t] = market + eps
    return rets, dates, tickers


def _run(rets, dates, buckets, n_draws=2000, seed=42):
    prices = _prices_from_returns(rets, dates)
    weights = _weights(buckets)
    return coherence.compute(weights, prices, BENCH, "daily", 365, n_draws, seed, 0.67)


def test_planted_blocks_score_high_random_does_not():
    rets, dates, tickers = _planted_panel()
    buckets = {
        "block_a": tickers[:8],
        "block_b": tickers[8:16],
        # deliberately random group from the idiosyncratic names
        "random_grp": tickers[16:24],
    }
    res = _run(rets, dates, buckets)
    stats = res["stats"].set_index("bucket_id")
    assert stats.loc["block_a", "pctile"] > 95
    assert stats.loc["block_b", "pctile"] > 95
    # A random group must not systematically exceed the null (~50); allow
    # sampling noise but reject anything that looks like real structure.
    assert stats.loc["random_grp", "pctile"] < 90
    # Residualization should have stripped the shared market term: the random
    # group's residual rho is near zero.
    assert abs(stats.loc["random_grp", "rho_intra"]) < 0.1


def test_residualization_kills_market_beta():
    rng = np.random.default_rng(11)
    dates = pd.bdate_range("2025-01-02", periods=200)
    market = rng.normal(0, 0.01, len(dates))
    y = 2.0 * market + rng.normal(0, 0.0005, len(dates))
    rets_df = pd.DataFrame({"Y": y}, index=dates)
    bench = pd.Series(market, index=dates)
    resid = coherence.residualize(rets_df, bench)
    corr = np.corrcoef(resid["Y"].to_numpy(), market)[0, 1]
    assert abs(corr) < 0.05


def test_determinism_csv_byte_identical(tmp_path):
    rets, dates, tickers = _planted_panel()
    buckets = {"block_a": tickers[:8], "random_grp": tickers[16:24]}
    outs = []
    for i in range(2):
        res = _run(rets, dates, buckets, n_draws=1000, seed=123)
        disp = coherence.stats_display(res["stats"])
        p = tmp_path / f"run{i}.csv"
        coherence.write_csv(disp, p)
        outs.append(p.read_bytes())
    assert outs[0] == outs[1]


def test_effective_n_uses_surviving_size():
    rets, dates, tickers = _planted_panel()
    # 5 nominal members, 2 with no price data at all -> judged at n_eff=3
    buckets = {
        "partial": tickers[:3] + ["GHOST1", "GHOST2"],
        "too_thin": tickers[4:6] + ["GHOST3"],  # survives only 2 -> insufficient
        # wide filler bucket so the non-member draw pool is realistic
        "filler": tickers[6:30],
    }
    res = _run(rets, dates, buckets, n_draws=1000)
    stats = res["stats"].set_index("bucket_id")
    assert stats.loc["partial", "n_nominal"] == 5
    assert stats.loc["partial", "n_eff"] == 3
    assert stats.loc["partial", "status"] == "ok"
    assert not np.isnan(stats.loc["partial", "z"])
    assert stats.loc["too_thin", "n_eff"] == 2
    assert stats.loc["too_thin", "status"] == "insufficient"
    assert np.isnan(stats.loc["too_thin", "pctile"])
    # ghosts show up in the dropped list
    dropped = {t for t, _ in res["dropped"]}
    assert {"GHOST1", "GHOST2", "GHOST3"} <= dropped


def test_mean_upper_hand_checked():
    m = np.array([
        [1.0, 0.5, 0.2],
        [0.5, 1.0, 0.8],
        [0.2, 0.8, 1.0],
    ])
    assert abs(coherence.mean_upper(m) - 0.5) < 1e-12
