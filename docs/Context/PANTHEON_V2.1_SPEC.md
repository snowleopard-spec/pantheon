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

### 3.6 New weighting method: `weight_cap_score_aug`

- `weight_cap_score` with a **per-constituent weight cap of m/N**, where N = number of constituents in the sub-index and m = `report.weight_cap_m` in `config.json` (default **3** — e.g. an 11-name index caps any single name at 3/11 ≈ 27.3%).
- **Derived at render time** in `report_metrics.py` from the existing `weight_cap_score` column — the frozen `definitions/minidex_weights.csv` is not touched.
- Methodology (standard capped-index): cap all weights at m/N, redistribute the excess proportionally among uncapped members, repeat until no member exceeds the cap; weights sum to 1 throughout. Feasible whenever m ≥ 1 (the loader validates this and rejects m < 1).
- Selectable via `report.weight_col: "weight_cap_score_aug"`; the constituent tables' Index Weight column shows the derived weights when active.

### 3.7 Architecture page on the frontend

- `docs/ARCHITECTURE.html` is fully self-contained (verified: zero external refs; the only `url()` hits are internal SVG marker ids), so it publishes verbatim: `droplet_refresh.sh` gains a second atomic publish step, `docs/ARCHITECTURE.html → /srv/pantheon/architecture.html`. `tailscale serve` already serves the whole directory — no serve-config change.
- The report header gets a subtle **"Architecture"** link to `architecture.html`; `ARCHITECTURE.html` gets a small link back to the report (`/`).
- At the end of the build, `ARCHITECTURE.html`'s content is updated to describe the v2.1 report (Sharpe column, benchmark row, charts, capped weighting) and the two-file publish.

## 4. Configuration

`config.json` gains (defaults apply if the block is absent, so nothing breaks if config is stale):

```json
"report": {
  "sharpe_window": "3m",
  "benchmark_ticker": "QQQ",
  "weight_col": "weight_score",
  "weight_cap_m": 3,
  "_valid_values": {
    "sharpe_window": ["1y", "6m", "3m", "1m", "1w"],
    "weight_col": ["weight_score", "weight_cap_score", "weight_cap_score_aug", "weight_equal"],
    "weight_cap_m": "any number >= 1 (per-name cap = m/N; only used by weight_cap_score_aug)",
    "benchmark_ticker": "any ticker Polygon serves (ETFs included)"
  }
}
```

Read by both `bucket_returns.py` and `pull_prices.py` with stdlib `json` (per D5/D6).

JSON has no comments, so `_valid_values` is the in-file documentation of what each key accepts: underscore-prefixed keys are ignored by the loader. The loader validates the *actual* settings against the same lists hardcoded in `report_metrics.py` (the source of truth — editing `_valid_values` can't unlock new values) and exits with a clear message on an invalid value rather than rendering something silently wrong.

`weight_col` moves the index-weighting method into config: `weight_score` (score-normalized, today's behavior), `weight_cap_score` (market-cap × score), or `weight_equal`. Today this exists only as the `--weight-col` CLI flag, which the droplet cron never passes — so the method is effectively hardcoded. After v2.1, edit `config.json` (git-tracked: commit → push → droplet pulls nightly) and the report follows. Precedence: explicit CLI flag > `config.json` > built-in default, matching the existing `MINIDEX_*` convention.

## 5. New dependencies

| Dependency | Kind | Notes |
|---|---|---|
| **uPlot** (v1.6.x, MIT) | Vendored JS/CSS, committed under `assets/vendor/` | Embedded into the HTML at render time. No runtime install, no server-side cost, nothing added to `pyproject.toml`. |
| **QQQ price data** | +1 Polygon call/day | Same API key, same endpoint. No new subscription. |

No new Python packages. The droplet's lean profile is untouched.

## 6. Codebase cleanup

A read-only audit runs in Wave 1 (Agent D); nothing is deleted until the findings list is approved at a gate (H2). Known candidates going in:

| Candidate | Status | Proposed action |
|---|---|---|
| `scripts/performance.py` + `tests/test_performance.py` | Tracked. The legacy static backtester (`PANTHEON_SPEC.md` §8) — never wired into the report/droplet flow, superseded by the live report; its benchmark-normalization reference value disappears once QQQ lands natively. | Delete both (pending H2). |
| `data/minidex.db.*.bak` (20 files, ~60 MB) | Disk-only (gitignored) build-stage snapshots from the July full-universe run. | Delete from the Mac; droplet unaffected. |
| `outputs/2026-07-24*` run dirs | Disk-only (gitignored). Superseded scoring iterations v11–v16. | Delete from the Mac. `outputs/2026-07-25/` (tracked) is the promoted-run archive — **keep**. |
| Dead code inside `minidex/` | Unknown — audit greps for unreferenced functions/imports/CLI paths. | Findings reported with evidence; no speculative deletion. |

## 7. File plan

### 7.1 New files

| File | Purpose |
|---|---|
| `scripts/report_metrics.py` | **The key structural decision enabling parallel work.** A pure-compute module (pandas/numpy/stdlib only, no `minidex` imports) holding everything M2 adds: `load_report_config()` (stdlib-`json` read of the root `config.json` `report` block, with defaults), `daily_returns(prices, tickers, window_days, min_start_price)` (per-ticker daily simple-return series with the penny-filter applied at window start), `sharpe(series)` (D1: mean ÷ std ddof=1 × √252, guards: <10 obs or zero std → `None`), `bucket_daily_series(weights, daily_returns, weight_col)` (daily-renormalized weighted index series), `capped_weights(weights, m)` (the §3.6 iterative m/N capping, applied when `weight_col == "weight_cap_score_aug"`), and `median_constituent_returns(...)`. Imported by `bucket_returns.py` as a same-directory import (`scripts/` lands on `sys.path` when run as a script — same mechanism the droplet cron already relies on). |
| `tests/test_report_metrics.py` | Unit tests for the above, importing via the established `sys.path.insert(0, scripts/)` pattern from `tests/test_performance.py`. Cases: Sharpe on a hand-computed series, annualization factor, <10-obs and zero-std guards, daily renormalization when a member is missing a bar, penny-filter consistency with the returns table, median with per-window membership differences, capped-weight redistribution (sums to 1, respects the cap, m<1 rejected), config defaults when the `report` block is absent, loud rejection of invalid `sharpe_window`/`weight_col` values. |
| `assets/vendor/uPlot.iife.min.js` + `assets/vendor/uPlot.min.css` | Vendored uPlot v1.6.x (MIT), committed verbatim with a `VERSION` note. Inlined into the HTML at render time. |

### 7.2 Edited files

| File | Changes |
|---|---|
| `scripts/bucket_returns.py` | The only heavily edited `.py`. Compute side: import `report_metrics`, wire Sharpe (index + constituent), medians, and the benchmark row into the returns assembly; embed the price-history JSON. Render side: all §2 label/column fixes, pinned QQQ row, median sub-lines, Sharpe columns, sortable main table with paired detail-row movement, wireframe sort arrows, chart rows + period buttons, inlined uPlot, rewritten footer. |
| `scripts/pull_prices.py` | Small: union `report.benchmark_ticker` from `load_report_config()` into the ticker list. |
| `config.json` | Add the `report` block (§4). |
| `scripts/droplet_refresh.sh` | Second atomic publish step: `docs/ARCHITECTURE.html → /srv/pantheon/architecture.html` (§3.7). |
| `docs/ARCHITECTURE.html` | Link back to the report; end-of-build content update covering the v2.1 features (§3.7). |
| `docs/Explainers/OUTPUT_COLUMNS.md` | Document Index Weight relabel, Sharpe column, median line, benchmark row, `weight_cap_score_aug`. |
| `docs/Context/PROGRESS_REPORT.md` | §15 build log — updated **at sensible intervals throughout the build** (at minimum at every wave/milestone close), not just at the end. |

Possibly deleted (pending the §6 audit + H2 approval): `scripts/performance.py`, `tests/test_performance.py`. Not touched: everything under `minidex/`, the weights CSV, cron timing, serve config.

## 8. Parallel execution plan

Ground rules: **exactly one writer per file per wave** (the repo's real constraint — `bucket_returns.py` is a single 611-line file with interleaved Python/CSS/JS, so it gets a single owner whenever it's open for edits); parallel agents are used where file ownership is disjoint; each wave ends with the orchestrating session integrating, running tests, and committing before the next wave starts.

**Wave 1 — foundations (4 agents in parallel, disjoint files; D is read-only):**
- *Agent A — metrics module:* write `scripts/report_metrics.py` + `tests/test_report_metrics.py` against the interface contract in §7.1 (including §3.6 capped weights); run pytest to green. The contract is fixed here in the spec precisely so Wave 2 can build against it without waiting.
- *Agent B — benchmark plumbing:* edit `scripts/pull_prices.py` + `config.json`; verify with a live one-ticker QQQ pull into `data/prices.csv`.
- *Agent C — uPlot vendoring:* fetch and commit `assets/vendor/` files, pin the version, smoke-test the IIFE in a minimal local HTML page.
- *Agent D — cleanup audit (read-only):* verify the §6 candidates and sweep for dead code; produce an evidenced findings list for the H2 gate. Writes nothing.

**Wave 2 — integration (sequential, single owner):** one agent (or the main session) makes all `bucket_returns.py` changes — compute wiring first, then the §2 fixes and §3 frontend features — rendering locally after each chunk. Sequential because every change lands in the same file; splitting it across agents would trade a fake speedup for merge conflicts.

**Wave 3 — verification + docs (parallel again, disjoint):**
- *Agent E — docs:* `OUTPUT_COLUMNS.md` update, `ARCHITECTURE.html` v2.1 content update (§3.7), footer-copy cross-check against actual behavior.
- *Agent F — adversarial review:* full test suite, fresh render from real data, and a browser pass over the artifact: expand/collapse, every sortable header both directions, QQQ pinned under sort, chart open + all five period buttons, the Architecture link both ways, print stylesheet, page weight sanity (~2 MB).
- *Main session — approved cleanup:* execute the H2-approved deletion list (repo deletions as their own commit; disk-only deletions logged in the PROGRESS_REPORT).

Waves map onto the milestones: Wave 1 = first half of M2, Wave 2 = rest of M2 + M3, Wave 3 = M4's pre-merge half. Merge, tag, and droplet refresh stay with the main session (deploy actions aren't parallelizable and shouldn't be delegated).

## 9. Milestones

Documentation cadence (applies across all milestones): `PROGRESS_REPORT.md` §15 is updated **at sensible intervals as the build proceeds** — at minimum at every wave/milestone close, plus whenever something surprising or decision-shaped happens — with docs committed alongside code. `ARCHITECTURE.html` is updated at the end of the build (M4) to reflect v2.1.

- **M1 — Spec + branch** (this document): branch `v2.1-upgrades` created; spec + brief committed. *Gate: user review of this spec.*
- **M2 — Compute layer** (= Wave 1 + the compute half of Wave 2): config plumbing, QQQ pull, `report_metrics.py` + `tests/test_report_metrics.py` (incl. `weight_cap_score_aug`), index + constituent Sharpe, median-constituent returns wired into the assembly; stdout table gains the Sharpe column; cleanup-audit findings presented. *Verify: pytest green, local pull + render runs clean. Gate: H2 cleanup approval.*
- **M3 — Frontend** (= the render half of Wave 2): all §2 fixes, benchmark row, median lines, Sharpe columns, sortable main table, wireframe sort arrows, uPlot charts + period buttons, Architecture link. *Verify: local render inspected in browser (expand, sort, chart, print stylesheet).*
- **M4 — Docs + release** (= Wave 3, then main-session deploy): docs + adversarial-review agents; approved cleanup executed; `ARCHITECTURE.html` updated for v2.1; `droplet_refresh.sh` two-file publish; PROGRESS_REPORT §15 finalized; merge to `main`, tag `v2.1`, droplet `git pull` + manual refresh; tailnet verification from the phone (H3).

## 10. Human actions

- **H1** — review this spec; give the go-ahead for M2.
- **H2** — approve (or trim) the cleanup audit's deletion list at the M2 gate before anything is removed.
- **H3** — after the M4 droplet refresh, confirm the upgraded report **and the Architecture page link** render on the iPhone over the tailnet URL (the droplet cannot self-verify — see `docs/Skills/VERIFYING_TAILSCALE_SERVE_LOCALLY.md`).

## 11. Out of scope

- The deleted `PANTHEON_FRONTEND_SPEC.html` "carved-stone-ledger" redesign mockup — v2.1 keeps the current GitHub-dark theme. (The mockup's deletion sits uncommitted in the working tree; disposition is the user's call at the M1 gate.)
- OHLC/volume/intraday data — charts are close-only lines, matching the data on hand. Polygon returns OHLCV, so a candlestick upgrade is possible later by widening `pull_prices.py`.
- Any change to scoring, weights, index membership, cron timing, or serving.
