"""Compute weighted returns per bucket over five trailing windows and render
a stylish HTML report.

Windows: 1w, 1m, 3m, 6m, 1y (calendar-day lookbacks, snapped to the nearest
prior trading day per ticker). Bucket return = sum over members of
(weight × ticker_return), renormalising the weight column across members
that have valid prices for that window.

v2.1 additions: a configurable-window annualised Sharpe column (index and
constituent level), a pinned benchmark row (QQQ by default), median-constituent
lines under each index return, client-side sorting on both tables, and a
per-constituent price chart (vendored uPlot, inlined — the page stays fully
self-contained). Report knobs live in the root config.json "report" block,
read via report_metrics.load_report_config(); an explicit --weight-col flag
overrides the config.

Usage:
    uv run python scripts/bucket_returns.py [--weights definitions/minidex_weights.csv]
                                            [--prices data/prices.csv]
                                            [--weight-col weight_score]
                                            [--out outputs/bucket_returns.html]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

# Same-directory import: scripts/ is on sys.path when run as a script (the
# mechanism the droplet cron relies on). Tests insert scripts/ explicitly.
import report_metrics

REPO_ROOT = Path(__file__).resolve().parent.parent
VENDOR_DIR = REPO_ROOT / "assets" / "vendor"

# Display order: longest window first
WINDOWS = [
    ("1y", 365),
    ("6m", 180),
    ("3m", 90),
    ("1m", 30),
    ("1w", 7),
]

WEIGHT_COL_BLURB = {
    "weight_score": "score-normalised",
    "weight_cap_score": "market-cap × score",
    "weight_cap_score_aug": "market-cap × score, per-name cap m/N",
    "weight_equal": "equal-weighted",
}


def _load_company_names(db_path: Path) -> dict[str, str]:
    """ticker.upper() -> company name. Empty dict if DB unavailable."""
    if not db_path.exists():
        return {}
    try:
        conn = sqlite3.connect(db_path)
        rows = conn.execute("SELECT UPPER(ticker), name FROM companies").fetchall()
        conn.close()
    except sqlite3.Error:
        return {}
    return {t: n for t, n in rows if t and n}


def _load_search_data(
    db_path: Path,
    weights: pd.DataFrame,
    weight_col: str,
    score_floor: float,
    company_names: dict[str, str],
) -> str:
    """JSON payload for the company search: every scored (company, bucket)
    pair from the DB's latest_scores view — deliberately NO floor, so the
    panel can show why a company is (or isn't) in each sub-index. Joined
    against the active weights for membership/weight. Falls back to a
    weights-only payload if the DB is unavailable: the report must render
    regardless of search-data health.
    """
    bucket_names = (
        weights.drop_duplicates("bucket_id").set_index("bucket_id")["bucket_name"].to_dict()
    )
    member_weight = {
        (r["bucket_id"], r["ticker"]): float(r[weight_col]) * 100
        for _, r in weights.iterrows()
    }
    rows: list = []
    if db_path.exists():
        try:
            conn = sqlite3.connect(db_path)
            rows = conn.execute(
                "SELECT UPPER(ls.ticker), c.name, c.market_cap, ls.bucket_id, "
                "ls.score, ls.confidence "
                "FROM latest_scores ls LEFT JOIN companies c ON c.cik = ls.cik"
            ).fetchall()
            conn.close()
        except sqlite3.Error as exc:
            print(f"bucket_returns: warn — search DB read failed ({exc}); "
                  "search falls back to index members only", file=sys.stderr)
    else:
        print(f"bucket_returns: warn — {db_path} missing; search covers index "
              "members only", file=sys.stderr)

    companies: dict[str, dict] = {}
    if rows:
        for ticker, name, mcap, bucket_id, score, conf in rows:
            c = companies.setdefault(ticker, {
                "t": ticker,
                "n": name or company_names.get(ticker, ""),
                "m": float(mcap) if mcap is not None else None,
                "s": [],
            })
            w = member_weight.get((bucket_id, ticker))
            c["s"].append([bucket_id, round(float(score), 4), conf or "",
                           round(w, 2) if w is not None else None])
    else:
        for _, r in weights.iterrows():
            t = r["ticker"]
            c = companies.setdefault(t, {
                "t": t,
                "n": company_names.get(t, ""),
                "m": None,
                "s": [],
            })
            c["s"].append([r["bucket_id"], round(float(r["score"]), 4),
                           str(r.get("confidence", "")),
                           round(float(r[weight_col]) * 100, 2)])

    payload = {
        "floor": score_floor,
        "buckets": bucket_names,
        "companies": sorted(companies.values(), key=lambda c: c["t"]),
    }
    # Same script-breakout hardening as the PRICES blob.
    return json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")


def _load_descriptions(path: Path) -> dict[str, str]:
    """definitions/company_descriptions.yaml -> {TICKER: text}.

    Hand-editable file; missing or unparseable degrades to {} (the report
    never fails over description health), but parse errors are loud.
    """
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        print(f"bucket_returns: warn — could not parse {path.name}: {e}",
              file=sys.stderr)
        return {}
    if not isinstance(data, dict):
        print(f"bucket_returns: warn — {path.name} is not a mapping; ignoring",
              file=sys.stderr)
        return {}
    return {str(k).upper(): " ".join(str(v).split()) for k, v in data.items() if v}


def _price_at_or_before(prices: pd.DataFrame, ticker: str, target: date) -> float | None:
    sub = prices.loc[(prices["ticker"] == ticker) & (prices["date"] <= pd.Timestamp(target))]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def compute_ticker_returns(
    tickers: list[str],
    prices: pd.DataFrame,
    min_start_price: float = 1.0,
) -> dict[str, dict[str, float | None]]:
    """Per-ticker trailing returns keyed by window label. None if excluded.

    A ticker is excluded from a given window if its starting price is below
    ``min_start_price``. This filters near-dead penny-stock recoveries whose
    percentage returns swamp aggregates (e.g., Zapata Quantum trading from
    $0.0001 to $0.90 = +899,900%). Standard practice in equity return analysis.
    """
    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values(["ticker", "date"]).reset_index(drop=True)

    ref = pd.Timestamp(date.today())
    end_date = min(ref, prices["date"].max()).date()
    end_prices = {t: _price_at_or_before(prices, t, end_date) for t in tickers}

    out: dict[str, dict[str, float | None]] = {}
    for t in tickers:
        p_end = end_prices.get(t)
        rets: dict[str, float | None] = {}
        for label, days in WINDOWS:
            target = end_date - timedelta(days=days)
            p_start = _price_at_or_before(prices, t, target)
            if p_end is None or p_start is None or p_start == 0:
                rets[label] = None
            elif p_start < min_start_price:
                rets[label] = None  # excluded: penny-stock start
            else:
                rets[label] = (p_end / p_start) - 1.0
        out[t] = rets
    return out


def compute_bucket_returns(
    weights: pd.DataFrame,
    ticker_returns: dict[str, dict[str, float | None]],
    weight_col: str,
) -> pd.DataFrame:
    """Per bucket, per window: sum(w_i × return_i) with per-window renormalisation."""
    out_rows = []
    for bucket_id, g in weights.groupby("bucket_id", sort=False):
        bucket_name = g["bucket_name"].iloc[0]
        n_members = len(g)
        row = {"bucket_id": bucket_id, "bucket_name": bucket_name, "n_members": n_members}
        for label, _days in WINDOWS:
            member_returns = []
            member_weights = []
            for _, m in g.iterrows():
                t = m["ticker"]
                w = float(m[weight_col])
                r = ticker_returns.get(t, {}).get(label)
                if r is None:
                    continue
                member_returns.append(r)
                member_weights.append(w)
            if not member_weights:
                row[label] = np.nan
                continue
            wsum = sum(member_weights)
            if wsum == 0:
                row[label] = np.nan
                continue
            norm_weights = [w / wsum for w in member_weights]
            row[label] = sum(w * r for w, r in zip(norm_weights, member_returns))
        out_rows.append(row)

    return pd.DataFrame(out_rows)


def _fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '<span class="na">—</span>'
    pct = x * 100
    cls = "pos" if pct > 0.05 else ("neg" if pct < -0.05 else "flat")
    sign = "+" if pct > 0 else ""
    return f'<span class="{cls}">{sign}{pct:.2f}%</span>'


def _fmt_sharpe(x: float | None) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '<span class="na">—</span>'
    cls = "pos" if x > 0.005 else ("neg" if x < -0.005 else "flat")
    return f'<span class="{cls}">{x:.2f}</span>'


def _fmt_z(x: float | None) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '<span class="na">—</span>'
    cls = "pos" if x > 0.005 else ("neg" if x < -0.005 else "flat")
    return f'<span class="{cls}">{x:+.2f}</span>'


def _sort_attr(x: float | None) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return ""
    return f"{x:.6f}"


# ---------------------------------------------------------------------------
# Static CSS / JS blocks (plain strings — no f-string brace escaping needed)
# ---------------------------------------------------------------------------

CSS = """
  html { -webkit-text-size-adjust: 100%; background: #0d1117; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #e6edf3;
    background: #0d1117;
    max-width: 1100px;
    margin: 2rem auto;
    padding: 0 1.4rem 3rem;
    line-height: 1.5;
    font-size: 15px;
  }
  h1 {
    font-size: 3rem;
    letter-spacing: -0.02em;
    margin: 0.6rem 0 1.6rem 0;
    color: #f0f6fc;
    text-align: center;
    font-weight: 700;
  }
  .updated {
    text-align: center;
    color: #8b949e;
    font-size: 0.85rem;
    margin: -1.2rem 0 1.6rem 0;
    letter-spacing: 0.01em;
  }
  .updated a { color: #58a6ff; text-decoration: none; }
  .updated a:hover { text-decoration: underline; }
  table {
    width: 100%;
    background: #161b22;
    border-collapse: collapse;
    border: 1px solid #30363d;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.25);
    font-variant-numeric: tabular-nums;
  }
  thead th {
    background: #1c232c;
    padding: 0.65rem 0.7rem;
    text-align: center;
    font-weight: 600;
    font-size: 0.85rem;
    color: #b1bac4;
    border-bottom: 1px solid #30363d;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }
  thead th.left { text-align: center; }
  tbody td {
    padding: 0.65rem 0.7rem;
    border-bottom: 1px solid #21262d;
    vertical-align: top;
  }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: #1c232c; }
  code {
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
  }
  td.name {
    text-align: left;
    line-height: 1.35;
    cursor: pointer;
    user-select: none;
  }
  td.name:hover strong { color: #56d364; }
  .chev {
    display: inline-block;
    width: 0.85rem;
    color: #6e7681;
    transition: transform 0.15s ease;
  }
  tr.bucket-row.expanded td.name .chev {
    transform: rotate(90deg);
    color: #56d364;
  }
  tr.benchmark-row td { background: #2e2508; }
  tr.benchmark-row:hover td { background: #372d0b; }
  tr.benchmark-row td.name { cursor: default; }
  tr.benchmark-row td.name:hover strong { color: #f0f6fc; }
  .bench-sub {
    display: block;
    color: #7d8590;
    font-size: 0.72rem;
    letter-spacing: 0.04em;
    text-transform: uppercase;
  }
  tr.detail-row td.detail-cell {
    background: #0d1117;
    padding: 0.5rem 0 1rem 2.2rem;
    border-top: 1px dashed #30363d;
  }
  table.constituents {
    width: 100%;
    margin: 0.4rem 0 0.4rem 0;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 4px;
    box-shadow: none;
    font-size: 0.85rem;
  }
  table.constituents thead th {
    background: #1c232c;
    text-align: center;
    text-transform: none;
    letter-spacing: 0;
    font-size: 0.75rem;
    padding: 0.35rem 0.55rem;
    color: #b1bac4;
  }
  table.constituents thead th.num { text-align: center; }
  table.constituents tbody td {
    padding: 0.3rem 0.55rem;
    border-bottom: 1px solid #21262d;
    vertical-align: middle;
  }
  table.constituents .ct-ticker { text-align: left; white-space: nowrap; }
  table.constituents .ct-name {
    text-align: left;
    color: #b1bac4;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    max-width: 18rem;
  }
  table.constituents tbody tr.ct-row { height: 1.8rem; cursor: pointer; }
  /* v2.4 leader/laggard tints (buckets with > 6 valid z only) */
  table.constituents tr.ct-top td { background: #10231a; }
  table.constituents tr.ct-top:hover td { background: #152c21; }
  table.constituents tr.ct-bottom td { background: #271416; }
  table.constituents tr.ct-bottom:hover td { background: #30191c; }
  table.constituents .ct-wt,
  table.constituents .ct-score,
  table.constituents .ct-mcap { text-align: right; }
  table.constituents .ct-conf { text-align: center; color: #8b949e; font-size: 0.78rem; width: 3rem; }
  table.constituents .ct-z,
  table.constituents .ct-ret {
    text-align: right;
    font-size: 0.82rem;
    width: 4.4rem;
    min-width: 4.4rem;
  }
  table.constituents .ct-wt,
  table.constituents .ct-score,
  table.constituents .ct-mcap { width: 4.4rem; }
  /* --- sortable headers: subtle wireframe chevron ------------------------ */
  th.sortable {
    cursor: pointer;
    user-select: none;
  }
  th.sortable:hover { background: #262d38; }
  th.sortable::after {
    content: "";
    display: inline-block;
    width: 5px;
    height: 5px;
    margin-left: 7px;
    border-right: 1.2px solid #4b5563;
    border-bottom: 1.2px solid #4b5563;
    transform: rotate(45deg) translate(-1px, -1px);
    opacity: 0.55;
    transition: transform 0.12s ease;
  }
  th.sortable.sorted-desc::after {
    border-color: #56d364;
    opacity: 1;
    transform: rotate(45deg) translate(-1px, -1px);
  }
  th.sortable.sorted-asc::after {
    border-color: #56d364;
    opacity: 1;
    transform: rotate(-135deg) translate(-2px, -2px);
  }
  td.sharpe {
    text-align: right;
    font-size: 0.95rem;
    width: 4.6rem;
    min-width: 4.6rem;
  }
  td.ret, th.ret-h {
    text-align: right;
    font-size: 0.95rem;
    width: 4.4rem;
    min-width: 4.4rem;
  }
  .med {
    display: block;
    font-size: 0.72rem;
    margin-top: 1px;
    opacity: 0.75;
  }
  .med span { font-weight: 500; }
  .pos { color: #56d364; font-weight: 600; }
  .neg { color: #f85149; font-weight: 600; }
  .flat { color: #8b949e; }
  .na { color: #4b5563; }
  /* --- constituent price charts ------------------------------------------ */
  tr.chart-row td.chart-cell {
    background: #0d1117;
    padding: 0.55rem 0.8rem 1rem 0.8rem;
    border-top: 1px dashed #30363d;
  }
  .chart-controls {
    display: flex;
    gap: 0.35rem;
    align-items: center;
    margin-bottom: 0.5rem;
  }
  .chart-desc {
    color: #ccff00;
    font-size: 0.95rem;
    line-height: 1.5;
    margin: 0.6rem 0 0 0;
    max-width: 68rem;
  }
  .chart-controls .chart-title {
    color: #b1bac4;
    font-size: 0.8rem;
    font-weight: 600;
    margin-right: auto;
  }
  .chart-controls button {
    background: #21262d;
    color: #8b949e;
    border: 1px solid #30363d;
    border-radius: 4px;
    font-size: 0.72rem;
    font-weight: 600;
    letter-spacing: 0.04em;
    padding: 0.15rem 0.55rem;
    cursor: pointer;
  }
  .chart-controls button:hover { color: #e6edf3; border-color: #8b949e; }
  .chart-controls button.active {
    background: #1f6feb33;
    color: #58a6ff;
    border-color: #58a6ff;
  }
  .uplot { font-family: inherit; }
  .u-legend { color: #8b949e; font-size: 0.78rem; }
  .u-legend .u-value { color: #e6edf3; font-variant-numeric: tabular-nums; }
  /* --- company search ----------------------------------------------------- */
  .search-wrap {
    position: relative;
    max-width: 430px;
    margin: 0 auto 1.3rem auto;
  }
  #company-search {
    width: 100%;
    box-sizing: border-box;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    color: #e6edf3;
    font: inherit;
    font-size: 0.95rem;
    padding: 0.45rem 0.8rem;
  }
  #company-search:focus { outline: none; border-color: #58a6ff; }
  #company-search::placeholder { color: #6e7681; }
  #search-suggestions {
    position: absolute;
    top: 100%;
    left: 0; right: 0;
    z-index: 20;
    margin: 0.2rem 0 0 0;
    padding: 0;
    list-style: none;
    background: #1c232c;
    border: 1px solid #30363d;
    border-radius: 6px;
    box-shadow: 0 6px 16px rgba(0,0,0,0.5);
    overflow: hidden;
  }
  #search-suggestions li {
    padding: 0.4rem 0.8rem;
    cursor: pointer;
    display: flex;
    gap: 0.6rem;
    align-items: baseline;
  }
  #search-suggestions li.active,
  #search-suggestions li:hover { background: #262d38; }
  #search-suggestions .sg-ticker {
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
    font-size: 0.85rem;
    color: #58a6ff;
    min-width: 3.6rem;
  }
  #search-suggestions .sg-name {
    color: #b1bac4;
    font-size: 0.85rem;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    flex: 1;
  }
  #search-suggestions .sg-tag { color: #6e7681; font-size: 0.72rem; white-space: nowrap; }
  #search-panel {
    max-width: 700px;
    margin: 0 auto 1.6rem auto;
    background: #161b22;
    border: 1px solid #30363d;
    border-radius: 6px;
    padding: 0.9rem 1.1rem 1.1rem;
    box-shadow: 0 1px 3px rgba(0,0,0,0.25);
  }
  .sp-head { display: flex; align-items: baseline; gap: 0.7rem; margin-bottom: 0.6rem; }
  .sp-ticker {
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
    font-size: 1.15rem;
    color: #f0f6fc;
    font-weight: 700;
  }
  .sp-name { color: #b1bac4; flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .sp-mcap { color: #8b949e; font-size: 0.85rem; white-space: nowrap; }
  .sp-close {
    position: absolute;
    right: 0.45rem;
    top: 50%;
    transform: translateY(-50%);
    background: none;
    border: none;
    color: #6e7681;
    font-size: 1.2rem;
    line-height: 1;
    cursor: pointer;
    padding: 0 0.2rem;
  }
  .sp-close:hover { color: #e6edf3; }
  #company-search { padding-right: 2.1rem; }
  .sp-chart { margin: 0.4rem 0 0.8rem; }
  table.sp-scores { font-size: 0.85rem; box-shadow: none; }
  table.sp-scores thead th { font-size: 0.72rem; padding: 0.35rem 0.55rem; text-transform: none; letter-spacing: 0; }
  table.sp-scores tbody td { padding: 0.35rem 0.55rem; vertical-align: middle; }
  table.sp-scores td.spc-bucket { text-align: left; }
  table.sp-scores tr.member td.spc-bucket { cursor: pointer; }
  table.sp-scores tr.member:hover td.spc-bucket { color: #56d364; }
  table.sp-scores td.spc-score, table.sp-scores td.spc-wt { text-align: right; width: 4.8rem; }
  table.sp-scores td.spc-conf { text-align: center; color: #8b949e; width: 3.5rem; font-size: 0.78rem; }
  table.sp-scores tr.subfloor td { color: #6e7681; }
  .sp-floor-tag {
    font-size: 0.68rem;
    color: #6e7681;
    border: 1px solid #30363d;
    border-radius: 3px;
    padding: 0 0.25rem;
    margin-left: 0.4rem;
  }
  .sp-note { color: #6e7681; font-size: 0.75rem; margin: 0.5rem 0 0; }
  tr.bucket-row.flash td { animation: rowflash 1.6s ease-out; }
  @keyframes rowflash { 0% { background: #1f6feb55; } 100% { background: transparent; } }
  footer {
    margin-top: 2rem;
    padding-top: 0.9rem;
    border-top: 1px solid #30363d;
    color: #6e7681;
    font-size: 0.8rem;
    line-height: 1.5;
  }
  footer b { color: #8b949e; }
  @media print {
    html, body { background: #fff; color: #1c1c1c; }
    body { margin: 0; font-size: 10pt; }
    .chart-desc { color: #555; }
    table, table.constituents {
      background: #fff; box-shadow: none; border-color: #ccc;
    }
    thead th, table.constituents thead th { background: #f2efe6; color: #444; }
    .pos { color: #146c2e; } .neg { color: #b3261e; } .flat { color: #666; }
    tr.benchmark-row td { background: #f7f2e0; }
    table.constituents tr.ct-top td { background: #eef7ee; }
    table.constituents tr.ct-bottom td { background: #fbeeee; }
    tr.detail-row td.detail-cell, tr.chart-row td.chart-cell {
      background: #faf9f2; border-top-color: #ccc;
    }
    .search-wrap, #search-panel { display: none; }
    tbody td, table.constituents tbody td { border-bottom-color: #eee; }
    footer { color: #666; border-top-color: #ccc; }
    .updated { color: #666; }
  }
"""

# Tokens replaced at render: __WINDOW_DAYS__
SCRIPT = """
  // ---- shared sort helper: sorts rows (or row-pairs) by a column ----------
  function sortRows(table, th, pairSelector) {
    var headers = Array.from(table.tHead.rows[0].cells);
    var colIdx = headers.indexOf(th);
    var type = th.dataset.type || 'str';
    var currentlyDesc = th.classList.contains('sorted-desc');
    var currentlyAsc = th.classList.contains('sorted-asc');
    var wantDesc;
    if (currentlyDesc) wantDesc = false;
    else if (currentlyAsc) wantDesc = true;
    else wantDesc = (type === 'num');
    headers.forEach(function(h) { h.classList.remove('sorted-asc', 'sorted-desc'); });
    th.classList.add(wantDesc ? 'sorted-desc' : 'sorted-asc');

    var tbody = pairSelector
      ? table.querySelector('tbody.sortable-body')
      : th.closest('table').tBodies[0];
    var units;
    if (pairSelector) {
      // Row pairs: each bucket-row travels with its detail-row (and the
      // detail row's nested state) when sorted.
      units = Array.from(tbody.querySelectorAll('tr.bucket-row')).map(function(r) {
        return {
          key: r.cells[colIdx].dataset.sort,
          rows: [r, tbody.querySelector('tr.detail-row[data-bucket="' + r.dataset.bucket + '"]')]
        };
      });
    } else {
      units = Array.from(tbody.rows).filter(function(r) {
        return !r.classList.contains('chart-row');
      }).map(function(r) {
        var chart = r.nextElementSibling;
        var rows = [r];
        if (chart && chart.classList.contains('chart-row')) rows.push(chart);
        return { key: r.cells[colIdx].dataset.sort, rows: rows };
      });
    }
    units.sort(function(a, b) {
      var av = a.key, bv = b.key;
      var aEmpty = (av === '' || av == null);
      var bEmpty = (bv === '' || bv == null);
      if (aEmpty && bEmpty) return 0;
      if (aEmpty) return 1;   // empty / NaN always last
      if (bEmpty) return -1;
      var cmp = (type === 'num') ? (parseFloat(av) - parseFloat(bv)) : av.localeCompare(bv);
      return wantDesc ? -cmp : cmp;
    });
    units.forEach(function(u) {
      u.rows.forEach(function(r) { if (r) tbody.appendChild(r); });
    });
  }

  // ---- main table: sortable Sharpe + window columns; benchmark pinned ----
  var mainTable = document.getElementById('main-table');
  mainTable.tHead.querySelectorAll('th.sortable').forEach(function(th) {
    th.addEventListener('click', function() { sortRows(mainTable, th, true); });
  });

  // ---- bucket expand/collapse --------------------------------------------
  document.querySelectorAll('tr.bucket-row td.name').forEach(function(cell) {
    cell.addEventListener('click', function() {
      var bucket = cell.dataset.bucket;
      var row = cell.closest('tr.bucket-row');
      var detail = document.querySelector('tr.detail-row[data-bucket="' + bucket + '"]');
      if (!detail) return;
      var isHidden = detail.hasAttribute('hidden');
      if (isHidden) {
        detail.removeAttribute('hidden');
        row.classList.add('expanded');
      } else {
        detail.setAttribute('hidden', '');
        row.classList.remove('expanded');
      }
    });
  });

  // ---- constituent-table sortable headers --------------------------------
  document.querySelectorAll('table.constituents th.sortable').forEach(function(th) {
    th.addEventListener('click', function(evt) {
      evt.stopPropagation();  // don't collapse the parent bucket row
      sortRows(th.closest('table.constituents'), th, false);
    });
  });

  // ---- per-constituent price charts (lazy uPlot) -------------------------
  var WINDOW_DAYS = __WINDOW_DAYS__;
  var charts = {};  // ticker -> {u: uPlot, buttons: [...]}

  function sliceData(data, days) {
    var xs = data[0], ys = data[1];
    if (!xs.length) return [xs, ys];
    var cutoff = xs[xs.length - 1] - days * 86400;
    var i = 0;
    while (i < xs.length && xs[i] < cutoff) i++;
    if (i > 0) i--;  // one bar of head-room so the window starts with context
    return [xs.slice(i), ys.slice(i)];
  }

  function buildChart(cell, ticker) {
    var data = PRICES[ticker];
    if (!data) {
      cell.textContent = 'No price history for ' + ticker;
      return;
    }
    var controls = document.createElement('div');
    controls.className = 'chart-controls';
    var title = document.createElement('span');
    title.className = 'chart-title';
    title.textContent = ticker + ' — adjusted daily close';
    controls.appendChild(title);
    var wrap = document.createElement('div');
    wrap.className = 'chart-wrap';
    cell.appendChild(controls);
    cell.appendChild(wrap);
    if (typeof DESCS !== 'undefined' && DESCS[ticker]) {
      var desc = document.createElement('p');
      desc.className = 'chart-desc';
      desc.textContent = DESCS[ticker];
      cell.appendChild(desc);
    }

    var width = Math.max(320, cell.clientWidth - 20);
    var opts = {
      width: width,
      height: 260,
      scales: { x: { time: true } },
      axes: [
        { stroke: '#8b949e', grid: { stroke: '#21262d' }, ticks: { stroke: '#30363d' } },
        { stroke: '#8b949e', grid: { stroke: '#21262d' }, ticks: { stroke: '#30363d' }, size: 60,
          values: function(u, vals) { return vals.map(function(v) { return '$' + v.toFixed(v < 10 ? 2 : 0); }); } }
      ],
      series: [
        { label: 'Date' },
        { label: ticker, stroke: '#58a6ff', width: 1.5,
          fill: 'rgba(88,166,255,0.07)', points: { show: false },
          value: function(u, v) { return v == null ? '—' : '$' + v.toFixed(2); } }
      ],
      cursor: { y: false }
    };
    var u = new uPlot(opts, sliceData(data, WINDOW_DAYS['1y']), wrap);
    charts[ticker] = u;

    Object.keys(WINDOW_DAYS).forEach(function(label) {
      var btn = document.createElement('button');
      btn.textContent = label.toUpperCase();
      if (label === '1y') btn.classList.add('active');
      btn.addEventListener('click', function(evt) {
        evt.stopPropagation();
        controls.querySelectorAll('button').forEach(function(b) { b.classList.remove('active'); });
        btn.classList.add('active');
        u.setData(sliceData(data, WINDOW_DAYS[label]));
      });
      controls.appendChild(btn);
    });
  }

  document.querySelectorAll('table.constituents tr.ct-row').forEach(function(row) {
    row.addEventListener('click', function() {
      var next = row.nextElementSibling;
      if (next && next.classList.contains('chart-row')) {
        if (next.hasAttribute('hidden')) next.removeAttribute('hidden');
        else next.setAttribute('hidden', '');
        return;
      }
      var tr = document.createElement('tr');
      tr.className = 'chart-row';
      var td = document.createElement('td');
      td.className = 'chart-cell';
      td.colSpan = row.cells.length;
      tr.appendChild(td);
      row.parentNode.insertBefore(tr, row.nextSibling);
      buildChart(td, row.dataset.ticker);
    });
  });

  // ---- company search ----------------------------------------------------
  var searchInput = document.getElementById('company-search');
  var sugList = document.getElementById('search-suggestions');
  var panel = document.getElementById('search-panel');
  var activeIdx = -1;
  var currentMatches = [];

  // Close button lives beside the search bar (not in the panel), shown only
  // while a panel is open.
  var closeBtn = document.createElement('button');
  closeBtn.className = 'sp-close';
  closeBtn.textContent = '\\u2715';
  closeBtn.setAttribute('hidden', '');
  closeBtn.setAttribute('aria-label', 'Close company panel');
  closeBtn.addEventListener('click', function() {
    panel.setAttribute('hidden', '');
    searchInput.value = '';
    closeBtn.setAttribute('hidden', '');
  });
  document.querySelector('.search-wrap').appendChild(closeBtn);

  function fmtMcap(v) {
    if (v == null) return '';
    if (v >= 1e12) return '$' + (v / 1e12).toFixed(2) + 'T';
    if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return '$' + (v / 1e6).toFixed(1) + 'M';
    return '$' + Math.round(v);
  }

  function searchMatches(q) {
    q = q.trim().toUpperCase();
    if (!q) return [];
    var pref = [], other = [];
    SEARCH.companies.forEach(function(c) {
      if (c.t.indexOf(q) === 0) pref.push(c);
      else if (c.t.indexOf(q) !== -1 || (c.n || '').toUpperCase().indexOf(q) !== -1) other.push(c);
    });
    return pref.concat(other).slice(0, 8);
  }

  function renderSuggestions() {
    sugList.textContent = '';
    if (!currentMatches.length) { sugList.setAttribute('hidden', ''); return; }
    currentMatches.forEach(function(c, i) {
      var li = document.createElement('li');
      if (i === activeIdx) li.classList.add('active');
      var tk = document.createElement('span'); tk.className = 'sg-ticker'; tk.textContent = c.t;
      var nm = document.createElement('span'); nm.className = 'sg-name'; nm.textContent = c.n || '';
      li.appendChild(tk); li.appendChild(nm);
      var isMember = c.s.some(function(s) { return s[3] != null; });
      if (!isMember) {
        var tag = document.createElement('span'); tag.className = 'sg-tag'; tag.textContent = 'not in any index';
        li.appendChild(tag);
      }
      // mousedown, not click: fires before the input's blur/click-away
      li.addEventListener('mousedown', function(evt) { evt.preventDefault(); selectCompany(c); });
      sugList.appendChild(li);
    });
    sugList.removeAttribute('hidden');
  }

  function closeSuggestions() { sugList.setAttribute('hidden', ''); activeIdx = -1; }

  function gotoBucket(bucketId) {
    var row = document.querySelector('tr.bucket-row[data-bucket="' + bucketId + '"]');
    var detail = document.querySelector('tr.detail-row[data-bucket="' + bucketId + '"]');
    if (!row) return;
    if (detail && detail.hasAttribute('hidden')) row.querySelector('td.name').click();
    row.scrollIntoView({ behavior: 'smooth', block: 'center' });
    row.classList.remove('flash');
    void row.offsetWidth;  // restart the CSS animation
    row.classList.add('flash');
  }

  function selectCompany(c) {
    closeSuggestions();
    searchInput.value = c.t + (c.n ? ' — ' + c.n : '');
    panel.textContent = '';
    var head = document.createElement('div'); head.className = 'sp-head';
    var tk = document.createElement('span'); tk.className = 'sp-ticker'; tk.textContent = c.t;
    var nm = document.createElement('span'); nm.className = 'sp-name'; nm.textContent = c.n || '';
    var mc = document.createElement('span'); mc.className = 'sp-mcap'; mc.textContent = fmtMcap(c.m);
    head.appendChild(tk); head.appendChild(nm); head.appendChild(mc);
    panel.appendChild(head);
    closeBtn.removeAttribute('hidden');

    if (PRICES[c.t]) {
      var cd = document.createElement('div'); cd.className = 'sp-chart';
      panel.appendChild(cd);
      panel.removeAttribute('hidden');  // must be visible for clientWidth
      buildChart(cd, c.t);  // buildChart appends the description under the chart
    } else if (typeof DESCS !== 'undefined' && DESCS[c.t]) {
      var pd = document.createElement('p'); pd.className = 'chart-desc';
      pd.textContent = DESCS[c.t];
      panel.appendChild(pd);
    }

    var tbl = document.createElement('table'); tbl.className = 'sp-scores';
    var thead = document.createElement('thead');
    thead.innerHTML = '<tr><th style="text-align:left">Sub Index</th>' +
                      '<th>Score</th><th>Conf</th><th>Index Weight</th></tr>';
    tbl.appendChild(thead);
    var tb = document.createElement('tbody');
    var rows = c.s.filter(function(s) { return s[1] > 0; })
                  .sort(function(a, b) { return b[1] - a[1]; });
    rows.forEach(function(s) {
      var tr = document.createElement('tr');
      var member = s[3] != null;
      tr.className = member ? 'member' : 'subfloor';
      var b = document.createElement('td'); b.className = 'spc-bucket';
      b.textContent = SEARCH.buckets[s[0]] || s[0];
      if (!member) {
        var tag = document.createElement('span'); tag.className = 'sp-floor-tag'; tag.textContent = 'below floor';
        b.appendChild(tag);
      }
      var sc = document.createElement('td'); sc.className = 'spc-score'; sc.textContent = s[1].toFixed(2);
      var cf = document.createElement('td'); cf.className = 'spc-conf'; cf.textContent = s[2] || '';
      var wt = document.createElement('td'); wt.className = 'spc-wt'; wt.textContent = member ? s[3].toFixed(2) + '%' : '\\u2014';
      tr.appendChild(b); tr.appendChild(sc); tr.appendChild(cf); tr.appendChild(wt);
      tr.addEventListener('click', function() { gotoBucket(s[0]); });
      tb.appendChild(tr);
    });
    tbl.appendChild(tb);
    if (rows.length) panel.appendChild(tbl);
    var note = document.createElement('p'); note.className = 'sp-note';
    note.textContent = (rows.length
        ? 'Nonzero LLM scores shown (no floor; zero scores omitted). Click a sub index to open it in the table below.'
        : "All of this company's sub-index scores are 0.")
      + (PRICES[c.t] ? '' : ' No price chart: not an index member, so prices are not tracked.');
    panel.appendChild(note);
    panel.removeAttribute('hidden');
  }

  searchInput.addEventListener('input', function() {
    currentMatches = searchMatches(searchInput.value);
    activeIdx = -1;
    renderSuggestions();
  });
  searchInput.addEventListener('keydown', function(evt) {
    if (sugList.hasAttribute('hidden')) return;
    if (evt.key === 'ArrowDown') {
      evt.preventDefault();
      activeIdx = Math.min(activeIdx + 1, currentMatches.length - 1);
      renderSuggestions();
    } else if (evt.key === 'ArrowUp') {
      evt.preventDefault();
      activeIdx = Math.max(activeIdx - 1, 0);
      renderSuggestions();
    } else if (evt.key === 'Enter') {
      evt.preventDefault();
      if (activeIdx >= 0) selectCompany(currentMatches[activeIdx]);
      else if (currentMatches.length) selectCompany(currentMatches[0]);
    } else if (evt.key === 'Escape') {
      closeSuggestions();
    }
  });
  document.addEventListener('click', function(evt) {
    if (!evt.target.closest('.search-wrap')) closeSuggestions();
  });
"""


def render_html(
    returns: pd.DataFrame,
    weights: pd.DataFrame,
    weight_col: str,
    asof: str,
    company_names: dict[str, str],
    ticker_returns: dict[str, dict[str, float | None]],
    medians: dict[str, dict[str, float | None]],
    bucket_sharpe: dict[str, float | None],
    const_sharpe: dict[str, float | None],
    benchmark: dict,
    sharpe_label: str,
    member_z: dict[str, dict[str, float | None]],
    z_label: str,
    chart_json: str,
    search_json: str,
    descs_json: str,
    uplot_js: str,
    uplot_css: str,
) -> str:
    """Stylish standalone HTML with sortable tables, benchmark row and charts."""
    # Default sort: 1y descending, NaN last (client-side sorting can re-order)
    returns = returns.sort_values("1y", ascending=False, na_position="last").reset_index(drop=True)

    def _fmt_day(ts: pd.Timestamp) -> str:
        return f"{ts.day} {ts.strftime('%b %Y')}"  # e.g. "29 Jul 2026" (no zero-pad)

    now_utc = datetime.now(timezone.utc)
    generated_str = f"{_fmt_day(pd.Timestamp(now_utc))} {now_utc.strftime('%H:%M')} UTC"
    try:
        asof_str = _fmt_day(pd.Timestamp(asof))
    except (ValueError, TypeError):
        asof_str = asof  # fall back to the raw value if unparseable
    updated_line = (
        f'<p class="updated">Updated {escape(generated_str)}'
        f' · prices through {escape(asof_str)}'
        f' · <a href="architecture.html">Architecture</a>'
        f' · <a href="coherence.html">Coherence</a></p>'
    )

    def _fmt_mcap(v) -> str:
        if pd.isna(v):
            return "—"
        v = float(v)
        if v >= 1e12:
            return f"${v/1e12:.2f}T"
        if v >= 1e9:
            return f"${v/1e9:.2f}B"
        if v >= 1e6:
            return f"${v/1e6:.1f}M"
        return f"${v:,.0f}"

    def _constituent_table(bucket_id: str) -> str:
        bucket_z = member_z.get(bucket_id, {})
        # Leader/laggard tinting: only when more than 6 members hold a
        # valid z (top-3 / bottom-3 can then never overlap).
        valid_z = {t: v for t, v in bucket_z.items() if v is not None}
        top3: set[str] = set()
        bottom3: set[str] = set()
        if len(valid_z) > 6:
            ranked = sorted(valid_z, key=valid_z.get, reverse=True)
            top3, bottom3 = set(ranked[:3]), set(ranked[-3:])
        members = weights[weights["bucket_id"] == bucket_id].copy()
        # Default sort: weight descending, NaN last. Click column headers
        # in the browser to re-sort by any other field.
        members["_wt_sort"] = pd.to_numeric(members[weight_col], errors="coerce")
        members = members.sort_values(
            "_wt_sort", ascending=False, na_position="last"
        )
        rows = []
        for _, m in members.iterrows():
            ticker = str(m["ticker"]).upper()
            full_name = company_names.get(ticker, "")
            weight_pct = float(m[weight_col]) * 100
            score = float(m["score"])
            mcap_raw = m.get("market_cap")
            mcap_num = "" if pd.isna(mcap_raw) else f"{float(mcap_raw):.0f}"
            mcap = _fmt_mcap(mcap_raw)
            conf = escape(str(m.get("confidence", "")))
            conf_rank = {"high": 3, "medium": 2, "low": 1}.get(str(m.get("confidence", "")).lower(), 0)
            z_v = bucket_z.get(ticker)
            tint = " ct-top" if ticker in top3 else (" ct-bottom" if ticker in bottom3 else "")
            rets = ticker_returns.get(ticker, {})
            ret_cells = "".join(
                f'<td class="ct-ret" data-sort="{_sort_attr(rets.get(label))}">{_fmt_pct(rets.get(label))}</td>'
                for label, _ in WINDOWS
            )
            rows.append(
                f'<tr class="ct-row{tint}" data-ticker="{escape(ticker)}">'
                f'<td class="ct-ticker" data-sort="{escape(ticker)}"><code>{escape(ticker)}</code></td>'
                f'<td class="ct-name" data-sort="{escape((full_name or ticker).lower())}">{escape(full_name) if full_name else "<em>—</em>"}</td>'
                f'<td class="ct-wt" data-sort="{weight_pct:.6f}">{weight_pct:.2f}%</td>'
                f'<td class="ct-score" data-sort="{score:.4f}">{score:.2f}</td>'
                f'<td class="ct-mcap" data-sort="{mcap_num}">{mcap}</td>'
                f'<td class="ct-conf" data-sort="{conf_rank}">{conf}</td>'
                f'<td class="ct-z" data-sort="{_sort_attr(z_v)}">{_fmt_z(z_v)}</td>'
                f'{ret_cells}'
                f'</tr>'
            )
        window_headers = "".join(
            f'<th class="num sortable" data-type="num">{label.upper()}</th>'
            for label, _ in WINDOWS
        )
        return (
            '<table class="constituents">'
            '<thead><tr>'
            '<th class="sortable" data-type="str">Ticker</th>'
            '<th class="sortable" data-type="str">Company</th>'
            '<th class="num sortable sorted-desc" data-type="num">Index Weight</th>'
            '<th class="num sortable" data-type="num">Score</th>'
            '<th class="num sortable" data-type="num">Market cap</th>'
            '<th class="sortable" data-type="num">Conf</th>'
            f'<th class="num sortable" data-type="num">{escape(z_label)}</th>'
            f'{window_headers}'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )

    n_cols = 2 + len(WINDOWS)  # name + sharpe + windows

    # Pinned benchmark row (never sorted, no expansion, no median line)
    bench_cells = [
        f'<td class="name"><strong>{escape(benchmark["ticker"])}</strong>'
        f'<span class="bench-sub">{escape(benchmark["label"])}</span></td>',
        f'<td class="sharpe">{_fmt_sharpe(benchmark["sharpe"])}</td>',
    ]
    for label, _ in WINDOWS:
        bench_cells.append(f'<td class="ret">{_fmt_pct(benchmark["returns"].get(label))}</td>')
    bench_row = '<tr class="benchmark-row">' + "".join(bench_cells) + "</tr>"

    rows_html = []
    for _, r in returns.iterrows():
        bucket_id = r["bucket_id"]
        b_sharpe = bucket_sharpe.get(bucket_id)
        b_medians = medians.get(bucket_id, {})
        cells = [
            f'<td class="name" data-bucket="{escape(bucket_id)}">'
            f'<span class="chev">▸</span> '
            f'<strong>{escape(r["bucket_name"])}</strong></td>',
            f'<td class="sharpe" data-sort="{_sort_attr(b_sharpe)}">{_fmt_sharpe(b_sharpe)}</td>',
        ]
        for label, _ in WINDOWS:
            cells.append(
                f'<td class="ret" data-sort="{_sort_attr(r[label])}">{_fmt_pct(r[label])}'
                f'<span class="med">{_fmt_pct(b_medians.get(label))}</span></td>'
            )
        rows_html.append(
            f'<tr class="bucket-row" data-bucket="{escape(bucket_id)}">'
            + "".join(cells) + "</tr>"
        )
        rows_html.append(
            f'<tr class="detail-row" data-bucket="{escape(bucket_id)}" hidden>'
            f'<td colspan="{n_cols}" class="detail-cell">{_constituent_table(bucket_id)}</td>'
            f'</tr>'
        )

    window_days_json = json.dumps({label: days for label, days in WINDOWS})
    script = SCRIPT.replace("__WINDOW_DAYS__", window_days_json)
    weight_blurb = WEIGHT_COL_BLURB.get(weight_col, weight_col)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pantheon — {escape(asof)}</title>
<style>{uplot_css}</style>
<style>{CSS}</style>
</head>
<body>

<h1>Pantheon</h1>
{updated_line}

<div class="search-wrap">
  <input id="company-search" type="text" placeholder="Search a company…"
         autocomplete="off" spellcheck="false">
  <ul id="search-suggestions" hidden></ul>
</div>
<div id="search-panel" hidden></div>

<table id="main-table">
  <thead>
    <tr>
      <th class="left">Sub Index</th>
      <th class="sortable" data-type="num">{escape(sharpe_label)}</th>
      {"".join(f'<th class="ret-h sortable{" sorted-desc" if label == "1y" else ""}" data-type="num">{label.upper()}</th>' for label, _ in WINDOWS)}
    </tr>
  </thead>
  <tbody>
    {bench_row}
  </tbody>
  <tbody class="sortable-body">
    {chr(10).join(rows_html)}
  </tbody>
</table>

<footer>
  <b>Click a sub index</b> to expand its constituents; <b>click a constituent</b>
  to open its price chart (1Y–1W buttons set the visible window).
  <b>Search</b> any scored company above — the panel shows all its LLM scores
  (floor ignored) and, for index members, its price chart.<br>
  <b>{escape(benchmark["ticker"])}</b> ({escape(benchmark["label"])}) is pinned
  to the top row and excluded from sorting. Click the Sharpe or return column
  headers to sort the sub indices.<br>
  <b>{escape(sharpe_label)}</b> (indices only): annualised Sharpe ratio — mean ÷ std
  (ddof 1) of daily returns × √252, risk-free rate 0 — over the trailing
  {escape(sharpe_label.split(" ")[0].lower())} window (configurable in
  <code>config.json</code>), using the daily weighted return series with
  weights renormalised each day.<br>
  <b>Small figures</b> under each index return: the median constituent's return
  for that window (the median stock can differ between windows).<br>
  <b>{escape(z_label)}:</b> intra-bucket out/underperformance — each member's
  abnormal return vs its own sub-index (recomputed <i>without</i> the member,
  beta-adjusted) over the trailing
  {escape(z_label.split(" ")[0].lower())} window, expressed as a robust
  (median/MAD) z-score across the bucket. In buckets with more than 6 scored
  members, the top-3 rows are tinted green and the bottom-3 red. A high z reads
  as <i>leader</i> or <i>stretched</i> depending on your prior — it is
  descriptive, not a signal.<br>
  <b>Weights:</b> <code>{escape(weight_col)}</code> ({escape(weight_blurb)}),
  renormalised per window across members that priced.
  <b>Penny-stock filter:</b> tickers with a window-start price below $1
  are excluded from that window (dashes in the constituent table).<br>
  Prices: Polygon.io adjusted daily closes. Returns are simple total returns.
  Charts: <a href="https://github.com/leeoniya/uPlot" style="color:#8b949e">uPlot</a> (MIT, inlined).<br>
  Generated by <code>scripts/bucket_returns.py</code> · <code>scripts/pull_prices.py</code>
</footer>

<script>{uplot_js}</script>
<script>var PRICES = {chart_json};</script>
<script>var SEARCH = {search_json};</script>
<script>var DESCS = {descs_json};</script>
<script>{script}</script>

</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="definitions/minidex_weights.csv")
    parser.add_argument("--prices", default="data/prices.csv")
    parser.add_argument("--db", default="data/minidex.db",
                        help="SQLite DB path for enriching constituents with company names")
    parser.add_argument("--weight-col", default=None,
                        choices=["weight_score", "weight_cap_score", "weight_cap_score_aug", "weight_equal"],
                        help="Weighting method (default: report.weight_col in config.json)")
    parser.add_argument("--min-start-price", type=float, default=1.0,
                        help="Exclude tickers with per-window starting price below this "
                             "(default $1.00, filters penny-stock recovery distortions)")
    parser.add_argument("--out", default=None,
                        help="Output HTML path (defaults to outputs/bucket_returns.html)")
    args = parser.parse_args()

    cfg = report_metrics.load_report_config()
    weight_col = args.weight_col or cfg["weight_col"]
    weight_cap_m = float(cfg["weight_cap_m"])
    sharpe_window = cfg["sharpe_window"]
    benchmark_ticker = str(cfg["benchmark_ticker"]).upper()
    sharpe_label = f"{sharpe_window.upper()} Sharpe"

    weights_path = REPO_ROOT / args.weights if not Path(args.weights).is_absolute() else Path(args.weights)
    prices_path = REPO_ROOT / args.prices if not Path(args.prices).is_absolute() else Path(args.prices)
    db_path = REPO_ROOT / args.db if not Path(args.db).is_absolute() else Path(args.db)
    out_path = (REPO_ROOT / args.out) if args.out else (REPO_ROOT / "outputs" / "bucket_returns.html")

    uplot_js_path = VENDOR_DIR / "uPlot.iife.min.js"
    uplot_css_path = VENDOR_DIR / "uPlot.min.css"
    if not uplot_js_path.exists() or not uplot_css_path.exists():
        sys.exit(f"bucket_returns: vendored uPlot missing under {VENDOR_DIR} — "
                 f"see assets/vendor/VERSION.md")
    uplot_js = uplot_js_path.read_text(encoding="utf-8")
    uplot_css = uplot_css_path.read_text(encoding="utf-8")

    weights = pd.read_csv(weights_path)
    weights["ticker"] = weights["ticker"].astype(str).str.upper()
    if weight_col == "weight_cap_score_aug":
        # Derived at render time from weight_cap_score — frozen CSV untouched.
        weights[weight_col] = weights.groupby("bucket_id")["weight_cap_score"].transform(
            lambda s: report_metrics.capped_weights(s, weight_cap_m)
        )
    prices = pd.read_csv(prices_path)
    prices["ticker"] = prices["ticker"].astype(str).str.upper()
    company_names = _load_company_names(db_path)

    print(f"bucket_returns: {len(weights):,} weight rows, {len(prices):,} price rows, "
          f"{len(company_names):,} company names, weight_col={weight_col}, "
          f"sharpe_window={sharpe_window}, benchmark={benchmark_ticker}")

    tickers = sorted(set(weights["ticker"]))
    all_tickers = sorted(set(tickers) | {benchmark_ticker})
    ticker_returns = compute_ticker_returns(all_tickers, prices, min_start_price=args.min_start_price)
    returns = compute_bucket_returns(weights, ticker_returns, weight_col)
    medians = report_metrics.median_constituent_returns(weights, ticker_returns)

    window_days = report_metrics.WINDOW_DAYS[sharpe_window]
    daily = report_metrics.daily_returns(prices, all_tickers, window_days,
                                         min_start_price=args.min_start_price)
    const_sharpe = {t: report_metrics.sharpe(daily[t].dropna()) for t in daily.columns}
    bucket_series = report_metrics.bucket_daily_series(weights, daily, weight_col,
                                                       weight_cap_m=weight_cap_m)
    bucket_sharpe = {b: report_metrics.sharpe(bucket_series[b].dropna())
                     for b in bucket_series.columns}

    # v2.4: intra-bucket residual z (LOO-beta-adjusted, robust cross-section)
    z_window = cfg["z_window"]
    z_label = f"{z_window.upper()} Z of Sub Idx Res"
    if z_window == sharpe_window:
        daily_z = daily
    else:
        daily_z = report_metrics.daily_returns(
            prices, all_tickers, report_metrics.WINDOW_DAYS[z_window],
            min_start_price=args.min_start_price)
    member_z = report_metrics.residual_car_z(weights, daily_z, weight_col,
                                             weight_cap_m=weight_cap_m)

    benchmark = {
        "ticker": benchmark_ticker,
        "label": "Nasdaq-100 benchmark" if benchmark_ticker == "QQQ" else "Benchmark",
        "returns": ticker_returns.get(benchmark_ticker, {}),
        "sharpe": const_sharpe.get(benchmark_ticker),
    }

    # Chart payload: full daily history per ticker as [epoch-seconds], [closes]
    chart_prices = prices.copy()
    chart_prices["date"] = pd.to_datetime(chart_prices["date"])
    chart_prices = chart_prices.sort_values(["ticker", "date"])
    chart_data: dict[str, list] = {}
    for t, g in chart_prices.groupby("ticker"):
        # Cast to second-precision before taking the int64 view: pandas may
        # parse CSV dates as datetime64[us] (or [s]), so a blind // 10**9
        # of the raw int64 silently yields garbage epochs.
        ts = g["date"].astype("datetime64[s]").astype("int64").tolist()
        px = [round(float(c), 4) for c in g["close"]]
        chart_data[t] = [ts, px]
    # Harden against script-block breakout: json.dumps doesn't escape "</",
    # so a hostile ticker string could otherwise close the <script> tag.
    chart_json = json.dumps(chart_data, separators=(",", ":")).replace("</", "<\\/")

    # Search payload: all LLM scores per company (no floor), for the panel.
    try:
        score_floor = float(json.loads((REPO_ROOT / "config.json").read_text())
                            .get("score_floor", 0.10))
    except (OSError, ValueError):
        score_floor = 0.10
    search_json = _load_search_data(db_path, weights, weight_col, score_floor,
                                    company_names)

    descs = _load_descriptions(REPO_ROOT / "definitions" / "company_descriptions.yaml")
    undescribed = sorted(set(weights["ticker"]) - set(descs))
    if descs and undescribed:
        print(f"bucket_returns: warn — {len(undescribed)} index member(s) have no "
              f"entry in company_descriptions.yaml: {', '.join(undescribed[:8])}"
              f"{'…' if len(undescribed) > 8 else ''}", file=sys.stderr)
    descs_json = json.dumps(descs, separators=(",", ":")).replace("</", "<\\/")

    asof = str(prices["date"].max())  # latest price bar = the report's true as-of date
    html = render_html(returns, weights, weight_col, asof, company_names,
                       ticker_returns, medians, bucket_sharpe, const_sharpe,
                       benchmark, sharpe_label, member_z, z_label,
                       chart_json, search_json, descs_json, uplot_js, uplot_css)
    out_path.write_text(html, encoding="utf-8")
    print(f"bucket_returns: wrote {out_path} ({len(returns)} buckets, "
          f"{len(html)/1e6:.2f} MB)")

    # Publish the architecture page alongside the report so the header link
    # works both locally and on the droplet (which serves the whole dir).
    # The doc's back-link is href="./" (correct at the droplet site root,
    # where the report is index.html); rewrite it for this file:// copy.
    arch_src = REPO_ROOT / "docs" / "ARCHITECTURE.html"
    if arch_src.exists():
        arch_out = out_path.parent / "architecture.html"
        arch_html = arch_src.read_text(encoding="utf-8")
        if 'href="./"' not in arch_html:
            print("bucket_returns: warn — architecture page has no "
                  'href="./" back-link to rewrite; local copy will lack '
                  "a working link back to the report")
        arch_html = arch_html.replace('href="./"', f'href="{out_path.name}"')
        arch_out.write_text(arch_html, encoding="utf-8")
        print(f"bucket_returns: copied architecture page to {arch_out}")

    # Also print a small table to stdout
    display_df = returns.copy()
    display_df["sharpe"] = display_df["bucket_id"].map(
        lambda b: f"{bucket_sharpe[b]:.2f}" if bucket_sharpe.get(b) is not None else "—"
    )
    for label, _ in WINDOWS:
        display_df[label] = display_df[label].apply(
            lambda x: f"{x*100:+.2f}%" if pd.notna(x) else "—"
        )
    display_df = display_df.sort_values("1y", ascending=False, key=lambda s: s.map(
        lambda v: float(v.rstrip("%")) if isinstance(v, str) and v != "—" else -1e9))
    bench_ret = benchmark["returns"]
    bench_strs = [f"{bench_ret[l]*100:+.2f}%" if bench_ret.get(l) is not None else "—"
                  for l, _ in WINDOWS]
    bench_sharpe_str = f"{benchmark['sharpe']:.2f}" if benchmark["sharpe"] is not None else "—"
    print(f"\n{benchmark_ticker} (benchmark): sharpe={bench_sharpe_str} "
          + " ".join(f"{l}={s}" for (l, _), s in zip(WINDOWS, bench_strs)))
    print()
    print(display_df[["bucket_name", "n_members", "sharpe", "1w", "1m", "3m", "6m", "1y"]].to_string(index=False))


if __name__ == "__main__":
    main()
