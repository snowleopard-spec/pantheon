"""Performance backtest for a frozen mini-dex weights file.

Reads a long-format weights CSV (produced by ``minidex build``) plus a
price history CSV, and computes a **static** (no-rebalance) cumulative
return per bucket.

Delisted names are held at their **terminal** (last observed) cumulative
return; they contribute their frozen weight to the bucket after the
delisting date but their per-day return is 0 from that point on. This
mirrors a passive holder who kept the tape-out proceeds in the stub.

Usage:
    uv run python scripts/performance.py \\
        --weights outputs/<date>/minidex_weights.csv \\
        --prices prices.csv \\
        --weight-col weight_cap_score \\
        [--benchmark SPY] \\
        [--fetch-yf]
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import typer

app = typer.Typer(add_completion=False, help="Static backtest for frozen mini-dex weights.")


def _load_prices_csv(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = {"date", "ticker", "close"} - set(df.columns)
    if missing:
        raise typer.BadParameter(f"prices file missing columns: {sorted(missing)}")
    df["date"] = pd.to_datetime(df["date"])
    df["ticker"] = df["ticker"].astype(str).str.upper()
    df["close"] = pd.to_numeric(df["close"], errors="coerce")
    df = df.dropna(subset=["close"]).sort_values(["ticker", "date"])
    return df


def _fetch_prices_yf(tickers: list[str], start: Optional[str] = None) -> pd.DataFrame:
    """Fetch price history via yfinance. Imported lazily so tests don't need it."""
    import yfinance as yf  # optional dependency

    if not tickers:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    hist = yf.download(
        tickers=" ".join(tickers),
        start=start,
        auto_adjust=True,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    rows: list[pd.DataFrame] = []
    if isinstance(hist.columns, pd.MultiIndex):
        for tkr in tickers:
            if tkr not in hist.columns.get_level_values(0):
                continue
            sub = hist[tkr][["Close"]].dropna().reset_index()
            sub = sub.rename(columns={"Date": "date", "Close": "close"})
            sub["ticker"] = tkr
            rows.append(sub[["date", "ticker", "close"]])
    else:
        sub = hist[["Close"]].dropna().reset_index()
        sub = sub.rename(columns={"Date": "date", "Close": "close"})
        sub["ticker"] = tickers[0]
        rows.append(sub[["date", "ticker", "close"]])
    if not rows:
        return pd.DataFrame(columns=["date", "ticker", "close"])
    out = pd.concat(rows, ignore_index=True)
    out["date"] = pd.to_datetime(out["date"])
    out["ticker"] = out["ticker"].astype(str).str.upper()
    return out


def _pivot_prices(prices: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Wide price frame indexed by date, columns per ticker, forward-filled
    across gaps and terminal-held after delisting (ffill only, no bfill)."""
    if prices.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([]), columns=tickers, dtype=float)
    wide = prices.pivot_table(
        index="date", columns="ticker", values="close", aggfunc="last"
    ).sort_index()
    # Ensure every ticker column present, even if no data (all NaN column).
    for t in tickers:
        if t not in wide.columns:
            wide[t] = np.nan
    wide = wide[tickers]
    # Forward-fill: this is how we implement the "hold terminal return" rule
    # for delisted / halted names — the last-known close is carried forward.
    wide = wide.ffill()
    return wide


def _cumulative_bucket_returns(
    weights: pd.DataFrame,
    prices_wide: pd.DataFrame,
    weight_col: str,
) -> pd.DataFrame:
    """Static-weight cumulative returns per bucket, indexed by date."""
    if prices_wide.empty:
        return pd.DataFrame(index=pd.DatetimeIndex([]))

    dates = prices_wide.index
    buckets = weights["bucket_id"].unique().tolist()
    result = pd.DataFrame(index=dates, columns=buckets, dtype=float)

    for bid in buckets:
        members = weights[weights["bucket_id"] == bid]
        tickers = members["ticker"].tolist()
        w = members.set_index("ticker")[weight_col].astype(float)
        # Ignore any ticker with no price data at all.
        available = [t for t in tickers if t in prices_wide.columns
                     and prices_wide[t].notna().any()]
        if not available:
            result[bid] = np.nan
            continue
        w_avail = w.reindex(available)
        # Renormalise across the members we can price (mirrors dropping the
        # unfindable names before freeze).
        if w_avail.sum() > 0:
            w_norm = w_avail / w_avail.sum()
        else:
            w_norm = pd.Series(1.0 / len(available), index=available)

        sub = prices_wide[available].copy()
        # First observed price per ticker (post-ffill this is the earliest
        # date at which that ticker has *any* price).
        first_valid = {t: sub[t].first_valid_index() for t in available}
        # Base price for normalisation
        base = pd.Series({t: sub[t].loc[first_valid[t]] for t in available})
        norm = sub.divide(base, axis=1)
        # Before a ticker's first observation, treat its normalised price as
        # 1.0 (i.e. no contribution to return yet).
        norm = norm.fillna(1.0)
        # Static-weight cumulative return path.
        result[bid] = (norm * w_norm).sum(axis=1)

    return result


def _benchmark_cumret(prices: pd.DataFrame, ticker: str, dates: pd.DatetimeIndex) -> Optional[pd.Series]:
    sub = prices[prices["ticker"] == ticker.upper()].sort_values("date")
    if sub.empty:
        return None
    s = sub.set_index("date")["close"].reindex(dates).ffill()
    base = s.dropna().iloc[0] if s.notna().any() else None
    if base is None or base == 0:
        return None
    return s / base


@app.command()
def main(
    weights: Path = typer.Option(..., "--weights", help="Path to minidex_weights.csv"),
    prices: Optional[Path] = typer.Option(
        None, "--prices", help="Prices CSV: columns date,ticker,close"
    ),
    weight_col: str = typer.Option(
        "weight_cap_score", "--weight-col",
        help="Which weight column to use: weight_cap_score | weight_equal | weight_score",
    ),
    benchmark: Optional[str] = typer.Option(
        None, "--benchmark", help="Optional benchmark ticker to add as a column."
    ),
    fetch_yf: bool = typer.Option(
        False, "--fetch-yf", help="Fetch prices via yfinance (ignores --prices)."
    ),
) -> None:
    """Compute static-weight cumulative returns per bucket for a frozen weights file."""
    weights_df = pd.read_csv(weights)
    missing = {"bucket_id", "ticker", weight_col} - set(weights_df.columns)
    if missing:
        raise typer.BadParameter(
            f"weights file missing columns: {sorted(missing)}"
        )
    weights_df["ticker"] = weights_df["ticker"].astype(str).str.upper()

    all_tickers = sorted(set(weights_df["ticker"].tolist()))
    if benchmark:
        all_tickers = sorted(set(all_tickers) | {benchmark.upper()})

    if fetch_yf:
        prices_df = _fetch_prices_yf(all_tickers)
    else:
        if prices is None:
            raise typer.BadParameter("Provide --prices or --fetch-yf")
        prices_df = _load_prices_csv(prices)

    member_tickers = sorted(set(weights_df["ticker"].tolist()))
    prices_wide = _pivot_prices(prices_df, member_tickers)

    cum = _cumulative_bucket_returns(weights_df, prices_wide, weight_col)

    if benchmark and not cum.empty:
        bench = _benchmark_cumret(prices_df, benchmark, cum.index)
        if bench is not None:
            cum[benchmark.upper()] = bench

    # Print a compact summary (final row) and the full time series.
    typer.echo("")
    typer.echo(f"Cumulative returns using {weight_col}:")
    if cum.empty:
        typer.echo("(no data)")
    else:
        # Print head/tail for readability if long
        display = cum.copy()
        display.index = display.index.strftime("%Y-%m-%d")
        typer.echo(display.round(4).to_string())
        typer.echo("")
        typer.echo("Final cumulative return per bucket:")
        typer.echo(cum.iloc[-1].round(4).to_string())

    out_path = weights.parent / f"bucket_returns_{weight_col}.csv"
    cum_to_write = cum.copy()
    cum_to_write.index.name = "date"
    cum_to_write.to_csv(out_path)
    typer.echo(f"\nWrote returns to {out_path}")


if __name__ == "__main__":
    app()
