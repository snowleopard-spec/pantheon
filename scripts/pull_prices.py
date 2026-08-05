"""Pull daily closing prices from Polygon.io for every ticker in a
minidex weights CSV, plus the report benchmark ticker (`report.benchmark_ticker`
in config.json, default QQQ), which is unioned into the fetch list so the
droplet cron picks it up with no extra flags.
Saves to data/prices.csv with columns (date,ticker,close).

Incremental by default: if data/prices.csv already exists, only fetches
bars newer than the latest cached date per ticker (and full lookback for
tickers not yet in the cache). Pass --full to force a complete refetch.

Usage:
    uv run python scripts/pull_prices.py [--weights definitions/minidex_weights.csv]
                                         [--out data/prices.csv]
                                         [--days 400]
                                         [--workers 8]
                                         [--full]

Adjusted closes (splits + dividends). Default 400 calendar days back so
1-year return windows have prior-day context.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv

# Same-directory import: scripts/ is on sys.path when run as a script
# (the mechanism the droplet cron relies on). A missing report_metrics
# module is a loud failure by design — silently dropping the benchmark
# ticker would be worse.
import report_metrics

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE_URL = "https://api.polygon.io/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"


def fetch_one(ticker: str, start: str, end: str, api_key: str, session: requests.Session) -> list[dict]:
    """One ticker, one request. Returns [] on failure."""
    url = BASE_URL.format(ticker=ticker, start=start, end=end)
    params = {"adjusted": "true", "sort": "asc", "limit": 50000, "apiKey": api_key}
    for attempt in range(4):
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException as exc:
            print(f"[{ticker}] network error: {exc}", file=sys.stderr)
            time.sleep(2 ** attempt)
            continue
        if r.status_code == 429:
            wait = min(2 ** attempt, 30)
            time.sleep(wait)
            continue
        if r.status_code == 404:
            return []
        if not r.ok:
            print(f"[{ticker}] HTTP {r.status_code}: {r.text[:120]}", file=sys.stderr)
            return []
        body = r.json()
        if body.get("status") == "ERROR":
            print(f"[{ticker}] Polygon error: {body.get('error')}", file=sys.stderr)
            return []
        rows = body.get("results") or []
        return [
            {
                "date": pd.Timestamp(row["t"], unit="ms").date().isoformat(),
                "ticker": ticker,
                "close": row["c"],
            }
            for row in rows
        ]
    print(f"[{ticker}] gave up after retries", file=sys.stderr)
    return []


def _load_cache(out_path: Path) -> pd.DataFrame:
    """Existing prices CSV, or empty frame with the right columns."""
    if not out_path.exists():
        return pd.DataFrame(columns=["date", "ticker", "close"])
    df = pd.read_csv(out_path, dtype={"ticker": str})
    df["ticker"] = df["ticker"].str.upper()
    return df


def _plan_fetch(
    tickers: list[str],
    cache: pd.DataFrame,
    full_start: date,
    end: date,
    full: bool,
) -> tuple[dict[str, tuple[str, str]], int, int, int]:
    """For each ticker, return the (start, end) window to actually fetch.

    Returns:
        plan: {ticker: (start_iso, end_iso)}
        n_full: tickers needing a full-lookback fetch (uncached)
        n_incremental: tickers where we only pull the tail
        n_uptodate: tickers already through today (no HTTP call)
    """
    plan: dict[str, tuple[str, str]] = {}
    n_full = n_incremental = n_uptodate = 0
    if full or cache.empty:
        for t in tickers:
            plan[t] = (full_start.isoformat(), end.isoformat())
        return plan, len(tickers), 0, 0

    latest_by_ticker = (
        cache.groupby("ticker")["date"].max().to_dict()
    )
    # Grace period: if the newest cached bar is within 3 days of today,
    # consider the ticker up-to-date. Covers weekends and short holidays
    # where Polygon returns no new bars for the ticker.
    grace_days = 3
    for t in tickers:
        last = latest_by_ticker.get(t)
        if last is None:
            plan[t] = (full_start.isoformat(), end.isoformat())
            n_full += 1
            continue
        last_date = pd.Timestamp(last).date()
        if (end - last_date).days <= grace_days:
            n_uptodate += 1
            continue
        plan[t] = ((last_date + timedelta(days=1)).isoformat(), end.isoformat())
        n_incremental += 1
    return plan, n_full, n_incremental, n_uptodate


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("POLYGON_API_KEY", "").strip()
    if not api_key:
        sys.exit("POLYGON_API_KEY missing from environment / .env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="definitions/minidex_weights.csv")
    parser.add_argument("--out", default="data/prices.csv")
    parser.add_argument("--days", type=int, default=400,
                        help="Calendar-day lookback for tickers not yet in the cache (default 400)")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--full", action="store_true",
                        help="Ignore existing cache and refetch every ticker's full window")
    args = parser.parse_args()

    weights_path = REPO_ROOT / args.weights if not Path(args.weights).is_absolute() else Path(args.weights)
    out_path = REPO_ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    weights_df = pd.read_csv(weights_path)
    tickers = set(weights_df["ticker"].astype(str).str.upper())
    benchmark = str(report_metrics.load_report_config()["benchmark_ticker"]).upper()
    tickers = sorted(tickers | {benchmark})
    end = date.today()
    full_start = end - timedelta(days=args.days)

    cache = _load_cache(out_path)
    plan, n_full, n_incremental, n_uptodate = _plan_fetch(
        tickers, cache, full_start, end, args.full
    )
    n_calls = len(plan)
    print(
        f"pull_prices: {len(tickers)} tickers | cache={len(cache):,} rows | "
        f"plan: full={n_full} incremental={n_incremental} uptodate={n_uptodate} "
        f"→ {n_calls} HTTP calls (workers={args.workers})"
    )
    if n_calls == 0:
        # Everything up to date — just rewrite the cache as-is (idempotent).
        cache.sort_values(["ticker", "date"]).to_csv(out_path, index=False)
        print(f"pull_prices: no new bars needed; cache untouched ({len(cache):,} rows)")
        return

    session = requests.Session()
    new_rows: list[dict] = []
    n_done = n_empty = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(fetch_one, t, s, e, api_key, session): t
            for t, (s, e) in plan.items()
        }
        for fut in as_completed(futures):
            t = futures[fut]
            rows = fut.result()
            if not rows:
                n_empty += 1
            new_rows.extend(rows)
            n_done += 1
            if n_done % 25 == 0 or n_done == n_calls:
                print(f"pull_prices: {n_done}/{n_calls} done, {n_empty} empty, "
                      f"{len(new_rows):,} new rows fetched")

    new_df = pd.DataFrame(new_rows)
    if new_df.empty and cache.empty:
        sys.exit("pull_prices: no rows fetched and no cache — nothing to write")

    # Merge, dedupe on (ticker, date), keep latest close for any duplicate
    combined = pd.concat([cache, new_df], ignore_index=True)
    combined = combined.drop_duplicates(subset=["ticker", "date"], keep="last")
    combined = combined.sort_values(["ticker", "date"]).reset_index(drop=True)
    combined.to_csv(out_path, index=False)
    print(
        f"pull_prices: wrote {len(combined):,} rows across {combined['ticker'].nunique()} "
        f"tickers to {out_path} (+{len(new_df):,} new)"
    )


if __name__ == "__main__":
    main()
