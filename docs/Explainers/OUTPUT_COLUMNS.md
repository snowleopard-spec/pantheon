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
- Companies below the `score_floor` (default `0.10`) are excluded from the bucket, unless they're an anchor (see below).

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

---

## How rows get selected

A row appears in the CSV only if **all** of these hold:

1. The `(cik, bucket_id)` pair is in the shortlist (either the embedding similarity cleared the threshold, or the ticker is a declared anchor for that bucket).
2. Either the average of the two scoring runs is `>= score_floor` (default `0.10`), **or** the ticker is a declared anchor. Anchors are force-included even at score `0.0`.
3. The row is the latest available for that `(cik, bucket_id)` — newest `fy`, then newest `prompt_version`, then newest `model_version`.

Weights are re-computed per bucket after this filtering, so `weight_cap_score`, `weight_equal`, and `weight_score` each sum to `1.0` within a bucket.

---

## See also

- `outputs/<asof>/manifest.json` — provenance record (as-of date, prompt SHA, definitions SHA, anchor-floor value, run date).
- `docs/ARCHITECTURE.html` §10 — the three weighting schemes in context, plus the mega-cap concentration discussion.
- `definitions/minidex_definitions.yaml` — full bucket definitions (`includes`, `excludes`, `anchors`).
- `prompts/scoring_prompt.md` — the scoring rules the LLM followed.
