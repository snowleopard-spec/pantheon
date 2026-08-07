# Pantheon v2.4 — Intra-Bucket Out/Underperformer Z-Scores

**Status:** draft for user review (H1 gate) · branch `v2.4-outperformers` on approval · tag `v2.4` on completion
**Brief:** user request 2026-08-07 — identify, on a purely price-time-series basis, the outperformers and underperformers of each sub-index using z-scores; Option C (beta-adjusted residual z) chosen from the three designs discussed. Additional display requirement: where a bucket has more than 6 constituents, tint the top-3 performers' rows green and the bottom-3 red.

## 1. Method (the statistic)

For each bucket, for each member, over a configurable lookback window:

1. **Leave-one-out bucket series.** The member's benchmark is its bucket's daily weighted return series **recomputed without the member** (weights renormalised over the remaining members, same daily-renormalisation rules as `bucket_daily_series`). Rationale: regressing a heavy member on an index containing itself biases beta toward 1 and shrinks its residuals — muting precisely the large names the feature should flag.
2. **OLS residuals.** Regress the member's daily returns on the LOO series (intercept + slope, closed-form — the established pattern from coherence's `residualize`). Keep the daily residuals.
3. **CAR.** Cumulative abnormal return = sum of daily residuals over the window (simple sum of dailies — a descriptive lens, not a compounded track record).
4. **Robust cross-sectional z.** Within the bucket:
   `z = (CAR_member − median(CARs)) / (1.4826 × MAD(CARs))`.
   Median/MAD rather than mean/std — bucket CARs have fat tails and one +900% name must not define everyone else's z. Guard: MAD below 1e-12 → all z blank.

**Exclusions** (member gets a dash, is excluded from the median/MAD and from ranking): fails the $1 window-start penny filter; fewer than `MIN_Z_OBS = 10` daily return observations in the window; or the LOO pool has < 2 members with data (degenerate bucket).

**Interpretation caveat (documented in the footer):** high z = leader (momentum reading) or stretched (reversion reading) — the report presents it descriptively and lets multi-horizon judgment disambiguate. A member of several buckets gets an independent z in each (relative performance is bucket-contextual; that is a feature).

## 2. Display

### 2.1 Z column
- New sortable column in the **constituent tables**, header e.g. `3M Z` (label derives from the configured window), placed between the Sharpe column and 1Y. Formatted `+1.35` / `−0.87` (2 dp, signed), same pos/neg colouring as returns; dash for excluded members.
- Main table, benchmark row, search panel: unchanged (search-panel z is a possible follow-up, out of scope).

### 2.2 Leader/laggard row tinting
- Eligibility: buckets where **n_z > 6** (members with a valid z — not nominal membership), so top-3 and bottom-3 can never overlap.
- Rank by z descending: top 3 rows get a **subtle green tinge**, bottom 3 a **slightly red tinge** — same understated register as the coherence report's row shading (no traffic-light saturation); hover states one notch lighter. Print stylesheet gets light-mode equivalents.
- The tint is a class on the `<tr>`, so it survives client-side sorting and chart-row insertion untouched.
- Buckets with n_z ≤ 6: column renders, no tinting.

## 3. Config

`report` block gains one key (+ `_valid_values` entry):

```json
"z_window": "3m"
```

Valid values identical to `sharpe_window` (`1y/6m/3m/1m/1w`); as with Sharpe, `1w` validates but yields ~5 obs < MIN_Z_OBS so everything dashes — documented, practical minimum `1m`. CLI is not extended (config-only knob; flags can be added later if ever needed). Tint parameters (top/bottom 3, n_z > 6) are hardcoded constants, not config.

## 4. File plan

| File | Change |
|---|---|
| `scripts/report_metrics.py` (edit) | New pure functions: `loo_bucket_series(weights, daily_rets, weight_col, bucket_id, exclude_ticker, weight_cap_m)` (or an efficient equivalent computing all LOO series per bucket in one pass) and `residual_car_z(weights, daily_rets, weight_col, weight_cap_m)` → `{bucket_id: {ticker: z | None}}`. `MIN_Z_OBS` exported. `load_report_config` gains `z_window` validation. |
| `tests/test_report_metrics.py` (edit) | New cases: planted alpha (member = 1×LOO-series + steady drift → strongly positive z); LOO correctness (heavy member's beta vs naive in-index regression differs in the expected direction); robust-z hand-check on a small CAR vector incl. one huge outlier; MAD≈0 guard; exclusion rules (penny, <10 obs, degenerate pool); `z_window` validation incl. loud rejection. |
| `scripts/bucket_returns.py` (edit) | Compute z map (second `daily_returns` panel when `z_window` ≠ `sharpe_window`); z column in `_constituent_table` with data-sort; top/bottom tint classes; CSS for the two tints (+ hover + print); footer method note; stdout unchanged. |
| `docs/Explainers/OUTPUT_COLUMNS.md` (edit) | Z column definition, tinting rule, exclusions, the momentum-vs-stretched caveat. |
| `docs/ARCHITECTURE.html` (edit) | One sentence in the §7 report description. |
| `docs/Context/PROGRESS_REPORT.md` | §19 build log at the usual cadence. |

No new files, no new dependencies, no pipeline/DB/serving changes. Droplet picks everything up via the existing nightly render once pulled.

## 5. Verification

- Unit tests above, offline; full suite stays at baseline + new.
- Real-data render: spot-check one bucket by hand (recompute a member's LOO beta, CAR and z independently from `prices.csv` + weights; values must match the rendered data-sort attributes).
- Headless-Chrome pass: for an eligible bucket, exactly 3 green + 3 red rows; tints survive sorting by any column and chart-row insertion; ineligible bucket (e.g. Foundry, n=4) has zero tinted rows; z column sorts correctly with dashes last both directions.
- Sanity: distribution of z across all buckets roughly centred on 0 (median ~0 by construction); no bucket where every member is excluded.

## 6. Milestones

- **M1 — this spec** committed on `v2.4-outperformers`. *Gate: user review (H1) — in particular the leave-one-out decision (§1.1), robust-z choice (§1.4), and tint placement (§2.2).*
- **M2 — compute**: report_metrics functions + tests green.
- **M3 — frontend**: column + tints, real-data render, hand-recomputation check, headless pass. *Gate: user eyeballs the report locally.*
- **M4 — docs + release**: OUTPUT_COLUMNS/ARCHITECTURE/PROGRESS_REPORT; merge, tag `v2.4`, droplet pull + refresh, phone check (H2).

## 7. Out of scope

- Options A (pure cross-sectional) and B (relative-strength time-series z) — B noted as the natural stat-arb follow-up.
- Z in the search panel or main table; any alerting/threshold logic; score-weighted variants.
- Making tint counts/eligibility configurable.
