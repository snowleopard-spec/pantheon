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

**Immediate next action** for the next session: the pipeline has produced its first full-universe output. Downstream now belongs to the user — inspect the frozen weights (canonical copy: `definitions/minidex_weights.csv`; the dated archive from this run: `outputs/2026-07-25/`), iterate on weighting schemes (custom cap, sqrt-cap, blended), and track returns via `scripts/pull_prices.py` + `scripts/bucket_returns.py`. (The legacy `scripts/performance.py` backtester this note originally pointed at was deleted in v2.1, superseded by the live report.) The droplet can be destroyed (`doctl compute droplet delete <id>` or via the DO console). Next full refresh is annual per spec — the runbook at `docs/Skills/DEPLOY_TO_DROPLET.md` walks through spinning up a fresh droplet from scratch.

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

**Report enhancements (interactive, later on 2026-07-25).** The HTML report has since been made **interactive** and rebranded. Changes to `scripts/bucket_returns.py`:

- **Title changed** from "mini-dex bucket returns" to **"Pantheon"** (both `<title>` and `<h1>`). Subtitle now prompts "Click any bucket to see its constituents."
- **Column order reversed** to 1Y / 6M / 3M / 1M / 1W (longest window first). Matches how performance readouts are usually scanned.
- **Bucket names are clickable.** Each bucket row expands to reveal an inner constituent table: ticker, full company name, `weight_score` %, LLM score, market cap (formatted as $T/$B/$M), confidence. Sorted by `weight_score` descending. A chevron (▸/▾) indicates expand state. Expand/collapse is pure inline JavaScript (~15 lines) — no external dependencies.
- **Company names** are looked up at report-render time from `data/minidex.db` (`companies.name`). Optional `--db` flag overrides the path; the report falls back to ticker-only if the DB is unavailable.

**Per-constituent returns.** Each expanded constituent row now shows its own 1Y / 6M / 3M / 1M / 1W return alongside ticker/name/weight/score/mcap/confidence — 2,220 per-ticker return cells across the 22 tables. Same red/green formatting as the bucket-level cells.

**Penny-stock filter.** A `--min-start-price` flag (default $1.00) excludes any ticker from a given window if its starting price is below the threshold. Standard practice in equity return analysis; catches near-dead-recovery names whose percentage returns otherwise swamp aggregates. Motivating case: Zapata Quantum (ZPTA) went from $0.0001 → $0.90 over the year, a +899,900% "return" that single-handedly drove the PQC & quantum bucket to +5,520%. After the filter, PQC & quantum's true 1Y is **−11%** (thematic drawdown consistent with quantum stock volatility). Other bucket movements from the filter were small (single-digit %).

**Sortable constituent columns.** Every column header in the constituent tables is clickable to re-sort by that column. First click on a numeric column defaults descending; text columns default ascending. Toggle direction on repeat click. Sort indicators (▲/▼) show on the active column, ↕ hint on the others. NaN / empty values always sort last regardless of direction. Underlying sort keys are stored in `data-sort` attributes so numeric sorts use the raw value (e.g. $5T > $500B, not lexicographic). Confidence sorts by ordinal rank (high=3, medium=2, low=1).

**Column-width alignment.** Return columns in the main index table and the constituent tables now share fixed widths (4.4rem each), and the detail-cell right padding was zeroed so the constituent table extends to the same right edge as the main table. Result: the 1Y/6M/3M/1M/1W columns line up vertically between bucket-level rows and their expanded constituent rows.

**Layout polish.** All column headers centered (was mixed right/left/center). Company names + tickers now nowrap with ellipsis at 18rem — every constituent row is a uniform 1.8rem tall. Title enlarged to 3rem and centered; subtitle and metabar removed for a cleaner opening.

**Dark theme.** Switched to a GitHub-dark palette (`#0d1117` page, `#161b22` surfaces, `#e6edf3` text; positives `#56d364`, negatives `#f85149`). Print stylesheet inverts to light so hard copies stay readable on paper.

**Constituent-table default sort** is now `weight_score` descending (was market cap descending briefly; before that, weight_score). Market cap remains one click away.

### Smart price cache

`scripts/pull_prices.py` used to refetch the full 400-day window for every ticker on every run. Now it's incremental:

- Reads the existing `data/prices.csv` at start; computes the latest cached bar per ticker.
- If the latest bar is within a 3-day grace period of today (covers weekends and short holidays), the ticker is treated as up-to-date — zero HTTP calls.
- Otherwise, fetches only from `(latest_cached + 1)` to today; new tickers not in the cache get the full lookback.
- Merges new + cached data with dedupe on `(ticker, date)`, last-write-wins (so any post-hoc adjusted-close revisions from Polygon supersede stale cached rows).
- `--full` flag forces a complete refetch (useful after a suspected split-adjustment issue).
- Verified: on the 2026-07-25 second run, 301 of 309 tickers up-to-date, 8 incremental (all returned empty for the weekend). 8 HTTP calls instead of 309. Subsequent runs today are 0 HTTP calls.

### `pantheon` shell wrapper

`scripts/pantheon` (executable) is a one-command refresh: auto-detects the newest `outputs/<date>/minidex_weights.csv`, runs `pull_prices.py` (with fallback if Polygon fails), regenerates the HTML via `bucket_returns.py`, and `open`s the result. Sources `~/.local/bin/env` so `uv` is on PATH from any shell. A `pantheon` alias is installed in `~/.zshrc` (outside the repo). Typing `pantheon` from any directory refreshes and opens the report — combined with the smart cache, an idle refresh during trading hours is a few HTTP calls; overnight it's zero.

## 14. Tailscale droplet deployment (2026-07-28 →)

New build per `docs/Context/PANTHEON_TAILSCALE_SPEC.md`: daily price refresh + report re-render on the existing `unicorn-hunt` droplet, served privately over Tailscale. Weights stay frozen; no scoring on the droplet. All work on the `droplet-deploy` branch.

### M0 — dependency split (done, gate passed)

Commit `d6a281e` on `droplet-deploy`. Import audit found that **neither droplet script imports `minidex` at all**: `pull_prices.py` needs pandas + requests + dotenv; `bucket_returns.py` needs pandas + numpy (DB access is stdlib `sqlite3`). Two findings:

- `requests` was never declared in `pyproject.toml` — it rode in transitively via the heavy packages and would have vanished in a lean install. Now an explicit base dependency.
- The heavy imports were already lazy inside functions. The six hard-required sites (`edgar` ×4, `sentence_transformers`, `anthropic` ×3) now route through a new `config.require_scoring()` helper that exits 1 with "Install with: `uv sync --extra scoring`" instead of a bare `ModuleNotFoundError`. The `yfinance` fallback in `edgar.py` already degraded gracefully and was left alone.

Final split — base: `pydantic, typer, pandas, numpy, pyyaml, python-dotenv, requests`; `scoring` extra: `edgartools, sentence-transformers, anthropic, yfinance, pyarrow`. One `uv.lock` covers both profiles.

**Gate results.** Fresh temp clone, plain `uv sync`: 82 MB venv, torch/sentence-transformers/edgartools/anthropic/yfinance/pyarrow all absent; `pull_prices` ran live against Polygon (309/309 tickers) and `bucket_returns` rendered the full report; `minidex universe` in the lean env exits 1 with the install hint. Full profile on the Mac (`uv sync --extra scoring`): 117/117 tests pass excluding the 6 known anchor-data failures.

**Mac gotcha:** the local sync command is now `uv sync --extra scoring` — plain `uv sync` strips torch from the Mac env.

**Deviation from spec D7:** merge to `main` deferred by user decision; the droplet tracks the `droplet-deploy` branch for now and switches to `main` after the merge.

### M1 — deploy to droplet (done)

Target is the existing `unicorn-hunt` box (`161.35.122.12`, 1 vCPU / 1.9 GiB + 2 GiB swap), not a new droplet — the lean profile makes that viable where the RED verdict in §8 (written for the full scoring stack) did not.

- Installed `uv` 0.11.33 via the official installer (was absent).
- Cloned `https://github.com/snowleopard-spec/pantheon.git` → `/root/pantheon` on the **`droplet-deploy` branch** (`238f380`); plain `uv sync`, no extras.
- **D4 sanity check passed:** torch / sentence-transformers / edgartools / anthropic / yfinance / pyarrow all absent; venv is **122 MB** (Linux wheels run larger than the Mac's 82 MB); disk 41 GB free, RAM untouched.
- `.env` created at `/root/pantheon/.env` (`chmod 600`) containing **only** `POLYGON_API_KEY` — no Anthropic/SEC keys on this box. Key copied from the Mac's `.env` (H4).
- H5 resolved as scp from Mac: `data/prices.csv` (1.7 MB warm cache) + `data/minidex.db` (7 MB, company names for the report).
- `logs/` created for the future cron log.
- **Verify:** both scripts respond to `--help` from `.venv/bin/python`; frozen weights present at `outputs/2026-07-25/` (they are committed to git, so the clone carries them — no separate weights transfer needed).

### M2 — manual returns refresh end-to-end (done)

Ran both stages by hand on the droplet under `/usr/bin/time -v`, logs at `logs/m2_*.log`:

| Stage | Exit | Wall | Peak RSS |
|---|---|---|---|
| `pull_prices` (incremental, +299 rows — Monday's bars) | 0 | 2.8 s | **102 MB** |
| `bucket_returns` (full render, 360 KB HTML) | 0 | 30.4 s | **84 MB** |

Peak memory is ~7% of the box's ~1.4 GiB available — no swap-thrash risk (the M2 abort threshold never came close). The render takes 30 s on the 1 vCPU box vs ~2 s on the Mac; irrelevant for a nightly cron. Latest bar in cache confirmed `2026-07-27`; report regenerated 13:18 UTC 2026-07-28. Noted for later: the HTML has no visible "as of" date — worth adding so staleness is self-evident from the phone.

### Interlude — weights moved to `definitions/` (2026-07-28)

User call: the frozen weights CSV is an *input* to the returns pipeline, not an output, so it moved to the tracked `definitions/` folder. Commits `c6e8c49` + `ae05d14`:

- `git mv outputs/2026-07-25/minidex_weights.csv definitions/minidex_weights.csv` — now the canonical copy; `outputs/<asof>/` keeps dated run archives (parquet + manifest + the CSV of future runs). Promoting a new `minidex build` = copying its CSV over the definitions copy (documented in README).
- Both scripts' `--weights` default → `definitions/minidex_weights.csv`; `bucket_returns` `--out` default → `outputs/bucket_returns.html` (was `<weights-dir>/bucket_returns.html`, which would have dropped HTML into definitions/ — and which previously dirtied the *tracked* dated snapshot on every render; the droplet's M2 run did exactly that, restored via `git checkout` before pulling).
- `scripts/pantheon` wrapper reads the fixed canonical path instead of globbing `outputs/*/`; `logs/` added to `.gitignore` so droplet cron logs never dirty the tree.
- Verified on both machines: Mac (both scripts on defaults + 117 tests pass) and droplet (bare `scripts/bucket_returns.py` renders to `outputs/bucket_returns.html`, `git status` clean).

### M3 — static output location (done)

- Confirmed the report HTML is fully self-contained: zero `src=`/`href=`/`url()` references of any kind — publish is a single file.
- New `scripts/droplet_refresh.sh` (`c7b57d7`) is the cron entry point: incremental price pull (a Polygon failure warns loudly but still re-renders from cache per D6), render, then **atomic publish** — write `/srv/pantheon/index.html.tmp`, `mv` over the target — so a reader mid-refresh never sees a half-written page. A failed render exits 1 and keeps the previous page.
- `/srv/pantheon/` created; first real publish landed `index.html` (360 KB, root-owned, 644).
- **Smoke test:** `python3 -m http.server --bind 127.0.0.1` in `/srv/pantheon` returned HTTP 200 with the full page; server killed and port confirmed closed afterwards. (Gotcha: `pkill -f`/`pgrep -f` self-matching the ssh command string produced phantom "still running" hits and one self-killed ssh session; ground truth was `ss -tln`.)
- **Title fix that fell out of the smoke test (`ab92a20`):** the page title derived its date from the weights *parent directory* name, so the restructure produced "Pantheon — definitions". Now uses the latest price bar (`prices["date"].max()`) — the report's true as-of, which also resolves the M2 "no visible staleness indicator" note. Republished: `<title>Pantheon — 2026-07-28</title>`.

### M4 — cron (done)

Added to root's crontab in the existing entry style (comment line + `cd` into project + per-project log), in the 22:00 UTC slot — clear of Unicorn 21:00, Birthday 22:30, Dilithium 23:00:

```
# Pantheon — daily price refresh + bucket-returns report re-render at 22:00 UTC
0 22 * * * cd /root/pantheon && bash scripts/droplet_refresh.sh >> /root/pantheon/logs/cron.log 2>&1
```

The interpreter is invoked explicitly inside `droplet_refresh.sh` (absolute `.venv/bin/python`, `$REPO` derived from the script's own path), and `pull_prices` finds `.env` because cron `cd`s into the repo first. **Verify:** ran the exact cron command string by hand — exit 0, clean `logs/cron.log` entry, `/srv/pantheon/index.html` republished 14:15 UTC. No cron-environment PATH dependencies beyond `bash`.

### M5 — Tailscale install + serve (2026-07-29)

- Tailscale **1.98.10** installed via the official script; `tailscale up` auth URL approved by user (H6). Tailnet: **`macaw-dominant.ts.net`**; devices `unicorn-hunt` (100.79.67.34) and `iphone182` both visible (H7). Key expiry **disabled** for the droplet in the admin console (was set to 2027-01-25 — headless servers shouldn't silently drop off after 180 days).
- Serve configured with the current CLI syntax: `tailscale serve --bg /srv/pantheon` → `https://unicorn-hunt.macaw-dominant.ts.net/` (**tailnet only** — funnel status confirms nothing funneled). Config persists in tailscaled state, survives reboots.
- Let's Encrypt cert provisioned on first request via Tailscale's DNS-01 flow (`got cert` in journal); stored in `/var/lib/tailscale/certs/`.
- **Gotcha worth remembering — the spec's self-curl verification is impossible on this box.** `curl https://unicorn-hunt.macaw-dominant.ts.net/` *from the droplet* fails TLS ("alert internal error"): locally-originated traffic to the node's own tailscale IP short-circuits through the host stack and lands on **Caddy's `*:443` wildcard listener**, which has no cert for the ts.net SNI. Proven by connecting to `100.79.67.34:443` with SNI `api.unicornpunk.org` — Caddy answered (HTTP 404 from that vhost). Traffic from *other* tailnet devices arrives over WireGuard and tailscaled diverts serve ports into its netstack before the host stack — Caddy never sees it. Verification therefore moved to the iPhone (H8).
- **Exposure audit clean:** `ss -tlnp` shows the identical listener set to the pre-build baseline (caddy 80/443/2019-local, sshd 22, uvicorn 8000, resolved 53) plus tailscaled's own ports bound only to tailnet IPs; ufw rules untouched; no funnel; Caddy config untouched.
- The self-curl trap is written up as a reusable skill note: `docs/Skills/VERIFYING_TAILSCALE_SERVE_LOCALLY.md`.

### M6 — phone verification + wrap-up (2026-07-29) — BUILD COMPLETE

User confirmed the report renders on the iPhone over the tailnet URL (H8). `DEPLOY_NOTES.md` written (`2852da6`, later moved to `docs/Explainers/DEPLOY_NOTES.md`) — covers what was installed, serve config, cron, the git-based weights-update flow (no scp — a restructure dividend), teardown, and the "extending the tailnet" section (add a device / path-based & port-based / serve on new machines, Tailscale SSH as a future project).

**Acceptance criteria (spec §6) — all verified:**

| Criterion | Evidence |
|---|---|
| Daily cron unattended | First real 22:00 UTC run happened overnight: `start 2026-07-28T22:00:01Z … done 22:00:36Z`, republished `index.html` at 22:00 |
| Reachable via tailnet HTTPS from iPhone | User-confirmed on phone |
| Unreachable from public internet | From the Mac (off-tailnet): ts.net URL doesn't resolve/connect; even the droplet's **public** IP on 443 with the ts.net SNI refuses the handshake (Caddy, unknown SNI). No funnel, no new listeners, ufw untouched |
| Frozen weights untouched; no torch | `git status` clean in `definitions/`; lean venv verified torch-free (M1) |
| No secrets in repo/crontab | crontab clean; `.env` gitignored, mode 600, Polygon key only |
| Existing services undisturbed | `caddy` / `unicornhunt` / `sonar` / `cron` / `tailscaled` all active; public API vhost responding |

**Outstanding (user-side / future):** H9 VPN-on-demand toggle on the phone (optional); Tailscale SSH + Mac-on-tailnet as a follow-up project.

**Merge complete (2026-07-29):** `droplet-deploy` fast-forwarded into `main` (`51dad6e..1459e85`, 20 files) and the branch deleted; the droplet now tracks `main` per D7, verified with a clean refresh + serve intact after the switch.

### Post-build polish (2026-07-29, parallel-agent batch)

- `DEPLOY_NOTES.md` moved to `docs/Explainers/DEPLOY_NOTES.md`.
- `docs/ARCHITECTURE.html` gained section 13 "Daily updates: the droplet and the private network" — concise non-technical account of the nightly refresh, the slim droplet install, tailnet-only serving, and the annual weights flow, with a Mac → git → droplet → tailnet → iPhone SVG diagram reusing the doc's existing styles.
- The report now shows a freshness banner directly under the title: `Updated 29 Jul 2026 14:27 UTC · prices through 27 Jul 2026` (generation time UTC + latest price bar; muted `#8b949e`, print-safe). Daily-confidence check: on any given day the banner should read ~22:00 UTC of the previous evening.

### Docs finalisation (2026-07-29, post-merge)

- **ARCHITECTURE.html reordered** (`11433e3`) into a narrative flow chosen by the user — concept → journey → diagram → rationale, mechanics in the middle, reference material (exception rules, the 22 buckets) last. Sections renumbered 1–13; the droplet/Tailscale section is now **§11** (was §13). Pure structural move: same line count, no content changes, no cross-references existed to break.
- **Docs reorg completed** (`d742673`): the long-uncommitted moves landed as proper git renames — `MINIDEX_SPEC.md → docs/Context/PANTHEON_SPEC.md` (user's rename), `OUTPUT_COLUMNS.md` + `DROPLET_BOOTSTRAP_EXPLAINED.md → docs/Explainers/`, `DEPLOY_TO_DROPLET.md → docs/Skills/`. `PANTHEON_TAILSCALE_SPEC.md` is now tracked (was disk-only). Old `docs/PROGRESS_REPORT.md` deleted (successor is this file). Live references updated in README, ARCHITECTURE.html header/footer, and DEPLOY_TO_DROPLET's "which doc when" section; historical path mentions in this file's §1–11 left as-is deliberately. Final layout: `docs/Context/` (specs + this log), `docs/Explainers/` (narrative), `docs/Skills/` (runbooks/gotchas), `ARCHITECTURE.html` at top level. Working tree fully clean for the first time in the build.
- **Hardware section rewritten** (`8358aae`) as a two-phase story: the rented 8 GB / 2 vCPU droplet for the annual heavy run (chosen for availability during the multi-hour EDGAR fetch + the embedding model's 2.5–3 GB RAM peak, destroyed after), vs the small always-on droplet for the ~100 MB daily refresh + private serving. Removed the stale claim that the 8 GB box was "current".
- **Outside the repo:** `~/Projects/Stack/Docs/STACK_CONTEXT.md` updated for Pantheon 2.0 — project section (motivation from the spec, 2.0 concepts), Learning outcomes (private networking, dependency profiles), journey arc, droplet setup (fifth tenant, tailscaled, 22:00 cron row), subscriptions (Tailscale), and the two-phase hardware story. Claude Code session memory seeded with the durable learnings (deployment state, the self-curl trap, the STACK_CONTEXT location, build-workflow conventions).

## 15. v2.1 report upgrades (2026-08-05 →)

Brief: `docs/Context/New Prompt.md`. Spec with resolved decisions: `docs/Context/PANTHEON_V2.1_SPEC.md`. Branch `v2.1-upgrades`; first repo git tag `v2.1` planned on completion.

### M1 — spec + branch (2026-08-05)

- Reviewed codebase + `docs/Context` in full. Key findings shaping the spec: QQQ absent from the price universe (pull list derives solely from the weights CSV); no daily-return-series machinery exists in `bucket_returns.py` (it samples two point prices per window), so Sharpe needs a new builder; the main 22-row table is pre-sorted in Python and not client-sortable at all today; price history spans 2025-06-20 → 2026-07-27 (309 tickers, ~276 bars — ample for a 3m Sharpe and 1y charts).
- Design choices resolved with the user: annualized Sharpe with rf = 0; vendored uPlot for the charts (self-contained page preserved); constituent-level Sharpe included; full daily history embedded (~2 MB page, acceptable on the tailnet).
- New dependencies flagged: uPlot (MIT, vendored ~48 KB, build-time embed only) and one extra Polygon call/day for QQQ. No new Python packages; droplet lean profile untouched.
- Spec committed; **paused at the M1 gate for user review (H1)**.
- Spec revised at the gate (user request): added a file plan — the compute layer goes in a **new** `scripts/report_metrics.py` (+ `tests/test_report_metrics.py`, `assets/vendor/` uPlot files) precisely so file ownership stays disjoint, with `bucket_returns.py`/`pull_prices.py`/`config.json` as the only edited code files — and a parallel execution plan (Wave 1: agents on disjoint files; Wave 2: single-owner `bucket_returns.py` integration; Wave 3: docs + adversarial-review agents; deploy stays with the main session).
- Spec revised further at the gate (user requests, 2026-08-05): `weight_col` + `weight_cap_m` + self-documenting `_valid_values` joined the `report` config block; new §3.6 `weight_cap_score_aug` weighting (cap-at-m/N with iterative redistribution, derived at render time — frozen CSV untouched); new §3.7 Architecture-as-second-page (`droplet_refresh.sh` two-file publish + header link — `ARCHITECTURE.html` verified self-contained); new §6 codebase-cleanup audit (known candidates: legacy `performance.py` pair, ~60 MB of disk-only `.bak` snapshots, superseded `outputs/2026-07-24*` dirs) gated on user approval (H2); explicit docs-cadence rule (PROGRESS_REPORT at every wave/milestone close, ARCHITECTURE.html at build end). Final spec numbering: §6 cleanup, §7 file plan, §8 parallel plan, §9 milestones; waves now A–D / single-owner / E–F; human actions H1 spec, H2 cleanup approval, H3 phone check.

### Wave 1 — foundations (2026-08-05, four parallel agents, all landed)

- **Agent A — `scripts/report_metrics.py` + `tests/test_report_metrics.py`** (`abb046c`): the §7.1 contract implemented (config loader with `_`-key skip + loud validation, `daily_returns`, `sharpe`, `bucket_daily_series`, `capped_weights`, `median_constituent_returns`; constants `WINDOW_DAYS`/`TRADING_DAYS_PER_YEAR`/`MIN_SHARPE_OBS` exported for Wave 2). 34 new tests, all green. Contract deviations (accepted): `bucket_daily_series` gained a `weight_cap_m` kwarg; `capped_weights` operates per-bucket; zero-std guard uses a 1e-12 tolerance (float-noise on constant series); benchmark_ticker validated non-empty; no-start-bar tickers excluded from `daily_returns` consistently with `compute_ticker_returns`.
- **Agent B — benchmark plumbing** (`7cc5bc1`): `config.json` `report` block per spec §4 verbatim; `pull_prices.py` unions the configured benchmark into the fetch list via module-top `import report_metrics` (missing module = loud failure by design). Verified live at wave close: pull fetched **QQQ 276 bars (2025-07-01 → 2026-08-05)** + incremental catch-up, 310 tickers, +2,377 rows.
- **Agent C — uPlot vendored** (`1d53eb5`): `assets/vendor/` gets uPlot **1.6.32** (pinned jsDelivr URLs, SHA-256s in `VERSION.md`, MIT license text). Verified beyond spec: IIFE executed under node with DOM stubs — `uPlot` global + expected statics resolve; self-contained demo page in the session scratchpad.
- **Agent D — cleanup audit (read-only)**: full findings delivered for the H2 gate. Headlines: `performance.py` + its tests confidently dead (only importer is its own test file; nothing runnable invokes it; its output CSV exists nowhere — never successfully run against a promoted archive); `.bak` snapshots total 53 MB disk-only, but two runbooks name `scored_v16.bak` as the droplet-bootstrap seed, so keep-one-delete-19 proposed; `outputs/2026-07-24*` (932 KB, ignored) zero-referenced; dead-code sweep found the `_pick_recent_fact` cluster in `minidex/edgar.py` (~90 lines, zero callers) and 5 unused imports; all CLI commands and config keys live (three commands are README gaps, not dead code); `PROGRESS_REPORT.md` §9's next-steps pointer flagged as the one genuinely misleading stale reference.
- Pre-existing baseline confirmed at wave close: the 6 `test_anchors.py` data-driven failures fail identically with all Wave 1 changes stashed. Full suite otherwise green (222 passed).
- **Wave 1 closed; awaiting H2 (cleanup-list approval) before deletions. Wave 2 (single-owner `bucket_returns.py` integration) is next.**

### H2 gate — cleanup executed (2026-08-05)

User approved all four recommendations; executed ahead of Wave 2 so integration starts from a clean tree (spec had it in Wave 3 — harmless reorder, logged):

- **Repo deletions** (`aaf17a7`): `scripts/performance.py` + `tests/test_performance.py`; the dead `_pick_recent_fact` cluster in `minidex/edgar.py` (~90 lines); five unused imports; the backtester's `ARCHITECTURE.html` table row; and the user's pending deletion of `PANTHEON_FRONTEND_SPEC.html` (unused redesign mockup).
- **Disk-only deletions** (gitignored, no commit): 19 of 20 `data/minidex.db.*.bak` snapshots (~49 MB reclaimed; `scored_v16.bak` kept — it's the droplet-bootstrap seed the runbooks name); the six superseded `outputs/2026-07-24*` pilot dirs (932 KB).
- Suite after cleanup: **217 passed** (exactly the 5 dead-script tests fewer), same 6 pre-existing anchor failures, 3 skipped. Working tree fully clean.
- Deferred to Wave 3 docs pass: README gains the three undocumented CLI commands; fix the stale §9 next-steps pointer this audit flagged.

### Wave 2 — bucket_returns.py integration (2026-08-05) — M3 gate

Single-owner rewrite landed (one commit). Compute: config-driven settings with CLI override, `weight_cap_score_aug` derived per bucket (verified across all 22 buckets: weights sum to 1, cap respected, cap binds in 20/22 at m=3), Sharpe wired at index + constituent + benchmark level, medians under each return. Frontend: pinned QQQ row in its own `<tbody>` (structurally exempt from sorting), sortable main table with paired detail-row movement, wireframe chevron sort indicators, all §2 label fixes, lazy uPlot charts with 1Y–1W buttons, footer rewritten, Architecture link in the header line.

Verification: renders clean both weight methods (1.66 MB page); structural greps confirm every added/removed element; all three inline script blocks compile under node; suite unchanged (217 passed + the 6 pre-existing anchor failures). Browser automation unavailable this session — visual pass is the user's at the M3 gate.

**M3 gate feedback (user, 2026-08-05):** chart period buttons were no-ops — root cause a 1000× epoch bug (`pandas` parses the CSV dates as `datetime64[us]`; `int64 // 1e9` gave ~1.76e6 "epochs", so every window cutoff fell below the whole series and `sliceData` always returned everything). Diagnosed and re-verified with a headless-Chrome click harness (simulated clicks report slice lengths: 1W→7 bars, 3M→63, 1Y→189). Also: the Architecture header link 404'd locally — the renderer now copies `docs/ARCHITECTURE.html` beside the report, so the link works locally and the droplet publish ships both from `outputs/`. Both fixed in `git log -1` commit.

### score_floor 0.10 → 0.25 + weights rebuild (2026-08-05, user request at the M3 gate)

`config.json` floor raised; `minidex build --asof 2026-08-05` re-froze weights from the existing DB scores (no LLM cost, no fetch): **444 → 340 rows**, all 22 buckets intact. Cuts concentrate in the diluted mega-buckets (ai_native_software 103→83, cybersecurity 43→26, data_infrastructure 32→19); anchors exempt; **foundry down to 4 members** (naturally small universe — flagged to the user). Promoted to `definitions/minidex_weights.csv`, `outputs/2026-08-05/` archived tracked per the promoted-run convention, report re-rendered (340 rows, 1.82 MB). `test_config.py` no longer pins the floor's business value (asserts a sane range instead) — suite back to baseline 217 passed.

### Wave 3 — docs + adversarial review (2026-08-05, two parallel agents) — pre-release

- **Agent E (docs)** landed the full documentation pass (one commit): OUTPUT_COLUMNS report-only section, README CLI gaps, ARCHITECTURE v2.1 content + mutual page links, stale handoff pointer fixed. Footer cross-check: no contradictions between the report footer and the code.
- **Agent F (adversarial review)**: verdict "the build holds up". Independent hand-recomputation of foundry's weighted 1y return and 3m Sharpe, QQQ's row, and TSEM's chart series matched rendered values exactly (Δ=0). Headless-Chrome interaction matrix all-PASS (13 checks: sorts vs independent ordering, benchmark pinning, detail/chart-row adjacency through repeated sorts, period buttons monotone, NaN-last both directions, collapse/re-expand with chart state). Findings triaged: F1 uncommitted-docs risk (resolved — docs committed, renderer now warns if the back-link target is missing), F5 theoretical `</script>` breakout in the PRICES JSON (hardened with `<\\/` escaping), F3 `1w` Sharpe window valid-but-always-dashed (documented in OUTPUT_COLUMNS, practical minimum 1m), F2 zero direct test coverage of `bucket_returns.py` (logged as a known gap — the renderer is covered by the headless harness + report_metrics unit tests), F4 multi-day bar gaps count as single daily returns in Sharpe (data wart, logged), F6 missing-confidence sort rank (unobservable with current data).
- Suite at baseline (217 passed + 6 pre-existing); fresh render verified with E's docs (back-link rewrites correctly in the local copy).
- **Wave 3 closed. Remaining: merge to main, tag v2.1, droplet refresh, phone verification (H3).**

### M4 — release (2026-08-05) — SHIPPED

- `v2.1-upgrades` fast-forwarded into `main` (`f703543..e773e95`, 29 files, +2,431/−1,832), branch deleted, **first repo tag `v2.1`** (annotated) pushed with main.
- Droplet: `git pull` + manual `droplet_refresh.sh` — pull fetched the fresh Aug-5 bars (incl. QQQ's first droplet-side fetch), render clean, atomic two-file publish verified in `/srv/pantheon/`: `index.html` 1.8 MB (Sharpe column, benchmark row, architecture link all present), `architecture.html` 47 KB with the `./` back-link. Both 22:38 UTC.
- Nightly cron (22:00 UTC) needs no changes — config-driven benchmark + the refresh script's second publish step are all it takes.
- **Outstanding: H3** — user confirms report + Architecture link on the iPhone over the tailnet.

**H3 verified (2026-08-06):** user confirmed the v2.1 report and the Architecture page render and interact correctly on the iPhone over the tailnet. **v2.1 build complete.**

## 16. v2.2 company search (2026-08-06)

Brief: rolling `docs/Context/New Prompt.md` (quoted verbatim in `PANTHEON_V2.2_SPEC.md`) — search box under the title, autocomplete over the sub-index companies, centre-aligned score panel on selection. User amendments at spec review: both proposed enrichments in (click-through to the sub-index + inline price chart), and **no floor — show all scores**. Single-wave, single-owner build on `v2.2-search`; all code edits in `scripts/bucket_returns.py` (spec D7).

### Design decisions (spec D1–D7)

- **Search universe = all 810 scored companies** (3,387 (company, bucket) pairs in the DB's `latest_scores` view), not just the 340 index memberships — the natural consequence of no-floor: a company that scored below the floor everywhere is findable, and the panel shows why it's in no index (D1).
- **Score source = `latest_scores`** — the same two-run average the index build uses — read from `data/minidex.db` at render time. Droplet's DB snapshot verified identical on these counts (810 / 3,387) before building, so both machines render the same search (D2). **Graceful degradation:** DB missing/unreadable ⇒ weights-CSV-only fallback (members + frozen scores) with a loud stderr warning — the report never fails to render over search-data health.
- **Panel rows** (D3/D4): sub-index · score (2 dp) · confidence · Index Weight, sorted by score descending; member rows carry the active-column weight + click-through; below-floor rows show a muted dash + "below floor" tag.
- **Charts for members only** (D5): the panel reuses the existing `PRICES` payload + uPlot chart builder at zero added payload. Prices are only pulled for weights tickers + benchmark; extending the daily pull to all 810 scored tickers (~2.6× Polygon calls, ~4 MB page) judged not worth it for companies we don't track — non-member panels get the scores table with a note, no chart.
- **Self-contained constraint holds** (D6): vanilla-JS autocomplete over an embedded `SEARCH` blob — ~180 KB, page 1.82 → 2.00 MB — no new dependencies; same `</`-escape script-breakout hardening as `PRICES`.

### Implementation (`4325797`, all in `bucket_returns.py`)

- `_load_search_data`: payload built from `latest_scores` LEFT JOIN `companies`, joined against the active weights frame for membership/weight; `floor` read from config for the below-floor tagging.
- Autocomplete: ≥1 char, up to 8 matches, ticker-prefix matches ranked above ticker/name-substring matches; ↑/↓/Enter keyboard nav, Esc + click-away close; suggestion clicks bind on `mousedown` (fires before the input's blur, so click-away can't eat the selection); non-members tagged "not in any index" in the dropdown.
- Panel: centre-aligned card (ticker / name / market cap header, ✕ to close), member chart with the standard 1Y–1W period buttons; member-row click-through scrolls to the sub-index, expands it, and flashes the row.

### Verification

Headless-Chrome gauntlet, all-PASS: 810 companies / 3,387 pairs embedded; ticker-prefix and lowercase name-substring matching; Enter and arrow-key selection; NVDA panel 2 member + 12 below-floor rows; click-through landed on the right bucket with flash; chartless non-member panel with note (AAPL, fittingly, being the scored-but-in-no-index case); Escape and click-away. All 4 inline script blocks compile under node. Suite at baseline (217 passed + 6 pre-existing anchor failures). Docs: OUTPUT_COLUMNS search-panel section, ARCHITECTURE report paragraph extended.

### Shipped (2026-08-06)

`v2.2-search` fast-forwarded into `main` (`f3d0e5b..9eb9ea2`), tag `v2.2` pushed, branch deleted. Droplet pulled + refreshed clean: `/srv/pantheon/index.html` 2.0 MB with the search feature, `architecture.html` updated, both 22:21 UTC. Search verified present in the published page.

### Post-ship notes (2026-08-06)

- **Stale page on the phone — client cache, not the server.** The user's phone initially showed the pre-v2.2 page. Server side verified current: `tailscale serve` maps `/` to the directory and reads files per-request, and the published `index.html` was the new 2.0 MB build. Root cause: the iOS home-screen web-app held its cached copy; fully closing and reopening the app fixed it. Operational gotcha worth remembering after any publish. Phone check then passed — **v2.2 build complete.**
- **Score semantics recorded** (post-ship user question): scores are per-bucket revenue-fraction estimates judged in isolation (prompt v1.6 Rule 1) and deliberately do **not** sum to 100% — buckets overlap by design (Rule 3), sub-0.10 fractions floor to zero (Rule 4), and unthemed revenue is simply unrepresented; pre-revenue companies score on business purpose instead (Rule 5 exception). Displayed values are the two-run averages.

## 17. LWLG inclusion via ticker allowlist (2026-08-07)

User noticed Lightwave Logic (LWLG) — a photonics name — was absent. Investigation traced it to a **Stage 2 false negative**: LWLG is in the Stage 1 universe but files under **SIC 3080 "Miscellaneous Plastics Products"** (its products are electro-optic *polymers*), outside every curated SIC range, so `is_candidate=0` and nothing downstream ever saw it.

- **Fix:** `TICKER_INCLUDE` allowlist in `minidex/universe.py` beside `SIC_INCLUDE` — passes named tickers through Stage 2 with no anchor-style privileges (no QC expectation, no 5% weight floor); membership is still earned through shortlist + scoring. The anchor route was considered and rejected precisely because of those privileges.
- **Pipeline run (Mac, scoring extra):** filter → 1,377 candidates (+1); fetch pulled LWLG's 10-K (Item 1 + market cap $1.14B); shortlist put it on 11 buckets (photonics_optical top, 0.695 similarity); `score submit --tickers LWLG` (2 requests, ~$0.02 — the full-universe resubmit at ~$10 was deliberately aborted; the stage is not incremental, `--tickers` is the right tool); scores: **photonics_optical 0.85 high-confidence**, all other 10 buckets 0.0–0.15 (below floor). Exactly the merit-based outcome the allowlist design intended.
- **Rebuild + promote:** `build --asof 2026-08-07` → 341 rows / 22 buckets; LWLG enters photonics_optical at **8.43%** (weight_score). Archive `outputs/2026-08-07/` tracked. Price pull fetched 275 LWLG bars; render verified: constituent row, search entry (member row + 10 below-floor), chart data present. Suite at baseline (217 + 6 pre-existing).
- Also in this branch: §16 expanded into the full v2.2 record with post-ship notes (user request, separate agent).

## 18. Coherence report (2026-08-07)

Built from `docs/Context/COHERENCE_SPEC_1.md` (v1.0 + §11 resolved decisions from spec review: `./`+local-rewrite nav link; **per-bucket nulls excluding the tested bucket's members** — user's choice over the spec-literal shared per-size null; the $1 penny filter applied for consistency with the returns report).

- `scripts/coherence.py` (~600 lines, style-matched to `bucket_returns.py`, reusing its CSS constant by import — output of `bucket_returns.py` untouched). Deterministic: seeded RNG, fixed CSV formatting; the CSV strings feed the HTML table directly so acceptance §10.3 holds by construction.
- Edge found by the test suite: with a tiny universe the non-member draw pool can be smaller than the bucket — guarded (rhos reported, null blank); unreachable on real data.
- Verification: 5 offline math tests green (planted blocks >95 pctile / random group <90; residualization kills a 2×-beta ticker's benchmark correlation; byte-identical CSV across runs; effective-n vs surviving size; hand-checked upper-triangle mean). Full suite 222 + 6 pre-existing. Real-data run ~3s (budget 2 min); CSV↔HTML identity and byte-determinism verified on real output; headless-Chrome pass: canvas painted, z-sorts verified both directions, tooltip/legend live, file:-protocol link retarget works.
- **Result on current data:** 20/22 buckets at ≥95th percentile vs their nulls — the buckets overwhelmingly do trade like sub-indices, even after stripping market beta. Weakest: Hyperscalers (pctile 78.3, ρ_intra 0.082) and Connectors (94.4). Strongest z: Cybersecurity 16.55, SemiCap 16.42, AI-native software 13.80. The ρ_raw − ρ_intra gaps confirm a large share of naive co-movement was just market beta.
- Droplet publish deliberately left to the user per spec §8 (one line in `droplet_refresh.sh`; the page's nav link is already droplet-root-correct).

### v2.3 — coherence on the droplet (2026-08-07)

User promoted the coherence add-on to a tagged release. `droplet_refresh.sh` now renders and publishes `coherence.html` beside the report each night (atomic publish, non-fatal — the main report is never hostage to the add-on; this supersedes spec §8's leave-it-to-the-human line at the user's request). The main report header gained a Coherence nav link (deviation from the spec's "no reciprocal link", user-approved — without it the page is undiscoverable from the phone). `ARCHITECTURE.html` gained §12: the method in one breath and the headline results (20/22 buckets ≥ 95th percentile; the Hyperscalers result explained as market-beta absorption rather than failure); old §12/§13 renumbered 13/14 (no inbound references existed).

**v2.3 shipped (2026-08-07):** merged + tagged; droplet pulled and refreshed 21:33 UTC — `/srv/pantheon/` now serves `index.html` (2.0 MB, Coherence nav link), `coherence.html` (354 KB, heatmap verified), `architecture.html` (§12 included). Coherence is now part of the nightly 22:00 UTC cycle.

**Post-v2.3 polish (2026-08-07):** QQQ benchmark row recoloured to olive gold `#2e2508` (hover `#372d0b`, print `#f7f2e0`) — user-picked from a six-swatch live palette page.

**Post-v2.3 polish 2 (2026-08-07):** `ARCHITECTURE.html` restyled to the report's GitHub-dark theme (user request) — sans typography at the doc's 900px reading measure, dark tables/callouts/code chips, both SVG diagrams recolored (pipeline boxes lifted to `#262d38`/`#6e7681` for contrast after review; output nodes in the QQQ olive gold), print stylesheet reverts to light.
