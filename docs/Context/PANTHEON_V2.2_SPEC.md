# Pantheon v2.2 — Company Search Specification

**Status:** decisions resolved 2026-08-06 · branch `v2.2-search` · tag `v2.2` on completion
**Brief (verbatim, from the rolling `docs/Context/New Prompt.md`):**
> At the top of the page under the title, I would like a search box, where the user can search for a company. As the user types, I would like there to be an autofill suggestion based on the set of all companies within the sub indices. Once a name is selected a panel drops down. Centre aligned, which shows the searched stock, and all its scores > score floor, for each of the sub-indices.

Amended by the user at review: include both proposed enrichments (click-through to the sub-index, inline price chart) and **drop the floor for this purpose — show all scores**.

## 1. Resolved decisions

| # | Decision |
|---|---|
| D1 | **Search universe = all 810 scored companies** (every company with a row in the DB's `latest_scores` view — 3,387 (company, bucket) score pairs), not just the 340 index memberships. This is the natural consequence of "show all scores": a company that scored below the floor everywhere is findable, and the panel shows why it's in no index. |
| D2 | **Score source = `latest_scores`** (the two-run average the index build itself uses), read from `data/minidex.db` at render time. Verified: the droplet's DB snapshot is identical on these counts (3,387 / 810), so both machines render the same search. **Graceful degradation:** if the DB is unavailable, fall back to a weights-CSV-only payload (members and their frozen scores) with a loud stderr warning — the report must never fail to render because search data is missing. |
| D3 | **Membership vs below-floor display:** each panel row shows sub-index name, score (2 dp), confidence, and Index Weight. Weight comes from the active weight column for buckets where the company is a member; below-floor rows show a muted dash and a "below floor" tag. Rows sort by score descending. |
| D4 | **Enrichment 1 (click-through):** clicking a member sub-index row in the panel scrolls to that sub-index in the main table, expands it, and flashes a highlight. Works for below-floor rows too (scroll + expand — the company just won't be in the constituent list). |
| D5 | **Enrichment 2 (chart):** the panel embeds the company's price chart, reusing the existing `PRICES` payload and uPlot chart builder (period buttons included) at zero added payload. Charts exist only for index members (prices are pulled for weights tickers + benchmark) — non-member panels show the scores table without a chart. Deliberate: extending the daily price pull to all 810 scored tickers (~2.6× Polygon calls, ~4 MB page) is not worth it for companies we don't track. |
| D6 | **Self-contained constraint holds:** vanilla-JS autocomplete over the embedded payload (~200 KB JSON). No new dependencies of any kind. |
| D7 | Single-wave, single-owner build (all code edits in `scripts/bucket_returns.py`); verified with the established headless-Chrome click harness; docs touched: `OUTPUT_COLUMNS.md`, `ARCHITECTURE.html` (one paragraph each), PROGRESS_REPORT §16. Branch `v2.2-search` → merge → tag `v2.2` → droplet refresh → phone check. |

## 2. UI specification

- **Search box** centred directly under the "Updated …" line: dark-themed input, placeholder "Search a company…". As the user types (≥1 char), a dropdown lists up to 8 matches — ticker-prefix matches ranked first, then company-name substring matches — each showing ticker + name (+ a muted "not in any index" tag where applicable). Keyboard: ↑/↓ to move, Enter to select, Esc to close; click-away closes.
- **Panel** (centre-aligned card below the search box, ✕ to close, replaced on a new selection):
  - Header: ticker, company name, market cap.
  - Chart (members only): the standard uPlot chart with 1Y–1W period buttons.
  - Scores table: one row per scored sub-index (all of them, no floor), columns Sub-Index · Score · Conf · Index Weight, sorted by score descending; member rows clickable per D4; below-floor rows muted with a "below floor" tag.

## 3. Data payload

Embedded as a `SEARCH` JSON blob beside `PRICES`:
```
{ "floor": 0.25,
  "buckets": {bucket_id: display_name, …},
  "companies": [ {"t": ticker, "n": name, "m": market_cap|null,
                  "s": [[bucket_id, score, conf, weight_pct|null], …]}, … ] }
```
Built in `bucket_returns.py` from `latest_scores` + `companies` (DB) joined against the weights frame for membership/weight; `floor` is read from the top-level `score_floor` config key for the "below floor" tagging.

## 4. Out of scope

- Extending the price pull / charts to non-member companies (D5).
- Searching companies never scored (the 6,900 filtered-out universe) — no useful data to show.
- Rationale text in the panel (available in the DB; would add ~1.5 MB of prose — revisit if wanted).
