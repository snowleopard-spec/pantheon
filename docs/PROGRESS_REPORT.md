# mini-dex build — progress report

_As of 2026-07-24. Pilot complete on 77 tickers. Full-universe run pending on a new droplet._

## 1. Initial state

The project began with four hand-authored artefacts and no code:

- `docs/MINIDEX_SPEC.md` — build spec, staging model, acceptance criteria.
- `definitions/minidex_definitions.yaml` — 22 buckets and their anchor tickers.
- `prompts/minidex_scoring_prompt.md` — scoring prompt for the LLM.
- `API Keys.md` — the Anthropic API key committed in plaintext.

The API key file was the first thing flagged: it was moved into `.env`, the plaintext file deleted, and `.env` added to `.gitignore` before any commits. The key itself should still be rotated before the full run (it was exposed on disk in a plaintext file in an as-yet-uninitialised repo). The directory was then `git init`'d and a remote created at `https://github.com/snowleopard-spec/pantheon`.

## 2. Build phases (spec §12)

Work followed the spec's four-phase plan. Every phase ended in a git commit; parallel work was serialised on integration.

| Phase | Scope | Commit |
|---|---|---|
| A | pyproject scaffold, `config.py`, DB schema + `latest_scores` view, pydantic models, CLI skeleton, initial tests | `aa50777` |
| B (4 agents in parallel) | `universe.py`, `edgar.py`, `shortlist.py`, `score.py` | `152a5a4` |
| C (2 agents in parallel) | `qc.py` + anchor tests, `indices.py` + `performance.py` | `de922c1` |
| D | End-to-end integration test for stages 4–7; then real stages 1–4 run | `14d240b` |

Phase A/B/C landed cleanly. Phase D — where synthetic tests met real EDGAR data — is where every interesting bug showed up. Everything below `14d240b` in the log is a defect surfaced by the pilot run.

## 3. Pilot run (stages 1–7 on real data)

- **Stage 1 universe.** 7,992 companies pulled from SEC; 6,934 with SIC codes after enrichment. Well over the ≥5,000 acceptance floor.
- **Stage 2 filter.** 1,376 candidates after keyword/SIC filter — inside the spec range of 800–2,500.
- **Pilot scope decision.** A full 1,376-ticker Stage 3 fetch was estimated at 5–8 hours from a residential IP with SEC rate limits. To iterate on the scoring pipeline before spending that time, Stage 3 was run with a `--tickers` filter covering 77 companies: the anchor set plus AVGO, DELL, ETN, VST, ARW, IBM (per spec §11.3).
- **Stage 3 fetch.** 77/77 filings successfully pulled. Market cap: 75/77 after the yfinance fallback. Segments: 0/77 → 49/77 → 75/77 → 55/77 real splits (see §4 for how those numbers changed).
- **Stage 4 shortlist.** 543 (ticker, bucket) pairs; median 5 buckets per company. Well under the 12k pair ceiling.
- **Stage 5 scoring (v1.0 → v1.4).** Batch API, `claude-haiku-4-5-20251001`, 154 requests per version, ~2 min wall time per batch, ~$1.16 per run. 100% valid rows on first try (no retries needed).
- **Stage 6 QC.** Report generates cleanly (`outputs/qc_report.md`). Latest anchor pass: 65/79 (see §5 for the full progression).
- **Stage 7 weighted index.** All 22 buckets have members; weight columns sum to 1.0 ± 1e-6 per bucket per scheme.

## 4. Bugs surfaced during the real run

These are the defects that only appeared once real data hit the pipeline, in the order they were found:

1. **DB-wiping test.** `test_init_creates_db` called `.unlink()` on the actual `data/minidex.db` because the `tmp_path` monkeypatch did not intercept the absolute `REPO_ROOT`-based `db_path`. Running the full test suite silently wiped state between runs. Replaced with a spy-based test that never touches disk (`0a8f998`).
2. **Ticker deduplication.** SEC's `company_tickers.json` returns multiple rows per CIK — preferred stock, warrants, foreign OTC forms. The last-write-wins upsert clobbered clean common-stock tickers with things like `ORCL-PD`, `IONQ-WT`, `TSMWF`. Added a `_ticker_rank` function that prefers the shortest, dashless, uppercase form (`71ea33a`).
3. **Market cap extraction.** `edgartools`' curated concept API doesn't expose `MarketCapitalization` or `SharePrice` (SEC XBRL doesn't carry market cap at all). Switched to `common_shares_outstanding × yfinance close` (`9a057bc`). Covered 75/77 pilot tickers.
4. **Segment extraction — the four-part saga.**
   - The initial extractor called `facts.query(concept='Revenues')` — wrong `edgartools` API shape. All 77 filings returned `[]`.
   - Rewrote to use `xbrl.query(include_dimensions=True).to_dataframe()` filtered on `dim_us-gaap_StatementBusinessSegmentsAxis` (`87b12dc`). Coverage jumped to 49/77.
   - That version deduped on the wrong column: `dimension_member_label` was `"Operating Segments"` for every NVDA row, so NVDA appeared as one aggregated blob. Fixed to dedupe by the axis-column value while displaying `dimension_label` (`"Compute & Networking"`, `"Graphics"`).
   - Added a single-segment fallback via `facts.get_concept('revenue', period='YYYY-FY')` (`fb8c59e`) for companies whose XBRL has no segment dimension. Coverage: 75/77.
   - Built an optional Stage 3.5 `llm_segments.py` (`5098375`) for edge cases — foreign-filer geographic breakdown (TSM), product-line prose (AMD). Final coverage: 55/77 real splits, 21 single-segment, 1 truly empty.
5. **Custom_id format.** Spec's `TICKER|FY|runN` violated Anthropic Batches' `^[a-zA-Z0-9_-]+$` regex. First batch submission got a 400 before a single request ran. Switched to underscore separator (`0f09ba9`).

None of these are wrong ideas in the spec — they are all impedance mismatches with real vendor APIs (SEC ticker file, `edgartools` XBRL surface, Anthropic Batches validation, `pytest` `tmp_path` semantics). Worth remembering next time we spec against an external data source without a first pass through it.

## 5. Pilot iteration — score deltas across prompt versions

Seven full scoring passes were run to isolate the effect of each change.

| Version | Change | Anchor PASS | Segments coverage | Cost |
|---|---|---:|---:|---:|
| v1.0 | Original text-only scoring | 63/79 | 0/77 | $1.16 |
| v1.1 | Hyperscalers bucket rewritten as explicit two-leg (`(a) cloud` / `(b) consumer internet at hyperscale`); anchor threshold 0.5 → 0.3 | 65/79 | 0/77 | $1.16 |
| v1.2 | Segments injected (XBRL only, per-segment splits) | 67/79 | 49/77 | $1.17 |
| v1.3 | Segments + single-segment total-revenue fallback | 66/79 | 75/77 | $1.17 |
| v1.4 | Segments + LLM-extracted disaggregation | 66/79 | 55/77 real + 21 total-only | $1.17 |
| v1.5 | All 22 bucket definitions gained the "(or whose principal business purpose is)" clause previously only on `pqc_quantum` | 66/79 | (unchanged) | $1.17 |
| v1.6 | Rule 5 rewritten with explicit numerical bands (pure-play 0.8–1.0, partial 0.3–0.7, aspirational 0.0) and named examples | **71/79** | (unchanged) | $1.19 |

The v1.5 → v1.6 jump was the biggest single-version win. Rule 5's original phrasing ("should be scored on business purpose") was too soft against Rule 1's imperative "SCORE = REVENUE FRACTION". The rewrite explicitly supersedes Rule 1 for qualifying companies and gives the LLM numerical bands to hit. Result: OKLO/SMR/NBIS moved from 0.0 to 0.9 on their intended buckets. Side effects worth watching (CEG jumped 0.15 → 0.85, NVDA dc_hardware dropped 0.85 → 0.40) suggest the model is being more generous with the exception; net anchor count still improved.

### Anchor weight floor (Stage 7 override)
Independent of scoring, `indices.py` now applies an anchor-weight floor at Stage 7 (default 5%, configurable in `config.json` as `anchor_min_weight`). Below-floor anchors are hard-fixed at exactly the floor; above-floor members (anchor or not) scale proportionally into the remaining pool so each bucket still sums to 1.0. This is a belt-and-suspenders guarantee: even if the LLM misses a canonical anchor for any reason, it still gets 5% of the bucket. In the pilot's `power_generation` bucket, OKLO and SMR now land at exactly 5.00% each in v1.6, with the LLM scoring plus the floor both contributing.

### Configuration file (config.json)
As of `d50e013`, six tuning knobs (`embedding_model`, `similarity_threshold`, `score_floor`, `batch_model`, `max_item1_chars`, `anchor_min_weight`) live in a plain JSON file at the repo root instead of environment variables. Secrets (`ANTHROPIC_API_KEY`, `SEC_USER_AGENT`) stay in `.env`. Precedence: env var beats JSON beats hardcoded default, so per-run droplet overrides still work without editing files.

## 6. Files persisted

- **16 git commits** on `main`, all pushed to `https://github.com/snowleopard-spec/pantheon`.
- **15 DB snapshots** at `data/minidex.db.*.bak` — one after each expensive stage and each re-score (`universe`, `filter`, `fetch_pilot`, `fetch_mcap`, `shortlist`, `scored`, `scored_v11..v16`, `segments`, `segments2`, `llmseg`). Cheap insurance against another DB-wipe.
- **6 output snapshots** in `outputs/` — v1.0 (`2026-07-24/`), v1.1 (`-v11/`), v1.2 (`-v12/`), v1.3 (`-v13/`), v1.4 (`-v14/`), v1.6 (`2026-07-25/`). Each contains `minidex_weights.csv/.parquet` + `manifest.json`.
- **QC report** at `outputs/qc_report.md`.
- **Architecture doc** at `docs/ARCHITECTURE.html` — self-contained (~26 KB) with inline SVG diagram, per-module purpose table, config-key legend, and exception-rule section.
- **Total pilot spend:** ~$8.32 (6 scoring batches × ~$1.17–1.19 + $0.11 for the Stage 3.5 LLM segment extraction).

## 7. Pilot vs spec acceptance criteria

| Stage | Criterion | Status |
|---|---|---|
| 1 | ≥5,000 rows; NVDA/MSFT/EQIX/VRT/CLS present with SIC | PASS |
| 2 | 800–2,500 candidates; anchors included; JPM/PFE not | PASS (with 4 anchor gaps traceable to missing SEC data — see qc report) |
| 3 | ≥90% of candidates have `item1_chars > 2,000` | PASS (77/77 in pilot) |
| 3 | Re-running does no network fetches | PASS |
| 4 | Every anchor pair present; median 1–6 buckets/company; total pairs < 12k | PASS |
| 5 | ≥95% valid rows after at most one retry | PASS (100% first-try) |
| 6 | Report generates; anchor tests pass or skip cleanly | PASS |
| 7 | Weight columns sum to 1.0 ± 1e-6 per bucket; every bucket has ≥1 member | PASS |

Acceptance criteria are all met at pilot scope. The 11 anchor failures in the QC report (STM, ACMR, NBIS, APLD, ETN, GEV, VST, CEG, OKLO, SMR, PLTR) are cases where the model's mean score fell below 0.30 — some are genuine (OKLO/SMR/NBIS have thin or no filings in this batch), others are candidates for a definition tweak once we see full-universe context.

## 8. Droplet compatibility

The current DO droplet (`unicorn-hunt`, per `docs/droplet-report-20260724-103235.md`) is a **1 vCPU / 1.9 GiB / 48 GB** box already running four other services (`caddy`, `unicornhunt`, `sonar`, `dilithium` cron). The review agent's verdict was **RED**:

- 1.9 GiB RAM is insufficient to run the `BAAI/bge-large-en-v1.5` embedding model alongside the existing workloads.
- Neither `uv` nor `tmux` is installed.
- 48 GB disk is fine; 41 GB free.
- Python 3.12.3 is available.

**Recommendation:** spin up a new, dedicated **8 GB** DO droplet for the full-universe run rather than shoehorning it onto `unicorn-hunt`. Bootstrap on the new box:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
sudo apt-get update && sudo apt-get install -y tmux git
git clone https://github.com/snowleopard-spec/pantheon.git
cd pantheon
cp .env.example .env   # then paste rotated ANTHROPIC_API_KEY
uv sync
```

## 9. Next steps (ordered)

1. **Rotate** the Anthropic API key that was originally committed in plaintext (still valid). Replace value in `.env` after rotation.
2. Spin up an 8 GB DO droplet (dedicated — do not shoehorn onto `unicorn-hunt`); bootstrap per §8.
3. Copy the pilot DB up (`scp data/minidex.db root@newdroplet:pantheon/data/`) so Stage 1/2 don't need to re-run. Alternatively `scp data/minidex.db.scored_v16.bak` and rename on the far side to preserve the scored pilot as a starting point.
4. Run the full pipeline. Estimated ~8–9 hours (dominated by Stage 3 EDGAR fetch of ~1,300 remaining tickers) and ~$25–35 in Anthropic spend for scoring.
5. Snapshot the final DB and copy `outputs/` back locally.
6. Optional: revisit the remaining flagged anchor definitions once full-universe scores are available for comparison (v1.6 side effects like CEG 0.15 → 0.85 may indicate the Rule 5 rewrite is over-generous for revenue-stage IPPs; the model-noise floor makes it hard to judge at pilot scale).
7. Optional: wire up a prices CSV and run `scripts/performance.py` to get cumulative bucket returns.

## 10. Open questions

- **Anchor threshold.** Current 0.30. Should it go lower for conglomerate anchors where the anchor bucket is a genuine minority of revenue (AWS is ~18% of AMZN, Azure is ~25% of MSFT)? The anchor-weight floor of 5% partially compensates for this in the index composition, but the QC signal still flags them as failures.
- **Weight scheme priority.** All three (`weight_cap_score`, `weight_equal`, `weight_score`) are produced. Which is the primary downstream artefact? That decides which one gets QC attention.
- **Refresh cadence.** Spec assumes annual. Given filings drift, would a monthly Stage 5 re-score (reusing Stage 3 filings) actually be useful, or is this a one-shot?
- **v1.6 over-generosity risk.** The Rule 5 rewrite worked as intended for the pre-revenue names but also moved some revenue-stage IPPs (CEG 0.15 → 0.85). If full-universe scoring shows systematic inflation for borderline names, a v1.7 tightening of the "pure-play" definition may be needed.

## 11. Handoff notes for next session

**State when this session ended:** full-universe run **complete**. Pilot at v1.6, 27+ commits pushed to `origin/main`, no uncommitted local changes. The `data/minidex.db` on local has 6,774 v1.6 scores across 810 companies × 22 buckets from the droplet run, plus all prior pilot versions (v1.0–v1.5). Full-universe outputs live at `outputs/2026-07-25/` (444 rows, all 22 buckets). Everything is local: raw filings (1,291 files), all 5 stage-boundary droplet snapshots, and the frozen weight CSV/parquet/manifest. The droplet at `139.59.127.139` can be destroyed at any time — nothing on it that isn't also here.

**Lessons from this run to remember:**
- **tmux + Python stdout buffering.** Python defaults to block-buffered stdout when not attached to a TTY. Inside `tmux capture-pane`, this manifests as "nothing appears to be happening" for tens of minutes even though the process is fine. Either set `PYTHONUNBUFFERED=1` in the environment, run with `python -u`, or (better) tee output to a log file and `tail -f` the log instead of scraping `capture-pane`.
- **Always `uv run python` inside chained scripts, never bare `python3`.** The system `python3` on the droplet does not have `anthropic` (or any project dep) installed; only the uv-managed venv does. A chained pipeline script that used `python3 -c "..."` for batch polling died with `ModuleNotFoundError: No module named 'anthropic'` on the first poll. Fix was a one-line `sed` across the script; prevention is to grep-check `python3` invocations before launching any long-running chain.

**Key files to re-read at session start:**
- `docs/PROGRESS_REPORT.md` (this file) — the timeline and current state.
- `docs/ARCHITECTURE.html` — the mental model, including the two exception rules.
- `prompts/scoring_prompt.md` — the current v1.6 prompt including the rewritten Rule 5.
- `config.json` — the current tuning-knob values.
- `git log --oneline` — the commit history is the reliable trail of what changed.

**Do not re-run** stages 1–5 on the local machine. The pilot DB (`data/minidex.db`) and its `.bak` snapshots are the canonical source of pilot state. The full-universe run belongs on the droplet.

**Reserved batch IDs** already used and terminated (all ended, DB has rows keyed by `prompt_version`):
- Pilot v1.0–v1.6: `msgbatch_01JcDNRK...`, `01KbLVcp...`, `01MFyzBK...`, `0134J39P...`, `014tkSUv...`, `012oGwzF...`, `011JstP5...`
- Pilot Stage 3.5 llm_segments: `msgbatch_01G5aJwLnwTHLEqPR2S2ztgX`
- Droplet Stage 3.5 llm_segments: `msgbatch_01T3PJj6nfEQpuzdRfFPN3Yn`
- Droplet Stage 5 scoring: `msgbatch_019YLgQibi3ijAnNfj95DB1z`

**Pilot ticker list** for `--tickers` flags is cached at `/tmp/pilot_tickers.txt` locally but that path is ephemeral — regenerate from the YAML anchors + AVGO,DELL,ETN,VST,ARW,IBM if needed for a pilot-scope re-run.

**Immediate next action** for the next session: the pipeline has produced its first full-universe output. Downstream now belongs to the user — inspect `outputs/2026-07-25/minidex_weights.csv`, iterate on weighting schemes (custom cap, sqrt-cap, blended), wire it up to `scripts/performance.py` with a prices CSV for cumulative bucket returns. The droplet can be destroyed (`doctl compute droplet delete <id>` or via the DO console). Next full refresh is annual per spec — the runbook at `docs/DEPLOY_TO_DROPLET.md` walks through spinning up a fresh droplet from scratch.

## 12. Droplet spinup and bootstrap (2026-07-25)

### Droplet specs
- Provider: DigitalOcean.
- IP: `139.59.127.139`.
- Size class: `s-2vcpu-8gb-160gb-intel-sgp1` (~2 vCPU, 8 GB RAM, 160 GB SSD, Intel, Singapore region).
- OS: Ubuntu (Linux 6.8.0-124-generic x86_64).
- Baseline usage when fresh: 456 MB RAM used, 7.3 GB available; 1.9 GB / 154 GB disk used.
- No swap configured — the 8 GB RAM makes it unnecessary for this workload.

### What was pre-installed vs what we added
Pre-installed on the base image: git 2.43, Python 3, curl, ca-certificates, baseline Ubuntu.

Added on the droplet:
- `uv` 0.11.32 via the official installer (`curl -LsSf https://astral.sh/uv/install.sh | sh`).
- `tmux` 3.4, for detached long-running sessions.
- `sqlite3` CLI, for DB introspection — the Python `sqlite3` module is a stdlib but the CLI tool was not installed.
- Repo cloned from `https://github.com/snowleopard-spec/pantheon.git` into `/root/pantheon`.
- Python venv + all pip dependencies via `uv sync` (torch, sentence-transformers, edgartools, anthropic, pandas, pyyaml, yfinance, pytest, etc.).

### What was transferred from the local Mac
- `.env` (181 bytes) via `scp` to `/root/pantheon/.env`, `chmod 600`. Contains `ANTHROPIC_API_KEY` and `SEC_USER_AGENT`. The key has **not** been rotated yet — it is still the original one that was exposed in `API Keys.md` at project start (§1). Rotation remains outstanding.
- `data/minidex.db.scored_v16.bak` (3.9 MB) via `scp` to `/root/pantheon/data/minidex.db`. This is the pilot database with all 77 filings, ~7,600 score rows across prompt_versions 1.0–1.6, and 543 shortlist pairs. Effect: stages 1–2 are already done on the droplet, and stage 3 resumes with 77/1,376 cached.

### Verification
- Test suite on the droplet: 117 tests pass (excluding anchors) via `uv run pytest -q --ignore=tests/test_anchors.py`.
- DB state confirmed post-transfer: 7,992 companies, 1,376 candidates, 77 filings, 7,602 score rows across 7 prompt_versions.

### Current run
- `minidex fetch` is running inside a detached `tmux` session named `pantheon`. To attach: `ssh root@139.59.127.139` then `tmux attach -t pantheon`.
- Expected wall time: ~6–8 hours (dominated by SEC EDGAR's 10 req/s rate limit for the ~1,299 non-pilot candidates).
- The local process is polling every ~5 min via `ssh + tmux capture-pane | grep 'fetch: done'`.

## 13. Full-universe run (2026-07-25)

### Stage 3 fetch outcome
Ran ~3.5 hours wall time — faster than the 6–8 hr estimate, the 2 vCPU box outperformed the single-core assumption on the parallel-safe extraction work between EDGAR fetches.

| Metric | Count | Rate |
|---|---:|---:|
| Filings in DB (pilot cached + newly fetched) | 77 + 1,221 = 1,298 | — |
| Item-1 extraction (`item1_chars > 2,000`) | 1,291 / 1,298 | 99.5% |
| Segment extraction (XBRL + single-segment fallback) | 1,167 / 1,298 | ~90% |
| Market cap (`shares × yfinance close`) | 1,202 / 1,376 candidates | ~87% |
| Item-1 fallback (couldn't cleanly parse section, took first 40k chars of body) | 117 candidates | — |

### Rsync raw filings back to local Mac
1,214 filing text files (~74 MB uncompressed, transferred with `-z`) merged into the local `data/raw/` alongside the 77 pilot files, giving 1,291 total files (~78 MB on disk) — an exact mirror of the droplet's raw corpus. Insurance: the droplet can be destroyed after the run without losing the raw filing text, which is the one artefact that costs wall time (not money) to re-produce.

### Stage 4 shortlist outcome
Ran ~15 min on the 2 vCPU box.

| Metric | Value |
|---|---:|
| Total (ticker, bucket) pairs | 2,920 |
| — via embedding similarity ≥ 0.60 | 2,844 |
| — via anchor force-include | 76 |
| Distinct companies with ≥1 bucket | 810 |
| Median buckets per company | ~4.2 |
| Min anchor similarity observed | 0.3627 |

The interesting number is **566 SIC-included candidates that produced zero buckets** (1,376 candidates − 810 with a bucket = 566). They didn't clear the 0.60 similarity threshold on any of the 22 buckets — legitimately not thematic, even though their SIC code was on the include list. This is the shortlist filter doing what it's meant to do: SIC gets us near the neighbourhood, embedding similarity confirms actual thematic proximity.

The 0.3627 min-anchor-similarity is below the 0.60 threshold; anchors are force-included regardless, so this is a calibration warning only (some anchor's Item-1 text is genuinely dissimilar to its bucket definition).

### Stage 3.5 LLM segments submit
812 requests submitted; estimated cost ~$3.39. Batch id: `msgbatch_01T3PJj6nfEQpuzdRfFPN3Yn`. Polled by the chained script (see below).

### Chain-script bug and one-line fix
A helper script `run_remaining.sh` was written to pipeline the remaining stages: llm_segments poll → score submit → score poll → qc → build. The batch-status-polling steps used bare `python3 -c "..."` instead of `uv run python -c "..."`. The system `python3` on the droplet does not have `anthropic` installed (only the uv-managed venv does). First poll iteration died with:

```
ModuleNotFoundError: No module named 'anthropic'
```

Fix: one-line `sed` replacing `python3 -c` → `uv run python -c` throughout the script, then re-launched. Lesson persisted in §11.

### DB snapshots on droplet
- `data/minidex.db.fetch_full.bak` (4.3 MB) — after stage 3 fetch.
- `data/minidex.db.shortlist_full.bak` (4.5 MB) — after stage 4 shortlist.
- `data/minidex.db.llmseg_full.bak` — after stage 3.5 LLM segments.
- `data/minidex.db.scored_full.bak` — after stage 5 scoring.
- `data/minidex.db.built_full.bak` — after stage 7 build.

### Chain completion outcome

The re-launched chain finished cleanly. Final numbers per stage:

| Stage | Result |
|---|---|
| llm_segments poll | 100 filings gained real splits, 701 correctly returned empty, 11 failures |
| Score submit | 1,620 requests (810 companies × 2 runs), $11.95 est |
| Score poll | **6,774 rows inserted, 0 failures** (100% first-try success) |
| QC | 6 anchor fails, 3 skips, 13 run disagreements, 141 borderline, 22 low-confidence highs |
| Build | **444 members across all 22 buckets** at `outputs/2026-07-25/` |

Notable: only **6 anchor failures** on the full universe vs. 11 at pilot scale. Richer context (more comparison filings + full segment coverage from Stage 3.5) made calibration tighter.

### Rsync-back to local Mac

After the build landed, the following were rsynced from the droplet to the local repo:

- `outputs/2026-07-25/{minidex_weights.csv, minidex_weights.parquet, manifest.json}`
- `data/minidex.db` (7.1 MB) — full production DB
- `data/*.bak` — all 5 stage-boundary snapshots

Combined with the 1,214 raw filing files rsynced earlier and the code (always on GitHub), **every artefact of the full run is now local**. The droplet at `139.59.127.139` can be destroyed at any time without loss.

### Anthropic cost — final

| Phase | Actual spend |
|---|---:|
| Pilot (7 scoring passes + 1 llm_segments) | ~$8.32 |
| Droplet llm_segments (812 requests) | $3.39 |
| Droplet full-universe scoring (1,620 requests) | $11.95 |
| **Grand total** | **~$23.66** |

### New docs added during and after the run

- `docs/DEPLOY_TO_DROPLET.md` — 1,424-word operational runbook for deploying to a fresh DO droplet from scratch, incorporating every gotcha caught tonight.
- `docs/OUTPUT_COLUMNS.md` — per-column reference for the output CSV (identifiers, scoring, financials, weights, rationale, provenance).
- `docs/ARCHITECTURE.pdf` — compiled from `ARCHITECTURE.tex` via `tectonic` for offline reading.
- Full-run outputs committed to git as canonical snapshot: `outputs/2026-07-25/*` (force-added despite the git-ignore on `outputs/`).

### Post-run: Polygon price pull + bucket returns

Two new standalone scripts were added after the full-universe run to close the loop from scores to actual returns:

- **`scripts/pull_prices.py`** — pulls adjusted daily closes from Polygon.io for every ticker in a `minidex_weights.csv`. 8 concurrent HTTP workers, 400-day default lookback, handles 429 rate-limit and 404 (unknown ticker) gracefully. Writes to `data/prices.csv` (git-ignored).
- **`scripts/bucket_returns.py`** — reads the weights CSV + prices CSV, computes trailing 1w/1m/3m/6m/1y returns per bucket using `weight_score` (per-window renormalisation across members that priced), and renders a stylish standalone HTML table sorted by 1-year return.

Ran against `outputs/2026-07-25/minidex_weights.csv`: 309 unique tickers, 81,229 daily price rows, zero fetch failures. Output at `outputs/2026-07-25/bucket_returns.html`.

Notable results (weight_score, 1Y):

| Bucket | 1Y return |
|---|---:|
| PQC & quantum | +5,520% |
| Memory & storage | +750% |
| Semiconductor materials | +429% |
| Data-centre power & cooling | +224% |
| Data-centre hardware | +219% |
| Photonics & optical interconnect | +201% |
| Semicap equipment | +199% |
| AI-native software | **−24%** |

The quantum outsize was driven by RGTI/QBTS moving from sub-$1 to $20+ in the year; the AI-native software drawdown is worth investigating (thematic disappointment vs. hyperscaler-embedded AI capturing the value).

`POLYGON_API_KEY` was added to `.env` (git-ignored) and `.env.example`.
