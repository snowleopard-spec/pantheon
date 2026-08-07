# mini-dex — bucket coherence analysis, build specification v1.0

Audience: Claude Code, building this feature autonomously inside the
existing `pantheon` repo. The human wants to be hands-off. Follow this spec
closely; where it is silent, prefer the simplest solution that satisfies the
acceptance criteria and matches existing repo conventions
(`scripts/bucket_returns.py` is the style reference throughout). Ask
questions only if something is genuinely blocking.

## 1. Purpose

Test whether each mini-dex bucket actually *trades* like a sub-index: is the
average pairwise correlation of its members' market-residual returns higher
than that of a random same-size group drawn from the Pantheon universe?

This is a downstream **report script**, not a pipeline stage. Like
`bucket_returns.py`, it reads the frozen weights and the cached price file
and writes a self-contained HTML page. It must not touch `data/minidex.db`
except (optionally) to resolve company display names, mirroring
`_load_company_names` in `bucket_returns.py`.

## 2. Deliverables

1. `scripts/coherence.py` — standalone script, runnable as
   `uv run python scripts/coherence.py`.
2. `outputs/coherence.html` — self-contained report (stats table + ordered
   correlation heatmap), same visual style as `outputs/bucket_returns.html`.
3. `outputs/coherence.csv` — the summary table as a flat file (one row per
   bucket) for downstream use.
4. A `coherence` block in `config.json` (see §7) with a `_valid_values` key,
   following the existing `report` block convention.
5. Offline pytest coverage (see §9).
6. A short "Coherence report" subsection appended to the README's run
   sequence section.

## 3. Inputs

- Weights: `definitions/minidex_weights.csv` (default; `--weights` to
  override). Columns used: `bucket_id`, `bucket_name`, `ticker`, `score`.
- Prices: `data/prices.csv` (default; `--prices` to override), long format
  `(date, ticker, close)`, adjusted closes, as produced by
  `scripts/pull_prices.py`.
- Benchmark: `report.benchmark_ticker` from `config.json` (QQQ). It is
  already unioned into the price pulls by `pull_prices.py`; if it is missing
  from the prices file, exit with a loud error telling the user to re-run
  `pull_prices.py`.

## 4. Method

All steps are deterministic given the inputs and the RNG seed.

### 4.1 Returns panel

1. Universe = unique tickers in the weights file (a ticker in several
   buckets appears once in the panel).
2. Pivot prices to a date × ticker close matrix; compute log returns.
3. Frequency knob `coherence.freq`: `daily` (default) or `weekly`
   (Friday-to-Friday, last available close of each ISO week).
4. Analysis window `coherence.window_days` (default 365 calendar days back
   from the latest price date).
5. Coverage filter: drop tickers with fewer than `coherence.min_coverage`
   (default 0.67) of the window's return observations. Dropped tickers are
   listed in the HTML footer.
6. Remaining NaNs: pairwise-complete correlations are acceptable
   (`DataFrame.corr()` default); do not forward-fill prices.

### 4.2 Residualization

For each ticker, OLS of its returns on the benchmark's returns over the
window (intercept + slope, e.g. `numpy.polyfit` or closed-form — no new
dependencies). Keep the residual series. Also retain the raw returns panel:
every statistic below is computed twice, on residuals (headline) and on raw
returns (context column), so the report shows how much co-movement was just
market beta.

### 4.3 Correlation matrix

One Pearson correlation matrix over the residual panel (N × N, N =
surviving tickers), computed once and reused for bucket stats, null draws,
and the heatmap. Same again for raw returns.

### 4.4 Per-bucket statistic

For each bucket:

- Members = its tickers that survived the coverage filter. `n_eff` = count.
  Buckets with `n_eff < 3` are reported but flagged `insufficient` and get
  no null comparison.
- `rho_intra` = mean of the upper triangle of the members' sub-matrix.
- `rho_intra_excl_overlap` = same, excluding pairs whose two tickers share
  ≥ 2 buckets (i.e. co-members somewhere else besides this bucket). If that
  exclusion removes every pair, report blank.

### 4.5 Null distribution (size-matched random draws)

- Distinct sizes = the set of `n_eff` values across buckets (≥ 3 only).
- For each distinct size n: `coherence.n_draws` (default 10_000) uniform
  random draws of n tickers from the surviving universe, without replacement
  within a draw. Each draw's statistic is the mean upper triangle read off
  the precomputed matrix — no re-estimation.
- Cache per size: null mean, null sd, and the sorted draw statistics (for
  percentiles).
- Per bucket: `z = (rho_intra − null_mean) / null_sd` and
  `pctile` = fraction of draws strictly below `rho_intra` × 100.
- RNG: `numpy.random.default_rng(coherence.seed)` (default seed 42) so runs
  are reproducible; the seed is printed in the HTML footer.

Vectorise the draws (index arrays into the matrix, mean over axis) — the
whole null computation should run in seconds, not minutes.

## 5. HTML report

Single self-contained file, dark GitHub-style palette identical to
`bucket_returns.html`. Lift the shared CSS rather than restyling from
scratch; if that motivates factoring the CSS constant into a small shared
module under `scripts/`, do it, but do not change `bucket_returns.py`
output. Add a nav link to `bucket_returns.html` in the header line (and a
reciprocal link is **not** required — leave `bucket_returns.py` untouched).

### 5.1 Summary table

One row per bucket, sortable by column (reuse the sort mechanism from
`bucket_returns.py`), default sort: `pctile` descending.

| column | content |
|---|---|
| Bucket | `bucket_name`, linked to its heatmap block anchor |
| n | `n_eff` (nominal membership in a tooltip/`title` attr) |
| ρ intra | `rho_intra`, residual |
| ρ raw | same statistic on raw returns |
| null μ | null mean for this size |
| z | z-score |
| pctile | percentile vs null, one decimal |
| ρ excl overlap | robustness column (§4.4) |

Row shading by percentile: ≥ 95 green tint, ≤ 50 muted/grey, in the same
understated register as the existing report (no traffic-light saturation).
`insufficient` buckets render greyed with a dash in the null columns.

### 5.2 Ordered heatmap

The full residual correlation matrix with rows/columns ordered by bucket so
block structure is visible:

- Ordering: buckets sorted by `pctile` descending; within a bucket, members
  by `score` descending. Tickers in multiple buckets are drawn in the first
  bucket they appear in under that ordering (each ticker appears exactly
  once — the matrix is at ticker level).
- Render to `<canvas>` from an inlined JSON payload (matrix values quantised
  to 2 dp to keep file size down). No new vendor libraries — uPlot is not a
  heatmap library and nothing else should be added to `assets/vendor`.
- Diverging colour scale, blue (−) → dark neutral (0) → warm (+), clipped at
  ±0.5 for contrast; scale legend below the canvas.
- Thin boundary lines between bucket blocks; bucket labels along the left
  edge (canvas text, rotated or truncated as needed).
- Hover tooltip: ticker pair + correlation value (a simple mousemove →
  cell lookup; no library).

### 5.3 Footer

Method one-liner (freq, window, benchmark, draws, seed), dropped-ticker
list, generation timestamp — same register as the `bucket_returns.html`
footer.

## 6. CLI

```
uv run python scripts/coherence.py [--weights definitions/minidex_weights.csv]
                                   [--prices data/prices.csv]
                                   [--out outputs/coherence.html]
                                   [--freq daily|weekly]
                                   [--window-days 365]
                                   [--draws 10000]
                                   [--seed 42]
```

Flags override config, which overrides hardcoded defaults, consistent with
the repo-wide precedence (env `MINIDEX_*` > `config.json` > default; support
`MINIDEX_COHERENCE_FREQ` etc. via the existing config helper if it
generalises cleanly, otherwise config + flags is sufficient).

## 7. Config

Add to `config.json`:

```json
"coherence": {
  "freq": "daily",
  "window_days": 365,
  "n_draws": 10000,
  "seed": 42,
  "min_coverage": 0.67,
  "_valid_values": {
    "freq": ["daily", "weekly"],
    "window_days": "integer >= 60",
    "n_draws": "integer >= 1000",
    "min_coverage": "fraction of window observations required, 0-1"
  }
}
```

## 8. Out of scope (do not build)

- PCA / PC1 variance-explained variant — explicitly dropped.
- Score-weighted pair statistics — possible future work, not now.
- Any change to the 7-stage pipeline, the DB schema, or `bucket_returns.py`
  output.
- Serving/scheduling — droplet integration is a one-line addition to
  `droplet_refresh.sh` that the human will make himself.

## 9. Tests

Offline, mock-free math tests in the existing pytest suite:

1. Synthetic panel with two planted blocks (common factor + block factors +
   noise): planted buckets must score `pctile > 95`; a deliberately random
   "bucket" must not systematically exceed ~50.
2. Residualization: a ticker constructed as `2 × benchmark + noise` has
   near-zero residual correlation with the benchmark.
3. Determinism: two runs with the same seed produce identical CSV output.
4. Effective-n: a bucket with members missing from prices is judged against
   the null for its surviving size.
5. Upper-triangle mean: hand-checked on a 3×3 case.

## 10. Acceptance criteria

1. `uv run python scripts/coherence.py` on the current repo data produces
   `outputs/coherence.html` and `outputs/coherence.csv` with one row per
   bucket in the weights file, in under ~2 minutes on a laptop.
2. The HTML is a single self-contained file that renders offline, visually
   consistent with `bucket_returns.html`, with a sortable summary table and
   a bucket-ordered heatmap showing visible block boundaries.
3. Every number in the HTML table appears identically in `coherence.csv`.
4. Re-running with the same inputs and seed is byte-identical in the CSV.
5. `uv run pytest` passes, including the new tests, offline.

## 11. Resolved decisions (2026-08-07, at spec review)

- **Nav link (§5):** the header links `href="./"` (correct at the droplet site root where the report is `index.html`); one line of JS retargets it to `bucket_returns.html` when the page is opened over `file:` locally. Both worlds work from a single generated file.
- **Null draws (§4.5, deviation from spec text):** draws **exclude the tested bucket's members** — each bucket is compared against random groups of non-members, so the null is per-bucket rather than per-distinct-size. Chosen by the user over the spec-literal unrestricted draws.
- **Penny filter (§4.1 addition):** the same $1 window-start-price exclusion used by `bucket_returns.py` applies to the coherence panel; excluded tickers appear in the dropped list with the reason.
