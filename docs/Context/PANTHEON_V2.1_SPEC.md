# Pantheon v2.1 — Report Upgrades Specification

**Status:** approved decisions resolved 2026-08-05 · build branch `v2.1-upgrades` · tag `v2.1` on completion
**Brief:** `docs/Context/New Prompt.md` (2026-08-05)
**Scope:** display fixes and feature upgrades to the bucket-returns report (`scripts/bucket_returns.py` + `scripts/pull_prices.py`). No changes to the scoring pipeline, weights, droplet cron, or serving.

---

## 1. Resolved decisions (do not relitigate)

| # | Decision | Choice |
|---|---|---|
| D1 | Sharpe convention | **Annualized, risk-free rate = 0.** Mean daily simple return ÷ sample std (ddof=1), × √252. Same formula at any lookback window. |
| D2 | Chart implementation | **Vendored uPlot** (~48 KB min JS + ~1 KB CSS, MIT), embedded inline at render time. Report stays fully self-contained — no CDN, no external assets. |
| D3 | Constituent-level Sharpe | **Yes** — the breakdown tables get the same Sharpe column, sortable. |
| D4 | Chart data payload | **Embed full daily history** for all tickers + QQQ (~1.5–2 MB JSON in the HTML). Single-file publish pipeline unchanged. |
| D5 | Sharpe config location | New `report` block in the existing root `config.json`, read with stdlib `json` inside the report scripts. `bucket_returns.py` stays import-independent of the `minidex` package (lean droplet profile preserved). |
| D6 | Benchmark plumbing | QQQ enters via config (`report.benchmark_ticker`), unioned into the `pull_prices.py` fetch list — the droplet cron needs **no change**. |
| D7 | Versioning | First git tag in the repo: annotated tag `v2.1` on `main` after merge + verified deploy. Branch `v2.1-upgrades`, merged and deleted per the established D7 flow. |

## 2. Small fixes

All in `render_html()` / `_constituent_table()` in `scripts/bucket_returns.py`:

1. **Remove sub-names** — drop the `<code>{bucket_id}</code>` line under each display name (e.g. `memory_storage` under "Memory & storage"). `bucket_id` remains as the `data-bucket` attribute for expand/collapse.
2. **Remove the "N" column** from the main table (and its `td.n` styling). Update the `colspan` arithmetic for detail rows.
3. **Remove the `n=[x]` notation** under each return (the `.nsmall` span). Its cell position is taken by the new median-constituent line (§3.2).
4. **`weight_score` → "Index Weight"** — the constituent-table header currently renders the raw column name; use the display label "Index Weight" (independent of `--weight-col`, which keeps working).
5. **`BUCKET` → "SUB INDEX"** in the main-table header.
6. **Footer copy** updated: drop the `n` explanation, document the Sharpe methodology, the median line, the benchmark row, and credit uPlot.

## 3. Upgrades

### 3.1 QQQ benchmark row
- `pull_prices.py` unions `report.benchmark_ticker` (default `"QQQ"`) into its ticker list; first droplet run makes one extra 400-day Polygon call, then incremental.
- The main table's first row shows QQQ's 1y…1w returns **and its Sharpe ratio** (same convention — it's the comparison anchor for the new column).
- Pinned: excluded from sorting, always first. Visually differentiated with a distinct background tint. Not expandable; no median sub-line (single ticker).

### 3.2 Median-constituent return line
- Under each index's return, same `_fmt_pct` format in a smaller font: the **unweighted median** of valid constituent returns for that window (post penny-filter, same exclusions as the weighted number). The median stock may differ per window — that is expected and fine.

### 3.3 Sharpe ratio column
- New column **before 1y** in the main table, header e.g. `3M SHARPE` (label derives from the configured window).
- **Lookback configurable**: `report.sharpe_window` ∈ `{"1y","6m","3m","1m","1w"}`, default `"3m"`, using the existing calendar-day window map snapped to trading days.
- **Index-level series:** for each trading day in the window, the index daily return = Σ wᵢ·rᵢ over members with a close on that day and the prior trading day, weights renormalized daily (mirrors the existing per-window renormalization). Members failing the `min_start_price` filter at window start are excluded for the whole window.
- **Constituent-level:** same formula on the individual ticker's daily series (per D3).
- Guards: fewer than 10 daily returns, or zero std → em-dash. Sharpe cells are sortable like return cells.

### 3.4 Sortable main table
- The 22-row table becomes sortable by the Sharpe column and each of the five return columns (click to toggle asc/desc; default remains 1y descending). Detail rows travel with their parent row; the QQQ row never moves.
- **Sort indicators restyled**: the clunky `↕/▲/▼` text glyphs are replaced with subtle wireframe arrows (thin-stroke inline SVG chevrons) in both the main and constituent tables.

### 3.5 Constituent price charts
- Clicking a constituent row opens a nested row with a Bloomberg-style chart: dark-theme uPlot line of adjusted daily closes with a subtle gradient fill, hover crosshair with date + price readout.
- Period buttons **1Y · 6M · 3M · 1M · 1W** above the chart set the visible lookback (default 1Y). Charts instantiate lazily on first expand.
- Data embedded once as a JSON blob keyed by ticker: `[epoch-seconds…], [closes…]` (per D4).

## 4. Configuration

`config.json` gains (defaults apply if the block is absent, so nothing breaks if config is stale):

```json
"report": {
  "sharpe_window": "3m",
  "benchmark_ticker": "QQQ"
}
```

Read by both `bucket_returns.py` and `pull_prices.py` with stdlib `json` (per D5/D6).

## 5. New dependencies

| Dependency | Kind | Notes |
|---|---|---|
| **uPlot** (v1.6.x, MIT) | Vendored JS/CSS, committed under `assets/vendor/` | Embedded into the HTML at render time. No runtime install, no server-side cost, nothing added to `pyproject.toml`. |
| **QQQ price data** | +1 Polygon call/day | Same API key, same endpoint. No new subscription. |

No new Python packages. The droplet's lean profile is untouched.

## 6. Milestones

- **M1 — Spec + branch** (this document): branch `v2.1-upgrades` created; spec + brief committed. *Gate: user review of this spec.*
- **M2 — Compute layer**: config plumbing, QQQ pull, daily-return-series builder, index + constituent Sharpe, median-constituent returns; new `tests/test_bucket_returns.py` (Sharpe on a known series, median logic, daily-renormalization, penny-filter consistency); stdout table gains the Sharpe column. *Verify: pytest green, local pull + render runs clean.*
- **M3 — Frontend**: all §2 fixes, benchmark row, median lines, Sharpe columns, sortable main table, wireframe sort arrows, uPlot charts + period buttons. *Verify: local render inspected in browser (expand, sort, chart, print stylesheet).*
- **M4 — Docs + release**: update `docs/Explainers/OUTPUT_COLUMNS.md` and footer-adjacent docs; PROGRESS_REPORT §15 finalized; merge to `main`, tag `v2.1`, droplet `git pull` + manual refresh; tailnet verification from the phone (H2).

## 7. Human actions

- **H1** — review this spec; give the go-ahead for M2.
- **H2** — after the M4 droplet refresh, confirm the upgraded report renders on the iPhone over the tailnet URL (the droplet cannot self-verify — see `docs/Skills/VERIFYING_TAILSCALE_SERVE_LOCALLY.md`).

## 8. Out of scope

- The deleted `PANTHEON_FRONTEND_SPEC.html` "carved-stone-ledger" redesign mockup — v2.1 keeps the current GitHub-dark theme. (The mockup's deletion sits uncommitted in the working tree; disposition is the user's call at the M1 gate.)
- OHLC/volume/intraday data — charts are close-only lines, matching the data on hand. Polygon returns OHLCV, so a candlestick upgrade is possible later by widening `pull_prices.py`.
- Any change to scoring, weights, index membership, cron timing, or serving.
