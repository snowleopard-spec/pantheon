"""Pure-compute metrics for the v2.1 bucket-returns report.

Why this module exists: `bucket_returns.py` runs on the droplet with the lean
dependency profile (no `minidex` imports), so everything M2 adds — Sharpe
ratios, daily index series, capped weighting, median-constituent returns, and
the `report` config block — lives here as pandas/numpy/stdlib-only functions
that the renderer imports as a same-directory module. No I/O happens here
except the stdlib-json config read in `load_report_config`.

Conventions match the existing renderer: the calendar-day window map
(1y=365 … 1w=7) snapped to trading days via "last bar at or before the
target date", the $1 penny filter applied at window start, and per-day
weight renormalisation over members that priced (the daily analogue of
`compute_bucket_returns`' per-window renormalisation).

Public API (final signatures):

    load_report_config(path: Path | str | None = None) -> dict
        Read the ``report`` block of the repo-root config.json. Defaults
        apply when the file or block is absent; "_"-prefixed keys are
        ignored; invalid values raise ValueError naming the key, the bad
        value, and the valid options.

    daily_returns(prices: pd.DataFrame, tickers: Sequence[str],
                  window_days: int, min_start_price: float = 1.0
                  ) -> pd.DataFrame
        Per-ticker daily simple returns over the trailing window. Wide
        frame: index = trading days, one column per included ticker, NaN
        where a ticker has no bar. Tickers whose window-start price is
        below ``min_start_price`` (or who have no window-start bar) are
        excluded entirely.

    sharpe(series: pd.Series | Sequence[float]) -> float | None
        D1 convention: mean daily return / sample std (ddof=1) * sqrt(252),
        risk-free rate 0. None if fewer than 10 daily returns or zero/NaN
        std.

    bucket_daily_series(weights: pd.DataFrame, daily_rets: pd.DataFrame,
                        weight_col: str, weight_cap_m: float = 3.0
                        ) -> pd.DataFrame
        Per-bucket daily weighted return series (index = trading days,
        one column per bucket_id), weights renormalised daily over the
        members with a return that day. ``weight_col ==
        "weight_cap_score_aug"`` derives capped weights from the
        ``weight_cap_score`` column via ``capped_weights`` (§3.6).

    capped_weights(weights: pd.Series | Sequence[float], m: float
                   ) -> pd.Series
        One bucket's weights, capped at m/N with iterative proportional
        redistribution of the excess; result sums to 1. ValueError if
        m < 1.

    median_constituent_returns(weights: pd.DataFrame,
                               ticker_returns: dict[str, dict[str, float | None]]
                               ) -> dict[str, dict[str, float | None]]
        {bucket_id: {window label: unweighted median of valid constituent
        returns | None}}. Interoperates with `compute_ticker_returns`'
        output; the same exclusions (penny filter → None) apply, so the
        median is over exactly the names in the weighted number.

    residual_car_z(weights: pd.DataFrame, daily_rets: pd.DataFrame,
                   weight_col: str, weight_cap_m: float = 3.0
                   ) -> dict[str, dict[str, float | None]]
        v2.4: intra-bucket out/underperformer z — OLS residuals vs the
        bucket's leave-one-out daily series, CAR over the window, robust
        (median/MAD) cross-sectional z. None per the spec's exclusions.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Sequence

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

# Calendar-day lookbacks, identical to bucket_returns.WINDOWS.
WINDOW_DAYS: dict[str, int] = {"1y": 365, "6m": 180, "3m": 90, "1m": 30, "1w": 7}

TRADING_DAYS_PER_YEAR = 252
MIN_SHARPE_OBS = 10
MIN_Z_OBS = 10

# Source of truth for config validation — editing config.json's
# "_valid_values" documentation block cannot unlock new values.
VALID_SHARPE_WINDOWS = ("1y", "6m", "3m", "1m", "1w")
VALID_WEIGHT_COLS = (
    "weight_score",
    "weight_cap_score",
    "weight_cap_score_aug",
    "weight_equal",
)

REPORT_DEFAULTS: dict[str, object] = {
    "sharpe_window": "3m",
    "benchmark_ticker": "QQQ",
    "weight_col": "weight_score",
    "weight_cap_m": 3,
    "z_window": "3m",
}


def load_report_config(path: Path | str | None = None) -> dict:
    """Read the ``report`` block of config.json, validated, with defaults.

    Defaults apply when the file or the block is absent (a stale config
    breaks nothing). Keys starting with "_" are in-file documentation and
    ignored. Invalid values raise ValueError with a message naming the
    key, the bad value, and the valid options — bucket_returns.py lets
    this propagate for a loud exit rather than rendering silently wrong.
    """
    cfg_path = Path(path) if path is not None else REPO_ROOT / "config.json"

    block: dict = {}
    if cfg_path.exists():
        with open(cfg_path, encoding="utf-8") as fh:
            raw = json.load(fh)
        block = raw.get("report") or {}

    out = dict(REPORT_DEFAULTS)
    out.update({k: v for k, v in block.items() if not k.startswith("_")})

    sw = out["sharpe_window"]
    if sw not in VALID_SHARPE_WINDOWS:
        raise ValueError(
            f"report.sharpe_window: invalid value {sw!r} — "
            f"valid options: {', '.join(VALID_SHARPE_WINDOWS)}"
        )
    wc = out["weight_col"]
    if wc not in VALID_WEIGHT_COLS:
        raise ValueError(
            f"report.weight_col: invalid value {wc!r} — "
            f"valid options: {', '.join(VALID_WEIGHT_COLS)}"
        )
    m = out["weight_cap_m"]
    if isinstance(m, bool) or not isinstance(m, (int, float)) or not m >= 1:
        raise ValueError(
            f"report.weight_cap_m: invalid value {m!r} — "
            f"must be a number >= 1 (per-name cap = m/N)"
        )
    bt = out["benchmark_ticker"]
    if not isinstance(bt, str) or not bt.strip():
        raise ValueError(
            f"report.benchmark_ticker: invalid value {bt!r} — "
            f"must be a non-empty ticker string"
        )
    zw = out["z_window"]
    if zw not in VALID_SHARPE_WINDOWS:
        raise ValueError(
            f"report.z_window: invalid value {zw!r} — "
            f"valid options: {', '.join(VALID_SHARPE_WINDOWS)}"
        )
    return out


def daily_returns(
    prices: pd.DataFrame,
    tickers: Sequence[str],
    window_days: int,
    min_start_price: float = 1.0,
) -> pd.DataFrame:
    """Per-ticker daily simple-return series over the trailing window.

    ``prices`` has columns date/ticker/close (adjusted closes, one row per
    ticker-day, as in data/prices.csv). The window runs ``window_days``
    calendar days back from the latest bar in the frame, snapped to
    trading days: the window-start bar is the last bar at or before the
    target date (same rule as the returns table's `_price_at_or_before`).

    A ticker whose window-start price is below ``min_start_price`` — or
    which has no bar at or before the window start — is excluded for the
    whole window, consistent with the returns-table penny filter.

    Returns a wide frame: index = trading days (union over included
    tickers), one column per included ticker, NaN where that ticker has
    no bar that day. The first return of each ticker falls on its first
    bar *after* the window-start bar.
    """
    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    end = prices["date"].max()
    start_target = end - pd.Timedelta(days=window_days)

    wide = (
        prices.pivot_table(index="date", columns="ticker", values="close", aggfunc="last")
        .sort_index()
    )

    cols: dict[str, pd.Series] = {}
    for t in tickers:
        if t not in wide.columns:
            continue
        s = wide[t].dropna()
        head = s.loc[:start_target]
        if head.empty:
            continue  # no window-start bar
        p_start = float(head.iloc[-1])
        if p_start < min_start_price:
            continue  # penny filter: excluded for the whole window
        window = s.loc[head.index[-1]:]
        rets = window.pct_change().iloc[1:]
        if not rets.empty:
            cols[t] = rets

    if not cols:
        return pd.DataFrame(index=pd.DatetimeIndex([], name="date"))
    return pd.DataFrame(cols).sort_index()


def sharpe(series: pd.Series | Sequence[float]) -> float | None:
    """Annualised Sharpe ratio of a daily simple-return series (D1).

    mean daily return / sample std (ddof=1) * sqrt(252), risk-free rate
    0. NaNs are dropped first. Returns None when fewer than
    ``MIN_SHARPE_OBS`` daily returns remain or the std is zero/NaN.
    """
    vals = pd.Series(series, dtype=float).dropna().to_numpy()
    if len(vals) < MIN_SHARPE_OBS:
        return None
    std = float(np.std(vals, ddof=1))
    # A constant series produces float-noise std (~1e-18) rather than an
    # exact zero, so guard with a tolerance well below any real daily vol.
    if not math.isfinite(std) or std < 1e-12:
        return None
    return float(np.mean(vals) / std * math.sqrt(TRADING_DAYS_PER_YEAR))


def capped_weights(weights: pd.Series | Sequence[float], m: float) -> pd.Series:
    """Cap one bucket's weights at m/N, redistributing the excess (§3.6).

    Standard capped-index methodology: normalise to sum 1, cap every
    weight at m/N (N = member count), redistribute the excess
    proportionally among uncapped members, and iterate until no member
    exceeds the cap. The result sums to 1 throughout. Feasible whenever
    m >= 1; ValueError otherwise.
    """
    if not m >= 1:
        raise ValueError(f"weight_cap_m must be >= 1, got {m!r}")
    w = pd.Series(weights, dtype=float)
    total = w.sum()
    if total <= 0:
        raise ValueError("capped_weights: weights must have a positive sum")
    w = w / total

    n = len(w)
    cap = m / n
    if cap >= 1.0:
        return w  # cap is never binding

    eps = 1e-12
    is_capped = pd.Series(False, index=w.index)
    while True:
        over = (~is_capped) & (w > cap + eps)
        if not over.any():
            break
        excess = float((w[over] - cap).sum())
        w[over] = cap
        is_capped |= over
        free = ~is_capped
        free_sum = float(w[free].sum())
        if free_sum <= 0:
            # m >= 1 guarantees total capacity N*cap = m >= 1, so this
            # only occurs at m == 1 with everything already at the cap.
            break
        w[free] += excess * w[free] / free_sum
    return w


def bucket_daily_series(
    weights: pd.DataFrame,
    daily_rets: pd.DataFrame,
    weight_col: str,
    weight_cap_m: float = 3.0,
) -> pd.DataFrame:
    """Per-bucket daily weighted return series with daily renormalisation.

    ``weights`` is the minidex weights frame (bucket_id/ticker/weight
    columns); ``daily_rets`` is the wide frame from `daily_returns`. For
    each trading day, the bucket return is sum(w_i * r_i) over members
    with a return that day, weights renormalised over those members —
    the daily analogue of `compute_bucket_returns`' per-window
    renormalisation. Days where no member has a return are NaN.

    ``weight_col == "weight_cap_score_aug"`` derives each bucket's
    weights at compute time from the existing ``weight_cap_score``
    column via `capped_weights(..., weight_cap_m)` (§3.6); the frozen
    weights CSV is never touched.

    Returns a wide frame: index = daily_rets' trading days, one column
    per bucket_id (weights-frame order preserved).
    """
    if weight_col == "weight_cap_score_aug":
        base_col = "weight_cap_score"
    else:
        base_col = weight_col
    if base_col not in weights.columns:
        raise ValueError(
            f"bucket_daily_series: weights frame has no column {base_col!r}"
        )

    out: dict[str, pd.Series] = {}
    for bucket_id, g in weights.groupby("bucket_id", sort=False):
        w = g.set_index("ticker")[base_col].astype(float)
        if weight_col == "weight_cap_score_aug":
            w = capped_weights(w, weight_cap_m)
        members = [t for t in w.index if t in daily_rets.columns]
        if not members:
            out[bucket_id] = pd.Series(np.nan, index=daily_rets.index)
            continue
        sub = daily_rets[members]
        wv = w.loc[members]
        num = sub.mul(wv, axis=1).sum(axis=1, min_count=1)
        den = sub.notna().mul(wv, axis=1).sum(axis=1)
        out[bucket_id] = num.where(den > 0) / den.where(den > 0)
    return pd.DataFrame(out, index=daily_rets.index)


def residual_car_z(
    weights: pd.DataFrame,
    daily_rets: pd.DataFrame,
    weight_col: str,
    weight_cap_m: float = 3.0,
) -> dict[str, dict[str, float | None]]:
    """Intra-bucket out/underperformer z-scores (v2.4 spec §1).

    For each bucket member: OLS of its daily returns on the bucket's
    **leave-one-out** daily weighted series (the bucket recomputed without
    the member — regressing a heavy member on an index containing itself
    biases beta toward 1 and shrinks its residuals), CAR = sum of the
    daily residuals over the window, then a robust cross-sectional z
    within the bucket: (CAR − median) / (1.4826 × MAD).

    Returns {bucket_id: {ticker: z | None}} over the bucket's nominal
    membership. None for members excluded from the panel (penny filter /
    no window-start bar), members with fewer than ``MIN_Z_OBS``
    overlapping observations, degenerate buckets (< 2 members with data),
    and whole buckets where the MAD collapses below 1e-12.
    """
    base_col = "weight_cap_score" if weight_col == "weight_cap_score_aug" else weight_col
    if base_col not in weights.columns:
        raise ValueError(f"residual_car_z: weights frame has no column {base_col!r}")

    out: dict[str, dict[str, float | None]] = {}
    for bucket_id, g in weights.groupby("bucket_id", sort=False):
        w = g.set_index("ticker")[base_col].astype(float)
        if weight_col == "weight_cap_score_aug":
            w = capped_weights(w, weight_cap_m)
        z_out: dict[str, float | None] = {t: None for t in w.index}
        members = [t for t in w.index if t in daily_rets.columns]
        if len(members) < 2:
            out[bucket_id] = z_out
            continue

        sub = daily_rets[members]
        wv = w.loc[members]
        # Full-bucket per-day aggregates; each member's LOO series is the
        # aggregate minus its own contribution (numerator and the
        # renormalisation denominator alike).
        num_all = sub.mul(wv, axis=1).sum(axis=1, min_count=1)
        den_all = sub.notna().mul(wv, axis=1).sum(axis=1)

        cars: dict[str, float] = {}
        for t in members:
            r = sub[t]
            num_loo = num_all - (wv[t] * r).fillna(0.0)
            den_loo = den_all - wv[t] * r.notna().astype(float)
            loo = num_loo.where(den_loo > 1e-15) / den_loo.where(den_loo > 1e-15)
            mask = r.notna() & loo.notna()
            if int(mask.sum()) < MIN_Z_OBS:
                continue
            x = loo[mask].to_numpy(dtype=float)
            y = r[mask].to_numpy(dtype=float)
            xm, ym = x.mean(), y.mean()
            var = float(((x - xm) ** 2).sum())
            slope = float(((x - xm) * (y - ym)).sum() / var) if var > 1e-18 else 0.0
            inter = ym - slope * xm
            # CAR = intercept × n_obs: the window-total abnormal return.
            # (Residuals of an intercept-fitted OLS sum to zero on the
            # estimation window by construction — the abnormal drift IS
            # the intercept.)
            cars[t] = float(inter * len(y))

        if len(cars) < 2:
            out[bucket_id] = z_out
            continue
        vals = np.array(list(cars.values()), dtype=float)
        med = float(np.median(vals))
        scale = 1.4826 * float(np.median(np.abs(vals - med)))
        if scale < 1e-12:
            out[bucket_id] = z_out
            continue
        for t, car in cars.items():
            z_out[t] = float((car - med) / scale)
        out[bucket_id] = z_out
    return out


def median_constituent_returns(
    weights: pd.DataFrame,
    ticker_returns: dict[str, dict[str, float | None]],
) -> dict[str, dict[str, float | None]]:
    """Per bucket per window: the unweighted median of valid constituent
    returns.

    ``ticker_returns`` is `compute_ticker_returns`' output shape
    ({ticker: {label: float | None}}), so the penny-filter and
    missing-price exclusions (None) match the weighted bucket number
    exactly. The median stock may differ per window — expected and fine
    (§3.2). A window with no valid constituents yields None.
    """
    out: dict[str, dict[str, float | None]] = {}
    for bucket_id, g in weights.groupby("bucket_id", sort=False):
        labels: list[str] = []
        for t in g["ticker"]:
            for label in ticker_returns.get(t, {}):
                if label not in labels:
                    labels.append(label)
        med: dict[str, float | None] = {}
        for label in labels:
            vals = [
                r
                for t in g["ticker"]
                if (r := ticker_returns.get(t, {}).get(label)) is not None
                and not (isinstance(r, float) and math.isnan(r))
            ]
            med[label] = float(np.median(vals)) if vals else None
        out[bucket_id] = med
    return out
