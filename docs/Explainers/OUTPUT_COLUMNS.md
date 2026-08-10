# Output columns — `outputs/<asof>/minidex_weights.csv`

Every row in the output CSV represents one company's membership in one bucket. There's one row per `(bucket_id, ticker)` pair — so a diversified company like NVIDIA appears multiple times, once per bucket it belongs to.

The 15 columns break into five groups:

- **Identifiers** — which company, which bucket
- **Scoring** — how much this company belongs to this bucket
- **Financials** — company size context
- **Weights** — three ways to convert scores into portfolio weights
- **Rationale + provenance** — audit trail for each score

---

## Identifiers

### `bucket_id`
Stable machine-readable slug for the bucket.
- **Example:** `fabless_chip_design`
- Matches the `id:` field in `definitions/minidex_definitions.yaml`. Never changes across runs; safe to key on.

### `bucket_name`
Human-readable display name for the bucket.
- **Example:** `Fabless chip design`
- Sourced from the `name:` field in the YAML. Change here is cosmetic only.

### `ticker`
Exchange ticker symbol, uppercase.
- **Example:** `NVDA`

### `cik`
SEC Central Index Key, 10-digit zero-padded. Uniquely identifies the filer at EDGAR — more reliable than ticker (which can be reassigned after mergers or delistings).
- **Example:** `0001045810`

---

## Scoring

### `score`
The LLM's estimate of what fraction of the company's total revenue comes from this bucket's defined activity. Averaged across two independent scoring runs.
- **Range:** `0.0` to `1.0` (a score of `0.75` means ~75% of revenue)
- **Example:** `0.85` for NVDA on `fabless_chip_design`
- Companies below the `score_floor` (hardcoded default `0.10`; currently `0.25` in `config.json`) are excluded from the bucket, unless they're an anchor (see below).

### `confidence`
The **minimum** confidence across the two scoring runs.
- **Values:** `high`, `medium`, `low`
- `low` typically means one of: (a) the LLM had no segment data and reasoned from Item 1 text alone, (b) the company is pre-revenue and Rule 5 kicked in, or (c) the filing was terse or ambiguous. Worth reviewing during QC.

---

## Financials

### `market_cap`
Market capitalisation in US dollars, computed at fetch time as `common_shares_outstanding × yfinance_last_close`.
- **Example:** `5069945226593.02` (~$5.07T for NVDA)
- May be `null` for foreign filers where yfinance has no price data. Rows with null market cap get a `weight_cap_score` of 0 — they still appear in the CSV but contribute nothing to that weight scheme.

---

## Weights

Every row carries three parallel weight columns. All three are normalised so **each bucket sums to 1.0 within a given weight column**. Downstream you can pick one, blend them, or run all three through a back-test.

In the returns report (`scripts/bucket_returns.py`), whichever weight column is active is displayed under the header **"Index Weight"** in the constituent tables — a display relabel only; the underlying CSV columns are unchanged.

### `weight_cap_score`
`(market_cap × score) / sum(market_cap × score)` per bucket.
- **The finance-standard weighting.** Bigger companies get bigger weight, adjusted for how much of their revenue is thematic.
- **Caveat:** mega-caps dominate. In the pilot, NVDA hit ~52% of `fabless_chip_design` under this scheme; MSFT hit ~46% of `cybersecurity`. If that concentration bothers you, use `weight_score` instead.
- This is the **only** column the anchor-weight floor is applied to. Anchor tickers are guaranteed at least `anchor_min_weight` (default 5%) here.

### `weight_equal`
`1 / n` where n is the number of members in the bucket.
- **One share per member**, ignoring cap and score magnitude.
- Useful as a diversification benchmark against the other two schemes.

### `weight_score`
`score / sum(score)` per bucket.
- **Ignores market cap entirely.** Weight is purely proportional to LLM-estimated revenue share.
- Best for measuring pure thematic exposure without mega-cap distortion.
- AMD at score 1.0 and NVDA at score 0.75 would rival each other in `fabless_chip_design` regardless of their $300B vs $5T cap gap.

### `weight_cap_score_aug` (derived — not a CSV column)
`weight_cap_score` with a per-name cap of `m/N`, where N is the bucket's member count and m is `report.weight_cap_m` in `config.json` (default `3` — so an 11-name bucket caps any single name at 3/11 ≈ 27.3%).
- **Derived at render time** by the report from the existing `weight_cap_score` column. The frozen CSV has no such column and is never touched.
- Methodology (standard capped-index): cap every weight at `m/N`, redistribute the excess proportionally among uncapped members, repeat until no member exceeds the cap. Weights sum to 1.0 throughout. Feasible whenever `m >= 1` (the loader rejects `m < 1`).
- Select it via `report.weight_col: "weight_cap_score_aug"` (or `--weight-col`); the constituent tables' Index Weight column then shows the derived weights.

---

## Report-only columns and rows

The bucket-returns report adds a few things that exist only in the rendered HTML, not in the CSV.

### Sharpe column
An annualised Sharpe ratio, shown before the 1Y column on the main table (index level only — the constituent-level Sharpe column was removed in favour of the residual-z column) (header e.g. `3M Sharpe`).
- **Formula:** mean of daily simple returns ÷ sample standard deviation (ddof=1) × √252, risk-free rate 0.
- **Window:** configurable via `report.sharpe_window` in `config.json` (`1y`/`6m`/`3m`/`1m`/`1w`, default `3m`), using the same calendar-day lookbacks as the return columns.
- **Index-level Sharpe** uses the daily weighted return series with weights renormalised each day over members that priced.
- **Guards:** fewer than 10 daily returns, or (near-)zero standard deviation → the cell shows a dash.
- **Note on `1w`:** it is accepted by the validator for symmetry with the return windows, but a 7-calendar-day window yields ~5 daily observations — below the 10-observation guard — so every Sharpe cell would render a dash. Practical minimum is `1m`.

### Z column (v2.4)
Intra-bucket out/underperformance, before the 1Y column on the constituent tables (header e.g. `3M Z of Sub Idx Res`; window via `report.z_window`, default `3m`, same valid values and `1w` caveat as the Sharpe window).
- **Method:** each member's daily returns are regressed on its bucket's daily series **recomputed without the member** (leave-one-out — prevents heavy members regressing on themselves), the abnormal return over the window is the regression intercept × observations, and the z is a robust cross-sectional score within the bucket: (CAR − median) ÷ (1.4826 × MAD).
- **Tinting:** in buckets with more than 6 scored members, the top-3 rows by z are tinted green and the bottom-3 red. Smaller buckets show the column untinted.
- **Exclusions (dash):** penny-filtered members, fewer than 10 observations, degenerate buckets, or a collapsed MAD.
- **Caveat:** a high z reads as *leader* (momentum) or *stretched* (mean-reversion) depending on your prior — it is descriptive, not a signal; multi-horizon agreement is the disambiguator. A member of several buckets gets an independent z in each.

### Median-constituent line
Under each index return, a smaller figure gives the **unweighted median** of valid constituent returns for that window — same penny-filter and missing-price exclusions as the weighted number. The median stock may differ between windows; that's expected.

### Company search panel (v2.2)
The search box under the title covers **every LLM-scored company** — all 810 companies with rows in the DB's `latest_scores` view, not just index members. The panel shows all of a company's scores with **no floor applied**: member rows carry the active Index Weight and click through to their sub-index in the main table; below-floor rows are muted and tagged. Index members also get their price chart inline (non-members' prices aren't tracked, so no chart). If `data/minidex.db` is unavailable at render, search degrades to index members only (scores from the frozen CSV) with a stderr warning.

### Benchmark row
The main table's first row is the benchmark (`report.benchmark_ticker`, default `QQQ`), showing its returns and Sharpe on the same conventions. It is pinned: excluded from sorting, always first, visually tinted, not expandable, no median sub-line.

---

## The `config.json` report block

The report's knobs live in a `report` block in the root `config.json`: `sharpe_window`, `benchmark_ticker`, `weight_col`, `weight_cap_m`. Defaults apply if the block is absent. The `_valid_values` key inside the block is in-file documentation of what each key accepts (JSON has no comments); underscore-prefixed keys are ignored by the loader, and validation runs against lists hardcoded in `scripts/report_metrics.py` — editing `_valid_values` can't unlock new values. An invalid value exits loudly rather than rendering something silently wrong. Precedence: explicit CLI flag > `config.json` > built-in default.

---

## Rationale + provenance

### `rationale_run1`
The LLM's one-sentence justification for its score, from the first scoring run. Cites specific segment revenue lines, product mentions from Item 1, or applicable prompt rules.
- **Example:** *"NVIDIA's Compute & Networking segment ($193B) is predominantly GPU-based accelerated computing sold to data centres and AI customers, matching the fabless chip design definition."*

### `rationale_run2`
Same, from the second independent scoring run.
- Comparing `run1` vs `run2` shows where the model was internally consistent vs. where it wavered. QC flags any pair where the scores differ by more than `0.2`.

### `fy`
Fiscal year of the 10-K the scoring was based on.
- **Example:** `2025`
- Note: this is the company's fiscal year, which may differ from the calendar year of the pipeline run. NVDA's FY2025 10-K covers the year ending January 2025.

### `prompt_version`
Version of `prompts/scoring_prompt.md` that produced the row.
- **Example:** `1.6`
- Any material change to prompt rules or bucket definitions bumps this. Old and new versions coexist in the database; the CSV always takes the latest per `(cik, bucket_id)`.

### `model_version`
Anthropic model ID echoed from the API response.
- **Example:** `claude-haiku-4-5-20251001`
- **Deep-score list:** tickers in the `deep_score` list in `definitions/minidex_definitions.yaml` (seeded: INTC, AMD) bypass the embedding shortlist — they are scored against **all 22 buckets** — and run on the stronger `deep_score_model` (`claude-opus-5` in `config.json`) instead of the batch model, so their rows carry a different `model_version`. The list is reserved for diversified names whose Item 1 text is too broad for the similarity gate to ever surface them; it grants no anchor-style privileges (no QC expectation, no weight floor).
- **Cross-model caveat:** scores from different models are not perfectly calibrated against each other. Mitigations: the shared prompt, two-run averaging, and QC disagreement checks — and the deep list is not a general quality-upgrade lever. When deep-model rows are ingested, the same company's rows from other models at the same `prompt_version` are deleted, so `latest_scores` never mixes models for one company by string-ordering accident.

---

## How rows get selected

A row appears in the CSV only if **all** of these hold:

1. The `(cik, bucket_id)` pair is in the shortlist (the embedding similarity cleared the threshold, the ticker is a declared anchor for that bucket, or the ticker is on the `deep_score` list — which pairs it with every bucket).
2. Either the average of the two scoring runs is `>= score_floor` (currently `0.25` in `config.json`), **or** the ticker is a declared anchor. Anchors are force-included even at score `0.0`.
3. The row is the latest available for that `(cik, bucket_id)` — newest `fy`, then newest `prompt_version`, then newest `model_version`.

Weights are re-computed per bucket after this filtering, so `weight_cap_score`, `weight_equal`, and `weight_score` each sum to `1.0` within a bucket.

---

## See also

- `outputs/<asof>/manifest.json` — provenance record (as-of date, prompt SHA, definitions SHA, anchor-floor value, run date).
- `docs/ARCHITECTURE.html` §9 — the three weighting schemes in context, plus the mega-cap concentration discussion.
- `definitions/minidex_definitions.yaml` — full bucket definitions (`includes`, `excludes`, `anchors`).
- `prompts/scoring_prompt.md` — the scoring rules the LLM followed.
