"""Pull daily closing prices from Polygon.io for every ticker in a
minidex weights CSV. Saves to data/prices.csv with columns (date,ticker,close).

Usage:
    uv run python scripts/pull_prices.py [--weights outputs/<asof>/minidex_weights.csv]
                                         [--out data/prices.csv]
                                         [--days 400]
                                         [--workers 8]

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
            return []  # ticker not found on Polygon
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


def main() -> None:
    load_dotenv(REPO_ROOT / ".env")
    api_key = os.environ.get("POLYGON_API_KEY", "").strip()
    if not api_key:
        sys.exit("POLYGON_API_KEY missing from environment / .env")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="outputs/2026-07-25/minidex_weights.csv",
                        help="Path to a minidex_weights.csv")
    parser.add_argument("--out", default="data/prices.csv",
                        help="Where to write the prices CSV")
    parser.add_argument("--days", type=int, default=400,
                        help="Calendar-day lookback window (default 400)")
    parser.add_argument("--workers", type=int, default=8,
                        help="Concurrent HTTP workers (default 8)")
    args = parser.parse_args()

    weights_path = REPO_ROOT / args.weights if not Path(args.weights).is_absolute() else Path(args.weights)
    out_path = REPO_ROOT / args.out if not Path(args.out).is_absolute() else Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    df = pd.read_csv(weights_path)
    tickers = sorted(set(df["ticker"].astype(str).str.upper()))
    end = date.today()
    start = end - timedelta(days=args.days)
    print(f"pull_prices: {len(tickers)} tickers, {start} → {end}, workers={args.workers}")

    session = requests.Session()
    all_rows: list[dict] = []
    n_done = 0
    n_empty = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(fetch_one, t, start.isoformat(), end.isoformat(), api_key, session): t
            for t in tickers
        }
        for fut in as_completed(futures):
            t = futures[fut]
            rows = fut.result()
            if not rows:
                n_empty += 1
            all_rows.extend(rows)
            n_done += 1
            if n_done % 25 == 0 or n_done == len(tickers):
                print(f"pull_prices: {n_done}/{len(tickers)} done, {n_empty} empty, "
                      f"{len(all_rows):,} rows total")

    out_df = pd.DataFrame(all_rows)
    if out_df.empty:
        sys.exit("pull_prices: no rows fetched")
    out_df = out_df.sort_values(["ticker", "date"]).reset_index(drop=True)
    out_df.to_csv(out_path, index=False)
    print(f"pull_prices: wrote {len(out_df):,} rows across {out_df['ticker'].nunique()} tickers to {out_path}")


if __name__ == "__main__":
    main()
