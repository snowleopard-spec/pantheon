"""Bucket coherence analysis: does each mini-dex bucket actually trade like a
sub-index?

For every bucket, compare the average pairwise Pearson correlation of its
members' market-residual returns (`rho_intra`) against a null distribution of
size-matched random groups drawn from the surviving Pantheon universe
*excluding the bucket's own members* (spec §11). Residuals come from a
per-ticker OLS on the benchmark's returns, so the headline statistic measures
co-movement beyond market beta; the same statistic on raw returns is shown as
context.

A downstream report script, not a pipeline stage (see
docs/Context/COHERENCE_SPEC_1.md): reads the frozen weights and the cached
price file, writes a self-contained HTML page (summary table + bucket-ordered
correlation heatmap) plus a flat CSV. Deterministic given the inputs and the
seed. Config lives in the root config.json "coherence" block; CLI flags
override config, which overrides defaults.

Usage:
    uv run python scripts/coherence.py [--weights definitions/minidex_weights.csv]
                                       [--prices data/prices.csv]
                                       [--out outputs/coherence.html]
                                       [--freq daily|weekly]
                                       [--window-days 365]
                                       [--draws 10000]
                                       [--seed 42]
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path

import numpy as np
import pandas as pd

# Same-directory imports (scripts/ is on sys.path when run as a script; tests
# insert it explicitly). BASE_CSS reuse keeps the two reports visually
# identical without touching bucket_returns output.
import report_metrics
from bucket_returns import CSS as BASE_CSS

REPO_ROOT = Path(__file__).resolve().parent.parent

COHERENCE_DEFAULTS = {
    "freq": "daily",
    "window_days": 365,
    "n_draws": 10_000,
    "seed": 42,
    "min_coverage": 0.67,
}
VALID_FREQS = ("daily", "weekly")
MIN_WINDOW_DAYS = 60
MIN_DRAWS = 1_000
MIN_BUCKET_N = 3  # below this a bucket is 'insufficient': no null comparison


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_coherence_config(path: Path | str | None = None) -> dict:
    """The config.json "coherence" block over defaults, validated loudly.

    Keys starting with "_" are in-file documentation and ignored. Invalid
    values raise ValueError naming the key, the bad value and what's allowed.
    """
    cfg_path = Path(path) if path is not None else REPO_ROOT / "config.json"
    block: dict = {}
    if cfg_path.exists():
        try:
            block = json.loads(cfg_path.read_text(encoding="utf-8")).get("coherence", {})
        except (OSError, ValueError):
            block = {}
    merged = dict(COHERENCE_DEFAULTS)
    merged.update({k: v for k, v in block.items() if not k.startswith("_")})

    if merged["freq"] not in VALID_FREQS:
        raise ValueError(
            f"coherence.freq: invalid value {merged['freq']!r}; valid: {list(VALID_FREQS)}")
    if not isinstance(merged["window_days"], int) or merged["window_days"] < MIN_WINDOW_DAYS:
        raise ValueError(
            f"coherence.window_days: invalid value {merged['window_days']!r}; "
            f"integer >= {MIN_WINDOW_DAYS} required")
    if not isinstance(merged["n_draws"], int) or merged["n_draws"] < MIN_DRAWS:
        raise ValueError(
            f"coherence.n_draws: invalid value {merged['n_draws']!r}; "
            f"integer >= {MIN_DRAWS} required")
    if not isinstance(merged["seed"], int):
        raise ValueError(f"coherence.seed: invalid value {merged['seed']!r}; integer required")
    mc = merged["min_coverage"]
    if not isinstance(mc, (int, float)) or not (0 < float(mc) <= 1):
        raise ValueError(
            f"coherence.min_coverage: invalid value {mc!r}; fraction in (0, 1] required")
    merged["min_coverage"] = float(mc)
    return merged


# ---------------------------------------------------------------------------
# Panel construction
# ---------------------------------------------------------------------------

def build_panel(
    prices: pd.DataFrame,
    tickers: list[str],
    benchmark: str,
    freq: str,
    window_days: int,
    min_coverage: float,
    min_start_price: float = 1.0,
) -> tuple[pd.DataFrame, pd.Series, list[tuple[str, str]]]:
    """Log-return panel for the analysis window.

    Returns (returns_panel[date × surviving ticker], benchmark_returns,
    dropped) where dropped is [(ticker, reason)] — reasons: 'no_prices',
    'penny' ($1 window-start filter, matching bucket_returns), 'coverage'.
    The benchmark is required and never part of the universe panel.
    """
    prices = prices.copy()
    prices["date"] = pd.to_datetime(prices["date"])
    pivot = prices.pivot_table(index="date", columns="ticker", values="close",
                               aggfunc="last").sort_index()
    if benchmark not in pivot.columns:
        sys.exit(f"coherence: benchmark {benchmark} missing from the prices file — "
                 f"re-run scripts/pull_prices.py first")

    end = pivot.index.max()
    start = end - timedelta(days=window_days)

    dropped: list[tuple[str, str]] = []
    survivors: list[str] = []
    for t in tickers:
        if t not in pivot.columns or pivot[t].notna().sum() == 0:
            dropped.append((t, "no_prices"))
            continue
        col = pivot[t]
        at_or_before = col.loc[col.index <= start].dropna()
        if not at_or_before.empty:
            start_price = float(at_or_before.iloc[-1])
        else:
            in_window = col.loc[col.index > start].dropna()
            start_price = float(in_window.iloc[0]) if not in_window.empty else np.nan
        if np.isnan(start_price) or start_price < min_start_price:
            dropped.append((t, "penny"))
            continue
        survivors.append(t)

    window = pivot.loc[pivot.index > start, survivors + [benchmark]]
    if freq == "weekly":
        iso = window.index.isocalendar()
        week_key = iso["year"].astype(str) + "-" + iso["week"].astype(str).str.zfill(2)
        window = window.groupby(week_key.values).last()
        window.index = pd.Index(window.index, name="week")

    rets = np.log(window).diff().iloc[1:]
    expected = len(rets)
    if expected == 0:
        sys.exit("coherence: no return observations in the analysis window")

    kept: list[str] = []
    for t in survivors:
        cov = rets[t].notna().sum() / expected
        if cov < min_coverage:
            dropped.append((t, "coverage"))
        else:
            kept.append(t)

    return rets[kept], rets[benchmark], dropped


def residualize(rets: pd.DataFrame, bench: pd.Series) -> pd.DataFrame:
    """Per-ticker OLS residuals vs the benchmark (intercept + slope,
    closed-form over each ticker's non-NaN overlap with the benchmark)."""
    out = pd.DataFrame(index=rets.index, columns=rets.columns, dtype=float)
    b = bench.astype(float)
    for t in rets.columns:
        y = rets[t].astype(float)
        mask = y.notna() & b.notna()
        if mask.sum() < 3:
            continue
        x, yy = b[mask].to_numpy(), y[mask].to_numpy()
        xm, ym = x.mean(), yy.mean()
        var = ((x - xm) ** 2).sum()
        slope = ((x - xm) * (yy - ym)).sum() / var if var > 0 else 0.0
        inter = ym - slope * xm
        out.loc[mask, t] = yy - (inter + slope * x)
    return out


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def mean_upper(matrix: np.ndarray) -> float:
    """Mean of the upper triangle (off-diagonal), NaN-aware; NaN if no pairs."""
    n = matrix.shape[0]
    if n < 2:
        return float("nan")
    vals = matrix[np.triu_indices(n, k=1)]
    vals = vals[~np.isnan(vals)]
    return float(vals.mean()) if len(vals) else float("nan")


def null_draws(
    corr: np.ndarray,
    pool_idx: np.ndarray,
    n: int,
    n_draws: int,
    rng: np.random.Generator,
) -> np.ndarray:
    """n_draws mean-upper-triangle statistics for random n-subsets of pool_idx
    (without replacement within a draw), read off the precomputed matrix.
    Chunked so memory stays ~tens of MB regardless of n."""
    stats = np.empty(n_draws)
    chunk = max(1, int(2_000_000 // max(1, n * n)))
    pos = 0
    while pos < n_draws:
        k = min(chunk, n_draws - pos)
        r = rng.random((k, len(pool_idx)))
        sel = np.argpartition(r, n - 1, axis=1)[:, :n]
        idx = pool_idx[sel]                                   # k × n
        sub = corr[idx[:, :, None], idx[:, None, :]]          # k × n × n
        diag = np.diagonal(sub, axis1=1, axis2=2)
        s = np.nansum(sub, axis=(1, 2)) - np.nansum(diag, axis=1)
        cnt = (~np.isnan(sub)).sum(axis=(1, 2)) - (~np.isnan(diag)).sum(axis=1)
        with np.errstate(invalid="ignore"):
            stats[pos:pos + k] = np.where(cnt > 0, s / np.maximum(cnt, 1), np.nan)
        pos += k
    return stats


def compute(
    weights: pd.DataFrame,
    prices: pd.DataFrame,
    benchmark: str,
    freq: str,
    window_days: int,
    n_draws: int,
    seed: int,
    min_coverage: float,
) -> dict:
    """Full coherence computation. Returns a dict with the stats DataFrame,
    both correlation matrices, the panel metadata and the heatmap ordering —
    everything the renderers (HTML/CSV) and the tests need."""
    weights = weights.copy()
    weights["ticker"] = weights["ticker"].astype(str).str.upper()
    tickers = sorted(set(weights["ticker"]))

    rets, bench, dropped = build_panel(
        prices, tickers, benchmark, freq, window_days, min_coverage)
    universe = list(rets.columns)
    resid = residualize(rets, bench)

    corr_resid = resid.corr().to_numpy()
    corr_raw = rets.corr().to_numpy()
    pos = {t: i for i, t in enumerate(universe)}

    # Pair-overlap counts for the robustness column: how many buckets a pair
    # of tickers co-inhabits (>= 2 means co-members somewhere else too).
    bucket_sets: dict[str, set[str]] = {}
    for _, r in weights.iterrows():
        bucket_sets.setdefault(r["ticker"], set()).add(r["bucket_id"])

    rng = np.random.default_rng(seed)
    rows = []
    for bucket_id, g in weights.groupby("bucket_id", sort=False):
        bucket_name = g["bucket_name"].iloc[0]
        nominal = g["ticker"].tolist()
        members = [t for t in nominal if t in pos]
        n_eff = len(members)
        row: dict = {
            "bucket_id": bucket_id, "bucket_name": bucket_name,
            "n_nominal": len(nominal), "n_eff": n_eff,
            "members": members,
            "member_scores": {t: float(s) for t, s in zip(g["ticker"], g["score"])},
        }
        if n_eff < MIN_BUCKET_N:
            row.update(status="insufficient", rho_intra=np.nan, rho_raw=np.nan,
                       null_mean=np.nan, null_sd=np.nan, z=np.nan, pctile=np.nan,
                       rho_excl_overlap=np.nan)
            rows.append(row)
            continue

        idx = np.array([pos[t] for t in members])
        sub = corr_resid[np.ix_(idx, idx)]
        rho = mean_upper(sub)
        rho_raw = mean_upper(corr_raw[np.ix_(idx, idx)])

        # Robustness: drop pairs that co-inhabit >= 2 buckets.
        keep_vals = []
        for i in range(n_eff):
            for j in range(i + 1, n_eff):
                if len(bucket_sets[members[i]] & bucket_sets[members[j]]) < 2:
                    v = sub[i, j]
                    if not np.isnan(v):
                        keep_vals.append(v)
        rho_excl = float(np.mean(keep_vals)) if keep_vals else np.nan

        # Null: size-matched draws from the universe EXCLUDING this bucket's
        # members (spec §11 resolved decision — per-bucket null).
        pool = np.array([i for t, i in pos.items() if t not in set(members)])
        if len(pool) < n_eff:
            # Not enough non-members to form a size-matched draw — report the
            # rhos but no null comparison. Unreachable on real data.
            row.update(status="insufficient", rho_intra=rho, rho_raw=rho_raw,
                       null_mean=np.nan, null_sd=np.nan, z=np.nan, pctile=np.nan,
                       rho_excl_overlap=rho_excl)
            rows.append(row)
            continue
        draws = null_draws(corr_resid, pool, n_eff, n_draws, rng)
        draws = draws[~np.isnan(draws)]
        null_mean = float(draws.mean())
        null_sd = float(draws.std(ddof=1))
        z = (rho - null_mean) / null_sd if null_sd > 1e-12 else np.nan
        pctile = float((draws < rho).mean() * 100) if len(draws) else np.nan

        row.update(status="ok", rho_intra=rho, rho_raw=rho_raw,
                   null_mean=null_mean, null_sd=null_sd, z=z, pctile=pctile,
                   rho_excl_overlap=rho_excl)
        rows.append(row)

    stats = pd.DataFrame(rows)

    # Heatmap ordering: buckets by pctile desc (insufficient last), members by
    # score desc; each ticker drawn once, in the first bucket it appears in.
    order: list[str] = []
    seen: set[str] = set()
    blocks: list[dict] = []
    ordered = stats.sort_values(["pctile"], ascending=False, na_position="last")
    for _, r in ordered.iterrows():
        members = sorted(r["members"], key=lambda t: -r["member_scores"].get(t, 0.0))
        fresh = [t for t in members if t not in seen]
        if not fresh:
            continue
        blocks.append({"bucket_id": r["bucket_id"], "bucket_name": r["bucket_name"],
                       "start": len(order), "size": len(fresh)})
        order.extend(fresh)
        seen.update(fresh)

    return {
        "stats": stats, "corr_resid": corr_resid, "corr_raw": corr_raw,
        "universe": universe, "dropped": dropped, "order": order,
        "blocks": blocks, "n_obs": len(rets),
        "freq": freq, "window_days": window_days, "n_draws": n_draws,
        "seed": seed, "min_coverage": min_coverage, "benchmark": benchmark,
        "asof": None if rets.empty else str(rets.index[-1])[:10],
    }


# ---------------------------------------------------------------------------
# Output formatting — the CSV strings ARE the HTML strings (acceptance §10.3)
# ---------------------------------------------------------------------------

def _fmt(x: float | None, nd: int) -> str:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return ""
    return f"{x:.{nd}f}"


def stats_display(stats: pd.DataFrame) -> pd.DataFrame:
    """One formatted-string frame feeding both the CSV and the HTML table."""
    disp = pd.DataFrame({
        "bucket_id": stats["bucket_id"],
        "bucket_name": stats["bucket_name"],
        "n_nominal": stats["n_nominal"].astype(str),
        "n_eff": stats["n_eff"].astype(str),
        "rho_intra": stats["rho_intra"].map(lambda v: _fmt(v, 3)),
        "rho_raw": stats["rho_raw"].map(lambda v: _fmt(v, 3)),
        "null_mean": stats["null_mean"].map(lambda v: _fmt(v, 3)),
        "null_sd": stats["null_sd"].map(lambda v: _fmt(v, 4)),
        "z": stats["z"].map(lambda v: _fmt(v, 2)),
        "pctile": stats["pctile"].map(lambda v: _fmt(v, 1)),
        "rho_excl_overlap": stats["rho_excl_overlap"].map(lambda v: _fmt(v, 3)),
        "status": stats["status"],
    })
    order = stats["pctile"].fillna(-1).sort_values(ascending=False).index
    return disp.loc[order].reset_index(drop=True)


def write_csv(disp: pd.DataFrame, path: Path) -> None:
    path.write_text(disp.to_csv(index=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

EXTRA_CSS = """
  tr.hot td { background: #12261a; }
  tr.hot:hover td { background: #17301f; }
  tr.cool td { color: #8b949e; }
  tr.insufficient td { color: #4b5563; }
  td.cell-name { text-align: left; }
  td.cell-name a { color: inherit; text-decoration: none; }
  td.cell-name a:hover { color: #56d364; }
  td.num-cell { text-align: right; font-variant-numeric: tabular-nums; }
  #heatmap-wrap { margin-top: 2rem; position: relative; }
  #heatmap-wrap h2 { font-size: 1.1rem; color: #b1bac4; font-weight: 600; }
  #hm-tip {
    position: fixed;
    z-index: 30;
    background: #1c232c;
    border: 1px solid #30363d;
    border-radius: 4px;
    color: #e6edf3;
    font-size: 0.78rem;
    padding: 0.25rem 0.5rem;
    pointer-events: none;
    display: none;
    font-variant-numeric: tabular-nums;
  }
  #hm-legend { margin-top: 0.5rem; color: #8b949e; font-size: 0.78rem; }
  #hm-legend canvas { vertical-align: middle; margin: 0 0.5rem; border: 1px solid #30363d; }
"""

# Tokens: __PAYLOAD__
HM_SCRIPT = """
  var HM = __PAYLOAD__;

  // ---- summary table sort (single tbody, no pinned rows) -----------------
  var cohTable = document.getElementById('coh-table');
  cohTable.tHead.querySelectorAll('th.sortable').forEach(function(th) {
    th.addEventListener('click', function() {
      var headers = Array.from(cohTable.tHead.rows[0].cells);
      var colIdx = headers.indexOf(th);
      var type = th.dataset.type || 'str';
      var wantDesc;
      if (th.classList.contains('sorted-desc')) wantDesc = false;
      else if (th.classList.contains('sorted-asc')) wantDesc = true;
      else wantDesc = (type === 'num');
      headers.forEach(function(h) { h.classList.remove('sorted-asc', 'sorted-desc'); });
      th.classList.add(wantDesc ? 'sorted-desc' : 'sorted-asc');
      var tbody = cohTable.tBodies[0];
      var rows = Array.from(tbody.rows);
      rows.sort(function(a, b) {
        var av = a.cells[colIdx].dataset.sort, bv = b.cells[colIdx].dataset.sort;
        var aE = (av === '' || av == null), bE = (bv === '' || bv == null);
        if (aE && bE) return 0;
        if (aE) return 1;
        if (bE) return -1;
        var cmp = (type === 'num') ? (parseFloat(av) - parseFloat(bv)) : av.localeCompare(bv);
        return wantDesc ? -cmp : cmp;
      });
      rows.forEach(function(r) { tbody.appendChild(r); });
    });
  });

  // ---- heatmap ------------------------------------------------------------
  var N = HM.order.length;
  var canvas = document.getElementById('hm-canvas');
  var maxW = Math.min(920, document.body.clientWidth - 40);
  var cell = Math.max(2, Math.floor((maxW - 120) / N));
  var pad = 120;  // left label gutter
  canvas.width = pad + cell * N;
  canvas.height = cell * N + 4;
  var ctx = canvas.getContext('2d');

  function color(v) {
    if (v == null) return '#0d1117';
    var t = Math.max(-0.5, Math.min(0.5, v)) / 0.5;  // clip ±0.5
    var neutral = [22, 27, 34], blue = [88, 166, 255], warm = [240, 136, 62];
    var from = neutral, to = t < 0 ? blue : warm, a = Math.abs(t);
    var c = from.map(function(f, i) { return Math.round(f + (to[i] - f) * a); });
    return 'rgb(' + c.join(',') + ')';
  }

  ctx.fillStyle = '#0d1117';
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  for (var i = 0; i < N; i++) {
    var rowVals = HM.m[i];
    for (var j = 0; j < N; j++) {
      ctx.fillStyle = color(rowVals[j]);
      ctx.fillRect(pad + j * cell, i * cell, cell, cell);
    }
  }
  // block boundaries + labels
  ctx.strokeStyle = '#30363d';
  ctx.lineWidth = 1;
  ctx.font = '10px -apple-system, sans-serif';
  ctx.textBaseline = 'middle';
  HM.blocks.forEach(function(b) {
    var y0 = b.start * cell, y1 = (b.start + b.size) * cell;
    ctx.beginPath();
    ctx.moveTo(pad, y0); ctx.lineTo(pad + N * cell, y0);
    ctx.moveTo(pad + b.start * cell, 0); ctx.lineTo(pad + b.start * cell, N * cell);
    ctx.stroke();
    if (b.size * cell >= 10) {
      ctx.fillStyle = '#8b949e';
      var label = b.name.length > 20 ? b.name.slice(0, 19) + '…' : b.name;
      ctx.fillText(label, 2, (y0 + y1) / 2);
    }
  });

  // hover tooltip
  var tip = document.getElementById('hm-tip');
  canvas.addEventListener('mousemove', function(evt) {
    var r = canvas.getBoundingClientRect();
    var x = evt.clientX - r.left - pad, y = evt.clientY - r.top;
    var j = Math.floor(x / cell), i = Math.floor(y / cell);
    if (x < 0 || i < 0 || i >= N || j >= N) { tip.style.display = 'none'; return; }
    var v = HM.m[i][j];
    tip.textContent = HM.order[i] + ' × ' + HM.order[j] + ': ' +
                      (v == null ? '—' : v.toFixed(2));
    tip.style.left = (evt.clientX + 14) + 'px';
    tip.style.top = (evt.clientY + 14) + 'px';
    tip.style.display = 'block';
  });
  canvas.addEventListener('mouseleave', function() { tip.style.display = 'none'; });

  // legend gradient
  var lg = document.getElementById('hm-grad');
  var lctx = lg.getContext('2d');
  for (var px = 0; px < lg.width; px++) {
    lctx.fillStyle = color((px / lg.width) - 0.5);
    lctx.fillRect(px, 0, 1, lg.height);
  }

  // local file: retarget the report link (droplet root serves it as index.html)
  if (location.protocol === 'file:') {
    document.getElementById('report-link').href = 'bucket_returns.html';
  }
"""

COLUMNS = [
    ("bucket_name", "Bucket", "str"),
    ("n_eff", "n", "num"),
    ("rho_intra", "ρ intra", "num"),
    ("rho_raw", "ρ raw", "num"),
    ("null_mean", "null μ", "num"),
    ("z", "z", "num"),
    ("pctile", "pctile", "num"),
    ("rho_excl_overlap", "ρ excl overlap", "num"),
]


def render_html(res: dict, disp: pd.DataFrame) -> str:
    now_utc = datetime.now(timezone.utc)
    generated = f"{now_utc.day} {now_utc.strftime('%b %Y %H:%M')} UTC"

    nominal = dict(zip(disp["bucket_id"], disp["n_nominal"]))
    rows_html = []
    for _, r in disp.iterrows():
        p = float(r["pctile"]) if r["pctile"] != "" else None
        cls = "insufficient" if r["status"] == "insufficient" else (
            "hot" if p is not None and p >= 95 else ("cool" if p is not None and p <= 50 else ""))
        cells = [
            f'<td class="cell-name" data-sort="{escape(r["bucket_name"].lower())}">'
            f'<a href="#hm-anchor">{escape(r["bucket_name"])}</a></td>',
            f'<td class="num-cell" data-sort="{r["n_eff"]}" '
            f'title="nominal membership: {escape(nominal[r["bucket_id"]])}">{r["n_eff"]}</td>',
        ]
        for key, _, _ in COLUMNS[2:]:
            val = r[key]
            cells.append(f'<td class="num-cell" data-sort="{val}">'
                         f'{val if val != "" else "—"}</td>')
        rows_html.append(f'<tr class="{cls}">' + "".join(cells) + "</tr>")

    headers = "".join(
        f'<th class="sortable{" sorted-desc" if key == "pctile" else ""}" '
        f'data-type="{typ}">{escape(label)}</th>'
        for key, label, typ in COLUMNS
    )

    order = res["order"]
    tick_pos = {t: i for i, t in enumerate(order)}
    m = res["corr_resid"]
    upos = {t: i for i, t in enumerate(res["universe"])}
    hm_matrix = [
        [None if np.isnan(m[upos[a], upos[b]]) else round(float(m[upos[a], upos[b]]), 2)
         for b in order]
        for a in order
    ]
    payload = json.dumps({
        "order": order,
        "m": hm_matrix,
        "blocks": [{"name": b["bucket_name"], "start": b["start"], "size": b["size"]}
                   for b in res["blocks"]],
    }, separators=(",", ":")).replace("</", "<\\/")
    script = HM_SCRIPT.replace("__PAYLOAD__", payload)

    dropped_str = ", ".join(f"{t} ({reason})" for t, reason in res["dropped"]) or "none"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Pantheon — Coherence</title>
<style>{BASE_CSS}</style>
<style>{EXTRA_CSS}</style>
</head>
<body>

<h1>Coherence</h1>
<p class="updated">Do the buckets trade like sub-indices? ·
<a id="report-link" href="./">← Report</a> · generated {escape(generated)}</p>

<table id="coh-table">
  <thead><tr>{headers}</tr></thead>
  <tbody>{"".join(rows_html)}</tbody>
</table>

<div id="heatmap-wrap">
  <h2 id="hm-anchor">Residual correlation matrix, ordered by bucket</h2>
  <canvas id="hm-canvas"></canvas>
  <div id="hm-legend">−0.5 <canvas id="hm-grad" width="160" height="12"></canvas> +0.5
  (clipped for contrast)</div>
</div>
<div id="hm-tip"></div>

<footer>
  <b>ρ intra</b>: mean pairwise Pearson correlation of members'
  <b>market-residual</b> returns (per-ticker OLS on {escape(res["benchmark"])});
  <b>ρ raw</b> is the same statistic before residualization — the gap is market
  beta. <b>Null</b>: {res["n_draws"]:,} size-matched random draws per bucket
  from the surviving universe excluding that bucket's members; <b>z</b> and
  <b>pctile</b> are read against that null. Buckets with fewer than
  {MIN_BUCKET_N} surviving members are flagged insufficient.<br>
  <b>ρ excl overlap</b>: recomputed excluding pairs that co-inhabit ≥ 2 buckets.<br>
  Method: freq={escape(res["freq"])}, window={res["window_days"]}d
  (through {escape(res["asof"] or "?")}), {res["n_obs"]} return observations,
  min coverage {res["min_coverage"]:.2f}, $1 window-start penny filter,
  seed {res["seed"]}.<br>
  <b>Dropped tickers:</b> {escape(dropped_str)}.<br>
  Generated by <code>scripts/coherence.py</code>
</footer>

<script>{script}</script>

</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights", default="definitions/minidex_weights.csv")
    parser.add_argument("--prices", default="data/prices.csv")
    parser.add_argument("--out", default=None,
                        help="Output HTML path (default outputs/coherence.html; "
                             "the CSV lands beside it)")
    parser.add_argument("--freq", default=None, choices=list(VALID_FREQS))
    parser.add_argument("--window-days", type=int, default=None)
    parser.add_argument("--draws", type=int, default=None)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    cfg = load_coherence_config()
    freq = args.freq or cfg["freq"]
    window_days = args.window_days if args.window_days is not None else cfg["window_days"]
    n_draws = args.draws if args.draws is not None else cfg["n_draws"]
    seed = args.seed if args.seed is not None else cfg["seed"]

    benchmark = str(report_metrics.load_report_config()["benchmark_ticker"]).upper()

    weights_path = REPO_ROOT / args.weights if not Path(args.weights).is_absolute() else Path(args.weights)
    prices_path = REPO_ROOT / args.prices if not Path(args.prices).is_absolute() else Path(args.prices)
    out_path = (REPO_ROOT / args.out) if args.out else (REPO_ROOT / "outputs" / "coherence.html")
    csv_path = out_path.with_suffix(".csv")

    weights = pd.read_csv(weights_path)
    prices = pd.read_csv(prices_path)
    prices["ticker"] = prices["ticker"].astype(str).str.upper()

    print(f"coherence: {len(weights):,} weight rows, {len(prices):,} price rows, "
          f"freq={freq}, window={window_days}d, draws={n_draws:,}, seed={seed}")

    res = compute(weights, prices, benchmark, freq, window_days, n_draws, seed,
                  cfg["min_coverage"])
    disp = stats_display(res["stats"])
    write_csv(disp, csv_path)
    out_path.write_text(render_html(res, disp), encoding="utf-8")

    n_ok = int((res["stats"]["status"] == "ok").sum())
    print(f"coherence: wrote {out_path} and {csv_path} "
          f"({len(disp)} buckets, {n_ok} with null comparison, "
          f"{len(res['dropped'])} tickers dropped)")
    print()
    print(disp[["bucket_name", "n_eff", "rho_intra", "rho_raw", "z", "pctile"]]
          .to_string(index=False))


if __name__ == "__main__":
    main()
