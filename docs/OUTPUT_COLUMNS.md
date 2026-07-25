# Output columns — `outputs/<asof>/minidex_weights.csv`

One row per `(bucket_id, ticker)` membership. Every column below appears in both the CSV and the Parquet file, in this order.

| Column | Type | Description |
|---|---|---|
| `bucket_id` | string | Stable slug identifying the mini-dex bucket. Matches the `id:` field in `definitions/minidex_definitions.yaml`. Example: `fabless_chip_design`. |
| `bucket_name` | string | Human-readable display name for the bucket. Example: `Fabless chip design`. Same source, `name:` field. |
| `ticker` | string | Exchange ticker symbol (uppercase). Example: `NVDA`. |
| `cik` | string | SEC Central Index Key, 10-digit zero-padded. Uniquely identifies the filer at EDGAR. Example: `0001045810`. |
| `score` | float 0.0–1.0 | LLM's estimated fraction of the company's revenue attributable to this bucket's defined activity, averaged across two independent scoring runs. `0.75` = ~75% of revenue. |
| `confidence` | enum | `high`, `medium`, or `low`. The **minimum** confidence across the two runs. `low` typically means the LLM was reasoning from Item 1 text alone with no segment data, or the company is pre-revenue (see Rule 5 in the scoring prompt). |
| `market_cap` | float (USD) | Market capitalisation in dollars, computed at fetch time as `common_shares_outstanding × yfinance_last_close`. May be null for some foreign filers where yfinance had no data. |
| `weight_cap_score` | float 0.0–1.0 | Cap-weighted × score, normalised so this bucket's members sum to 1.0. The finance-standard weighting. Mega-caps like NVDA and MSFT can dominate buckets under this scheme. This is the only column the anchor-weight floor (§9 of `ARCHITECTURE.html`) is applied to. |
| `weight_equal` | float 0.0–1.0 | `1 / n` where n is the number of members in the bucket after the score floor. Equal-weight benchmark; ignores both cap and score magnitude. |
| `weight_score` | float 0.0–1.0 | Score-only weighting, normalised per bucket. Ignores market cap — weight is purely proportional to LLM-estimated revenue share. Best for measuring pure thematic exposure without mega-cap distortion. |
| `rationale_run1` | string | The LLM's one-sentence justification for this score on the first independent run. Cites specific revenue lines, segment data, or business-description evidence. |
| `rationale_run2` | string | Same, from the second independent run. Comparing `run1` vs `run2` shows where the model was internally consistent vs. where it wavered. QC flags any pair whose scores differ by more than 0.2. |
| `fy` | integer | Fiscal year of the 10-K the scoring was based on. Example: `2025`. May differ from the calendar year of the run. |
| `prompt_version` | string | Version of `prompts/scoring_prompt.md` used for this row. Example: `1.6`. Any material change to prompt rules or bucket definitions bumps this. Old and new versions coexist in the DB; the CSV takes the latest per `(cik, bucket_id)`. |
| `model_version` | string | Anthropic model that produced the score, echoed from the API response. Example: `claude-haiku-4-5-20251001`. |

## How rows are selected

A row appears in the CSV only if all of these hold:

1. The `(cik, bucket_id)` pair is in `shortlist` (cleared the embedding similarity threshold or was force-included as an anchor pair).
2. The average of the two scoring runs is `>= score_floor` (default `0.10` from `config.json`) **OR** the ticker is a declared anchor for the bucket in the YAML.
3. The row is the latest available for that `(cik, bucket_id)` (newest `fy`, then newest `prompt_version`, then newest `model_version`).

Weights are always normalised so each `bucket_id` sums to 1.0 within its weight column.

## See also

- `outputs/<asof>/manifest.json` — provenance record (as-of date, prompt SHA, definitions SHA, anchor-floor value, run date).
- `docs/ARCHITECTURE.html` §10 — the three weighting schemes in context.
- `definitions/minidex_definitions.yaml` — full bucket definitions with `includes`/`excludes`/`anchors`.
- `prompts/scoring_prompt.md` — the scoring rules the LLM followed.
