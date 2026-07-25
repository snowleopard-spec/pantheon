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
import sqlite3
from datetime import date, timedelta
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent

# Display order: longest window first
WINDOWS = [
    ("1y", 365),
    ("6m", 180),
    ("3m", 90),
    ("1m", 30),
    ("1w", 7),
]


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


def render_html(
    returns: pd.DataFrame,
    weights: pd.DataFrame,
    weight_col: str,
    asof: str,
    company_names: dict[str, str],
    ticker_returns: dict[str, dict[str, float | None]],
) -> str:
    """Stylish standalone HTML with expandable per-bucket constituent tables."""
    # Sort by 1y descending, NaN last
    returns = returns.sort_values("1y", ascending=False, na_position="last").reset_index(drop=True)

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
        members = weights[weights["bucket_id"] == bucket_id].copy()
        # Default sort: market cap descending, NaN last.
        members["_mcap_sort"] = pd.to_numeric(members["market_cap"], errors="coerce")
        members = members.sort_values(
            "_mcap_sort", ascending=False, na_position="last"
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
            rets = ticker_returns.get(ticker, {})
            ret_cells = "".join(
                (
                    f'<td class="ct-ret" data-sort="{rets[label]:.6f}">{_fmt_pct(rets.get(label))}</td>'
                    if rets.get(label) is not None
                    else f'<td class="ct-ret" data-sort="">{_fmt_pct(None)}</td>'
                )
                for label, _ in WINDOWS
            )
            rows.append(
                f'<tr>'
                f'<td class="ct-ticker" data-sort="{escape(ticker)}"><code>{escape(ticker)}</code></td>'
                f'<td class="ct-name" data-sort="{escape((full_name or ticker).lower())}">{escape(full_name) if full_name else "<em>—</em>"}</td>'
                f'<td class="ct-wt" data-sort="{weight_pct:.6f}">{weight_pct:.2f}%</td>'
                f'<td class="ct-score" data-sort="{score:.4f}">{score:.2f}</td>'
                f'<td class="ct-mcap" data-sort="{mcap_num}">{mcap}</td>'
                f'<td class="ct-conf" data-sort="{conf_rank}">{conf}</td>'
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
            f'<th class="num sortable" data-type="num">{escape(weight_col)}</th>'
            '<th class="num sortable" data-type="num">Score</th>'
            '<th class="num sortable sorted-desc" data-type="num">Market cap</th>'
            '<th class="sortable" data-type="num">Conf</th>'
            f'{window_headers}'
            '</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>'
        )

    rows_html = []
    n_cols = 2 + len(WINDOWS)  # bucket + n + windows
    for _, r in returns.iterrows():
        bucket_id = r["bucket_id"]
        cells = [
            f'<td class="name" data-bucket="{escape(bucket_id)}">'
            f'<span class="chev">▸</span> '
            f'<strong>{escape(r["bucket_name"])}</strong>'
            f'<br><code>{escape(bucket_id)}</code></td>',
            f'<td class="n">{int(r["n_members"])}</td>',
        ]
        for label, _ in WINDOWS:
            cells.append(f'<td class="ret">{_fmt_pct(r[label])}<br>'
                         f'<span class="nsmall">n={int(r[f"{label}_n"])}</span></td>')
        rows_html.append(
            f'<tr class="bucket-row" data-bucket="{escape(bucket_id)}">'
            + "".join(cells) + "</tr>"
        )
        rows_html.append(
            f'<tr class="detail-row" data-bucket="{escape(bucket_id)}" hidden>'
            f'<td colspan="{n_cols}" class="detail-cell">{_constituent_table(bucket_id)}</td>'
            f'</tr>'
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pantheon — {escape(asof)}</title>
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
    cursor: pointer;
    user-select: none;
  }}
  td.name:hover strong {{ color: #146c2e; }}
  td.name code {{
    font-family: "SFMono-Regular", Menlo, Consolas, monospace;
    font-size: 0.75rem;
    color: #777;
    background: transparent;
  }}
  .chev {{
    display: inline-block;
    width: 0.85rem;
    color: #999;
    transition: transform 0.15s ease;
  }}
  tr.bucket-row.expanded td.name .chev {{
    transform: rotate(90deg);
    color: #146c2e;
  }}
  tr.detail-row td.detail-cell {{
    background: #faf9f2;
    padding: 0.5rem 1rem 1rem 2.2rem;
    border-top: 1px dashed #d8d5cd;
  }}
  table.constituents {{
    width: 100%;
    max-width: 900px;
    margin: 0.4rem 0 0.4rem 0;
    border: 1px solid #e2ded4;
    border-radius: 4px;
    box-shadow: none;
    font-size: 0.85rem;
  }}
  table.constituents thead th {{
    background: #efece3;
    text-transform: none;
    letter-spacing: 0;
    font-size: 0.75rem;
    padding: 0.35rem 0.55rem;
  }}
  table.constituents thead th.num {{ text-align: right; }}
  table.constituents tbody td {{
    padding: 0.3rem 0.55rem;
    border-bottom: 1px solid #efece3;
    vertical-align: middle;
  }}
  table.constituents .ct-ticker {{ text-align: left; }}
  table.constituents .ct-name {{ text-align: left; color: #444; }}
  table.constituents .ct-wt,
  table.constituents .ct-score,
  table.constituents .ct-mcap {{ text-align: right; }}
  table.constituents .ct-conf {{ text-align: center; color: #666; font-size: 0.78rem; }}
  table.constituents .ct-ret {{ text-align: right; font-size: 0.82rem; }}
  table.constituents th.sortable {{
    cursor: pointer;
    user-select: none;
    position: relative;
  }}
  table.constituents th.sortable:hover {{ background: #e5e0d1; }}
  table.constituents th.sortable::after {{
    content: " \\2195";
    color: #bbb;
    font-size: 0.7rem;
  }}
  table.constituents th.sorted-asc::after {{ content: " \\25B2"; color: #146c2e; }}
  table.constituents th.sorted-desc::after {{ content: " \\25BC"; color: #146c2e; }}
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

<h1>Pantheon</h1>
<p class="subtitle">Trailing-window returns for each of the 22 thematic sub-indices,
weighted by <code>{escape(weight_col)}</code>. Sorted by 1-year return.
Click any bucket to see its constituents.</p>

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
      {"".join(f"<th>{label.upper()}</th>" for label, _ in WINDOWS)}
    </tr>
  </thead>
  <tbody>
    {chr(10).join(rows_html)}
  </tbody>
</table>

<footer>
  <b>Click any bucket name</b> to expand its constituent list.<br>
  <b>n</b> = bucket members with a valid price for that window.
  Weights are renormalised per window across members that priced.<br>
  <b>Penny-stock filter:</b> tickers with a window-start price below $1
  are excluded from that window (dashes in the constituent table). Standard
  practice; avoids near-dead-recovery names (e.g. Zapata trading $0.0001 → $0.90)
  swamping bucket aggregates.<br>
  Prices: Polygon.io adjusted daily closes. Returns are simple total returns.<br>
  Generated by <code>scripts/bucket_returns.py</code> · <code>scripts/pull_prices.py</code>
</footer>

<script>
  // Bucket expand/collapse
  document.querySelectorAll('td.name').forEach(function(cell) {{
    cell.addEventListener('click', function() {{
      var bucket = cell.dataset.bucket;
      var row = cell.closest('tr.bucket-row');
      var detail = document.querySelector(
        'tr.detail-row[data-bucket="' + bucket + '"]'
      );
      if (!detail) return;
      var isHidden = detail.hasAttribute('hidden');
      if (isHidden) {{
        detail.removeAttribute('hidden');
        row.classList.add('expanded');
      }} else {{
        detail.setAttribute('hidden', '');
        row.classList.remove('expanded');
      }}
    }});
  }});

  // Per-constituent-table sortable headers
  document.querySelectorAll('table.constituents th.sortable').forEach(function(th) {{
    th.addEventListener('click', function(evt) {{
      evt.stopPropagation();  // don't collapse the parent bucket row
      var table = th.closest('table.constituents');
      var tbody = table.tBodies[0];
      var headers = Array.from(table.tHead.rows[0].cells);
      var colIdx = headers.indexOf(th);
      var type = th.dataset.type || 'str';
      var currentlyDesc = th.classList.contains('sorted-desc');
      var currentlyAsc = th.classList.contains('sorted-asc');
      // Toggle: if already sorted this way, flip; if not sorted, default desc for num / asc for str
      var wantDesc;
      if (currentlyDesc) wantDesc = false;
      else if (currentlyAsc) wantDesc = true;
      else wantDesc = (type === 'num');
      // Clear all headers in this table
      headers.forEach(function(h) {{
        h.classList.remove('sorted-asc', 'sorted-desc');
      }});
      th.classList.add(wantDesc ? 'sorted-desc' : 'sorted-asc');
      // Sort rows
      var rows = Array.from(tbody.rows);
      rows.sort(function(a, b) {{
        var av = a.cells[colIdx].dataset.sort;
        var bv = b.cells[colIdx].dataset.sort;
        var aEmpty = (av === '' || av == null);
        var bEmpty = (bv === '' || bv == null);
        // Empty / NaN values always last
        if (aEmpty && bEmpty) return 0;
        if (aEmpty) return 1;
        if (bEmpty) return -1;
        var cmp;
        if (type === 'num') {{
          cmp = parseFloat(av) - parseFloat(bv);
        }} else {{
          cmp = av.localeCompare(bv);
        }}
        return wantDesc ? -cmp : cmp;
      }});
      rows.forEach(function(r) {{ tbody.appendChild(r); }});
    }});
  }});
</script>

</body>
</html>
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="outputs/2026-07-25/minidex_weights.csv")
    parser.add_argument("--prices", default="data/prices.csv")
    parser.add_argument("--db", default="data/minidex.db",
                        help="SQLite DB path for enriching constituents with company names")
    parser.add_argument("--weight-col", default="weight_score",
                        choices=["weight_score", "weight_cap_score", "weight_equal"])
    parser.add_argument("--min-start-price", type=float, default=1.0,
                        help="Exclude tickers with per-window starting price below this "
                             "(default $1.00, filters penny-stock recovery distortions)")
    parser.add_argument("--out", default=None,
                        help="Output HTML path (defaults to <weights-dir>/bucket_returns.html)")
    args = parser.parse_args()

    weights_path = REPO_ROOT / args.weights if not Path(args.weights).is_absolute() else Path(args.weights)
    prices_path = REPO_ROOT / args.prices if not Path(args.prices).is_absolute() else Path(args.prices)
    db_path = REPO_ROOT / args.db if not Path(args.db).is_absolute() else Path(args.db)
    out_path = (REPO_ROOT / args.out) if args.out else (weights_path.parent / "bucket_returns.html")

    weights = pd.read_csv(weights_path)
    weights["ticker"] = weights["ticker"].astype(str).str.upper()
    prices = pd.read_csv(prices_path)
    prices["ticker"] = prices["ticker"].astype(str).str.upper()
    company_names = _load_company_names(db_path)

    print(f"bucket_returns: {len(weights):,} weight rows, {len(prices):,} price rows, "
          f"{len(company_names):,} company names, weight_col={args.weight_col}")

    tickers = sorted(set(weights["ticker"]))
    ticker_returns = compute_ticker_returns(tickers, prices, min_start_price=args.min_start_price)
    returns = compute_bucket_returns(weights, ticker_returns, args.weight_col)
    asof = weights_path.parent.name
    html = render_html(returns, weights, args.weight_col, asof, company_names, ticker_returns)
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
