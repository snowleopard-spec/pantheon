"""Compute weighted returns per bucket over five trailing windows and render
a stylish HTML report.

Windows: 1w, 1m, 3m, 6m, 1y (calendar-day lookbacks, snapped to the nearest
prior trading day per ticker). Bucket return = sum over members of
(weight_score × ticker_return), renormalising weight_score across members
that have valid prices for that window.

Usage:
    uv run python scripts/bucket_returns.py [--weights outputs/<asof>/minidex_weights.csv]
                                            [--prices data/prices.csv]
                                            [--weight-col weight_score]
                                            [--out outputs/<asof>/bucket_returns.html]
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

WINDOWS = [
    ("1w", 7),
    ("1m", 30),
    ("3m", 90),
    ("6m", 180),
    ("1y", 365),
]


def _price_at_or_before(prices: pd.DataFrame, ticker: str, target: date) -> float | None:
    sub = prices.loc[(prices["ticker"] == ticker) & (prices["date"] <= pd.Timestamp(target))]
    if sub.empty:
        return None
    return float(sub.iloc[-1]["close"])


def compute_bucket_returns(
    weights: pd.DataFrame, prices: pd.DataFrame, weight_col: str
) -> pd.DataFrame:
    """Per bucket, per window: sum(w_i × return_i) with per-window renormalisation."""
    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    prices = prices.sort_values(["ticker", "date"]).reset_index(drop=True)

    # Latest close per ticker (asof today or most recent trading day)
    latest = prices.groupby("ticker").tail(1).set_index("ticker")["close"].to_dict()

    today = date.today()
    ref = pd.Timestamp(today)
    # Snap 'end' to the latest observed price date so weekends/holidays don't NaN-out.
    end_date = min(ref, prices["date"].max())
    end_prices = {t: _price_at_or_before(prices, t, end_date.date()) for t in weights["ticker"].unique()}

    out_rows = []
    for bucket_id, g in weights.groupby("bucket_id", sort=False):
        bucket_name = g["bucket_name"].iloc[0]
        n_members = len(g)
        row = {"bucket_id": bucket_id, "bucket_name": bucket_name, "n_members": n_members}
        for label, days in WINDOWS:
            target = end_date.date() - timedelta(days=days)
            member_returns = []
            member_weights = []
            for _, m in g.iterrows():
                t = m["ticker"]
                w = float(m[weight_col])
                p_end = end_prices.get(t)
                p_start = _price_at_or_before(prices, t, target)
                if p_end is None or p_start is None or p_start == 0:
                    continue
                r = (p_end / p_start) - 1.0
                member_returns.append(r)
                member_weights.append(w)
            if not member_weights:
                row[label] = np.nan
                row[f"{label}_n"] = 0
                continue
            wsum = sum(member_weights)
            if wsum == 0:
                row[label] = np.nan
                row[f"{label}_n"] = len(member_weights)
                continue
            norm_weights = [w / wsum for w in member_weights]
            row[label] = sum(w * r for w, r in zip(norm_weights, member_returns))
            row[f"{label}_n"] = len(member_weights)
        out_rows.append(row)

    return pd.DataFrame(out_rows)


def _fmt_pct(x: float | None) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return '<span class="na">—</span>'
    pct = x * 100
    cls = "pos" if pct > 0.05 else ("neg" if pct < -0.05 else "flat")
    sign = "+" if pct > 0 else ""
    return f'<span class="{cls}">{sign}{pct:.2f}%</span>'


def render_html(returns: pd.DataFrame, weight_col: str, asof: str) -> str:
    """Stylish standalone HTML."""
    # Sort by 1y descending, NaN last
    returns = returns.sort_values("1y", ascending=False, na_position="last").reset_index(drop=True)

    rows_html = []
    for _, r in returns.iterrows():
        cells = [
            f'<td class="name"><strong>{escape(r["bucket_name"])}</strong>'
            f'<br><code>{escape(r["bucket_id"])}</code></td>',
            f'<td class="n">{int(r["n_members"])}</td>',
        ]
        for label, _ in WINDOWS:
            cells.append(f'<td class="ret">{_fmt_pct(r[label])}<br>'
                         f'<span class="nsmall">n={int(r[f"{label}_n"])}</span></td>')
        rows_html.append("<tr>" + "".join(cells) + "</tr>")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Bucket returns — {escape(asof)}</title>
<style>
  html {{ -webkit-text-size-adjust: 100%; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    color: #1c1c1c;
    background: #f7f6f2;
    max-width: 1100px;
    margin: 2rem auto;
    padding: 0 1.4rem 3rem;
    line-height: 1.5;
    font-size: 15px;
  }}
  h1 {{
    font-size: 1.7rem;
    letter-spacing: -0.01em;
    margin-bottom: 0.2rem;
    color: #202020;
  }}
  .subtitle {{
    color: #6b6b6b;
    margin: 0 0 1.6rem 0;
    font-size: 0.95rem;
  }}
  .metabar {{
    background: #fff;
    border: 1px solid #e2ded4;
    border-radius: 6px;
    padding: 0.75rem 1rem;
    margin-bottom: 1.6rem;
    display: flex;
    flex-wrap: wrap;
    gap: 1.5rem;
    font-size: 0.9rem;
    color: #444;
  }}
  .metabar b {{ color: #1c1c1c; }}
  table {{
    width: 100%;
    background: #fff;
    border-collapse: collapse;
    border: 1px solid #e2ded4;
    border-radius: 6px;
    overflow: hidden;
    box-shadow: 0 1px 3px rgba(0,0,0,0.04);
    font-variant-numeric: tabular-nums;
  }}
  thead th {{
    background: #f2efe6;
    padding: 0.65rem 0.7rem;
    text-align: right;
    font-weight: 600;
    font-size: 0.85rem;
    color: #444;
    border-bottom: 1px solid #e2ded4;
    letter-spacing: 0.02em;
    text-transform: uppercase;
  }}
  thead th.left {{ text-align: left; }}
  tbody td {{
    padding: 0.65rem 0.7rem;
    border-bottom: 1px solid #efece3;
    vertical-align: top;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover td {{ background: #fafaf3; }}
  td.name {{
    text-align: left;
    line-height: 1.35;
  }}
  td.name code {{
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
    font-size: 0.75rem;
    color: #777;
    background: transparent;
  }}
  td.n {{
    text-align: right;
    color: #666;
    font-size: 0.85rem;
    padding-top: 0.9rem;
  }}
  td.ret {{
    text-align: right;
    font-size: 0.95rem;
  }}
  .pos {{ color: #146c2e; font-weight: 600; }}
  .neg {{ color: #b3261e; font-weight: 600; }}
  .flat {{ color: #666; }}
  .na {{ color: #bbb; }}
  .nsmall {{
    color: #999;
    font-size: 0.72rem;
    font-variant-numeric: tabular-nums;
  }}
  footer {{
    margin-top: 2rem;
    padding-top: 0.9rem;
    border-top: 1px solid #e2ded4;
    color: #888;
    font-size: 0.8rem;
    line-height: 1.5;
  }}
  @media print {{
    body {{ margin: 0; background: #fff; font-size: 10pt; }}
    table {{ box-shadow: none; }}
  }}
</style>
</head>
<body>

<h1>mini-dex bucket returns</h1>
<p class="subtitle">Trailing-window returns for each of the 22 thematic sub-indices,
weighted by <code>{escape(weight_col)}</code>. Sorted by 1-year return.</p>

<div class="metabar">
  <span><b>As of:</b> {escape(asof)}</span>
  <span><b>Weight scheme:</b> <code>{escape(weight_col)}</code></span>
  <span><b>Buckets:</b> {len(returns)}</span>
  <span><b>Windows:</b> 1w / 1m / 3m / 6m / 1y</span>
</div>

<table>
  <thead>
    <tr>
      <th class="left">Bucket</th>
      <th>n</th>
      <th>1W</th>
      <th>1M</th>
      <th>3M</th>
      <th>6M</th>
      <th>1Y</th>
    </tr>
  </thead>
  <tbody>
    {chr(10).join(rows_html)}
  </tbody>
</table>

<footer>
  <b>n</b> is the number of bucket members with a valid price for that window.
  Weights are renormalised per window across members that priced.<br>
  Prices: Polygon.io adjusted daily closes. Returns are simple total returns from adjusted close to adjusted close.<br>
  Generated by <code>scripts/bucket_returns.py</code> · <code>scripts/pull_prices.py</code>
</footer>

</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="outputs/2026-07-25/minidex_weights.csv")
    parser.add_argument("--prices", default="data/prices.csv")
    parser.add_argument("--weight-col", default="weight_score",
                        choices=["weight_score", "weight_cap_score", "weight_equal"])
    parser.add_argument("--out", default=None,
                        help="Output HTML path (defaults to <weights-dir>/bucket_returns.html)")
    args = parser.parse_args()

    weights_path = REPO_ROOT / args.weights if not Path(args.weights).is_absolute() else Path(args.weights)
    prices_path = REPO_ROOT / args.prices if not Path(args.prices).is_absolute() else Path(args.prices)
    out_path = (REPO_ROOT / args.out) if args.out else (weights_path.parent / "bucket_returns.html")

    weights = pd.read_csv(weights_path)
    weights["ticker"] = weights["ticker"].astype(str).str.upper()
    prices = pd.read_csv(prices_path)
    prices["ticker"] = prices["ticker"].astype(str).str.upper()

    print(f"bucket_returns: {len(weights):,} weight rows, {len(prices):,} price rows, "
          f"weight_col={args.weight_col}")

    returns = compute_bucket_returns(weights, prices, args.weight_col)
    asof = weights_path.parent.name
    html = render_html(returns, args.weight_col, asof)
    out_path.write_text(html, encoding="utf-8")
    print(f"bucket_returns: wrote {out_path} ({len(returns)} buckets)")

    # Also print a small table to stdout
    display_df = returns.copy()
    for label, _ in WINDOWS:
        display_df[label] = display_df[label].apply(
            lambda x: f"{x*100:+.2f}%" if pd.notna(x) else "—"
        )
    display_df = display_df.sort_values("1y", ascending=False, key=lambda s: s.map(
        lambda v: float(v.rstrip("%")) if isinstance(v, str) and v != "—" else -1e9))
    print()
    print(display_df[["bucket_name", "n_members", "1w", "1m", "3m", "6m", "1y"]].to_string(index=False))


if __name__ == "__main__":
    main()
