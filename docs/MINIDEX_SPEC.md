# mini-dex — build specification v1.0

Audience: Claude Code, building this project autonomously. The human wants to
be hands-off. Follow this spec closely; where it is silent, prefer the
simplest solution that satisfies the acceptance criteria. Ask questions only
if something is genuinely blocking.

## 1. Purpose

Classify US-listed companies into 22 non-mutually-exclusive thematic
sub-indices ("mini-dexes") covering the tech/AI stack, with **weighted**
membership anchored to estimated revenue exposure. Produce **frozen, static
index compositions** (constituents + weights) as flat files. Refreshes will
be rare (roughly annual); this is a batch pipeline, not a service.

Two input artefacts are provided in the repo and are the source of truth:

- `definitions/minidex_definitions.yaml` — 22 bucket definitions with
  includes/excludes, anchor tickers, and keywords.
- `prompts/scoring_prompt.md` — the Stage 5 LLM system prompt, user prompt
  template, output JSON schema, and post-processing rules.

Do not rewrite the substance of either artefact. Code reads them at runtime.

## 2. Deliverables

1. A working Python CLI (`minidex`) implementing the 7-stage pipeline below.
2. A frozen output directory per run:

```
outputs/<YYYY-MM-DD>/
├── minidex_weights.csv       # long format, all buckets
├── minidex_weights.parquet   # identical content
└── manifest.json             # run metadata (see §9)
```

3. A standalone performance script `scripts/performance.py` that takes a
   frozen weights file + a prices CSV and reports cumulative bucket returns.
4. A pytest suite that passes, including anchor assertions against real
   scored data once a scoring run exists.
5. A README covering setup and the standard run sequence.

## 3. Tech stack

- Python 3.12, managed with **uv** (`pyproject.toml`, locked).
- Dependencies: `edgartools`, `sentence-transformers`, `anthropic`,
  `pydantic` (v2), `typer`, `pandas`, `numpy`, `pyarrow`, `pyyaml`,
  `python-dotenv`, `pytest`.
- Storage: **SQLite** at `data/minidex.db` via the stdlib `sqlite3` module
  (no ORM). Raw filing cache in `data/raw/`.
- Embeddings: `BAAI/bge-large-en-v1.5` locally via sentence-transformers,
  `normalize_embeddings=True`. (Config option to swap to `bge-small-en-v1.5`.)
- LLM scoring: Anthropic **Message Batches API**, model configurable,
  default a current Haiku-class model. Consult
  https://docs.claude.com/en/docs/build-with-claude/batch-processing and the
  current models list at build time rather than hardcoding assumptions.

## 4. Configuration & secrets

- `.env` (git-ignored; `.env.example` committed):
  - `ANTHROPIC_API_KEY`
  - `SEC_USER_AGENT` — e.g. `"minidex research <email>"`; required by SEC.
- `minidex/config.py` loads `.env`, exposes a frozen `Settings` object:
  paths, model names, embedding model, similarity threshold (default 0.60,
  see §7), score floor (0.10), batch model id, `prompt_version` (read from a
  version line in `prompts/scoring_prompt.md`).
- Never print, log, or commit the API key. Add `.env`, `data/`, and
  `outputs/` to `.gitignore` (keep `outputs/.gitkeep`).

## 5. Repository layout

```
minidex/
├── pyproject.toml
├── README.md
├── .env.example
├── definitions/minidex_definitions.yaml
├── prompts/scoring_prompt.md
├── data/                 # git-ignored: raw/ cache + minidex.db
├── outputs/              # git-ignored frozen runs
├── scripts/performance.py
├── minidex/
│   ├── __init__.py
│   ├── config.py
│   ├── db.py             # schema creation, connection helper, upserts
│   ├── universe.py       # stages 1–2
│   ├── edgar.py          # stage 3
│   ├── shortlist.py      # stage 4
│   ├── score.py          # stage 5
│   ├── qc.py             # stage 6
│   ├── indices.py        # stage 7
│   └── cli.py            # typer app
└── tests/
```

Modules communicate ONLY via the database and file cache — no cross-module
imports of internals (importing `db.py` and `config.py` is fine). `cli.py`
is a thin dispatcher.

## 6. Database schema

Create in `db.py` (idempotent `CREATE TABLE IF NOT EXISTS`):

```sql
companies(
  cik TEXT PRIMARY KEY, ticker TEXT NOT NULL, name TEXT,
  sic TEXT, exchange TEXT, is_candidate INTEGER DEFAULT 0,
  market_cap REAL, market_cap_asof TEXT
);
filings(
  cik TEXT, accession TEXT, fy INTEGER, filed_date TEXT,
  item1_path TEXT, item1_chars INTEGER, segments_json TEXT,
  PRIMARY KEY (cik, accession)
);
shortlist(
  cik TEXT, bucket_id TEXT, similarity REAL, source TEXT,  -- 'embed'|'anchor'
  PRIMARY KEY (cik, bucket_id)
);
scores(
  cik TEXT, ticker TEXT, bucket_id TEXT, fy INTEGER, run INTEGER,
  score REAL, confidence TEXT, rationale TEXT, evidence_type TEXT,
  pre_revenue INTEGER, prompt_version TEXT, model_version TEXT,
  created_at TEXT,
  PRIMARY KEY (cik, bucket_id, fy, run, prompt_version, model_version)
);
batches(
  batch_id TEXT PRIMARY KEY, submitted_at TEXT, status TEXT, meta_json TEXT
);
```

`scores` is append-only. A SQL view `latest_scores` exposes, per
(cik, bucket_id): mean score across runs 1–2, min confidence, both
rationales, for the newest (fy, prompt_version, model_version).

## 7. Pipeline stages — behaviour and acceptance criteria

### Stage 1 — `minidex universe`
Pull the SEC company_tickers list (edgartools) into `companies`. Enrich SIC
codes from EDGAR submissions metadata. Acceptance: ≥5,000 rows; NVDA, MSFT,
EQIX, VRT, CLS all present with SIC populated.

### Stage 2 — `minidex filter`
Mark `is_candidate=1` using an **inclusion** list of SIC ranges defined in a
constant with comments: semiconductors & electronics (3600s, 3674, 3825...),
computers/storage (357x), software & services (7372, 737x), communications
equipment (366x), electrical industrial apparatus (362x), engines/turbines
(351x), REITs (6798), electric services & IPPs (4911, 4991), electronic
components (367x), measuring instruments (382x), misc electrical (369x),
telecom (481x). Err inclusive. Also force-include every anchor ticker from
the YAML regardless of SIC. Acceptance: 800–2,500 candidates; every anchor
ticker is a candidate; JPM and PFE are not.

### Stage 3 — `minidex fetch`
For each candidate: latest 10-K (or 20-F if no 10-K) via edgartools; extract
Item 1 text (fall back to first 40k chars of the filing body if item parsing
fails, and record that in `filings`); extract segment revenue from XBRL where
available into `segments_json` (list of {segment, revenue, period}); write
Item 1 text to `data/raw/<accession>_item1.txt`. Respect SEC rate limits
(edgartools defaults; do not parallelize network calls beyond its built-in
politeness). Idempotent: skip (cik, accession) already in `filings`.
Also populate `market_cap` in `companies` (shares outstanding from company
facts × a recent price; a free price source or edgartools facts-derived
value is fine; record the as-of date). Acceptance: ≥90% of candidates have a
filing row with item1_chars > 2,000; re-running does no network fetches.

### Stage 4 — `minidex shortlist`
Embed each candidate's Item 1 (truncate to the model's max length) and each
bucket's `definition` + `includes` text. Cosine similarity matrix; insert
pairs above `Settings.similarity_threshold` with source='embed', and force
every (anchor ticker, its bucket) pair with source='anchor'. Then
**calibrate**: report the minimum similarity among anchor pairs; if any
anchor pair falls below the threshold, print a warning recommending a lower
threshold. Acceptance: every anchor pair present; median shortlisted buckets
per company between 1 and 6; total pairs < 12,000.

### Stage 5 — `minidex score submit` / `minidex score poll`
- Build one batch request per (candidate, run) where run ∈ {1, 2},
  `custom_id = "<ticker>|<fy>|run<1|2>"`.
- Prompt construction exactly per `prompts/scoring_prompt.md`: system prompt
  verbatim; user prompt from the template with Item 1 (truncate to ~12k
  chars), segments or "NOT AVAILABLE", and only that company's shortlisted
  buckets (id, name, definition, includes, excludes from the YAML).
- Submit via the Message Batches API; store batch id(s) in `batches`.
  Chunk into multiple batches if API limits require.
- `poll`: fetch results; validate each against a pydantic model mirroring
  the schema in the prompt file (strip accidental markdown fences before
  parsing); insert into `scores`; collect failures (parse errors, missing
  buckets, scores outside [0,1]) and write `data/score_failures.json`;
  `minidex score retry` resubmits failures as a new batch.
- Costs: this stage is the only paid one. Print an estimated token count and
  rough cost before submitting, and require `--yes` to skip the confirm
  prompt.
Acceptance: for a pilot of 30 companies (see §11), ≥95% of requests yield
valid rows in `scores` after at most one retry round.

### Stage 6 — `minidex qc`
Produce `outputs/qc_report.md` containing:
1. **Anchor check**: every anchor ticker's mean score on its anchor bucket;
   FAIL lines for any < 0.5.
2. **Run disagreement**: all (cik, bucket) with |run1 − run2| > 0.2.
3. **Borderline**: mean score in [0.10, 0.30].
4. **Low-confidence highs**: confidence == 'low' and mean score ≥ 0.3.
5. Summary counts per bucket (members at floor 0.10).
Mirror check 1 as pytest tests in `tests/test_anchors.py` (skipped if the
DB has no scores). Acceptance: report generates; tests pass on scored data
or are cleanly skipped.

### Stage 7 — `minidex build --asof <date>`
From `latest_scores` (mean score ≥ 0.10) joined to `companies.market_cap`:
- `weight_cap_score` = score × market_cap, normalised to sum to 1 per bucket
- `weight_equal` = 1/n per bucket
- `weight_score` = score normalised to sum to 1 per bucket
Write the long-format CSV + parquet with columns:
`bucket_id, bucket_name, ticker, cik, score, confidence, market_cap,
weight_cap_score, weight_equal, weight_score, rationale_run1,
rationale_run2, fy, prompt_version, model_version`.
Write `manifest.json` per §9. Acceptance: per bucket, each weight column
sums to 1.0 ± 1e-6; all 22 buckets have ≥ 2 members (warn, don't fail,
otherwise).

## 8. Performance script (separate from pipeline)

`scripts/performance.py --weights outputs/<date>/minidex_weights.csv
--prices prices.csv --weight-col weight_cap_score`
- prices.csv: columns `date, ticker, close` (the human supplies this from an
  existing source; also implement `--fetch-yf` best-effort via yfinance as a
  convenience, as an optional extra dependency).
- Static weights from freeze date; no rebalancing. Delisted names: hold
  terminal return (document this in README).
- Output: cumulative return per bucket (table printed + CSV written next to
  the weights file), and vs an optional benchmark column if provided.

## 9. manifest.json

```json
{
  "run_date": "...", "asof": "...", "n_companies_scored": 0,
  "n_pairs_scored": 0, "prompt_version": "...", "model_version": "...",
  "embedding_model": "...", "similarity_threshold": 0.0,
  "score_floor": 0.10, "definitions_sha256": "...",
  "prompt_sha256": "...", "filing_fy_histogram": {}
}
```

## 10. Testing

- Unit tests per module with fixtures; **mock all network and API calls**
  (a fake batch result fixture with valid + malformed responses for the
  poll/validation path).
- A tiny end-to-end test using 3 bundled fake filings driving stages 4→7
  with a stubbed scorer.
- Anchor tests per §7 stage 6.
- `uv run pytest` must pass with no network access.

## 11. Build & run order (what "done" looks like)

1. Scaffold repo, `db.py`, `config.py`, CLI skeleton; tests green.
2. Stages 1–4 implemented; run them for real end-to-end (network OK; no LLM
   cost). Check acceptance criteria.
3. **Pilot**: `minidex score submit --tickers <30 names>` — the anchor set
   plus AVGO, DELL, ETN, VST, ARW (a distributor), IBM (a conglomerate).
   Poll, run qc, eyeball. Iterate ONLY on parsing/plumbing — do not edit the
   prompt or definitions substantively without flagging to the human.
4. Full run: submit all candidates, poll, qc, build. Print the qc report
   summary and the top-5 weights of each bucket for human review.
5. Write README; final `pytest`; commit history with sensible messages.

## 12. Parallelisation with subagents

The module boundaries were designed for this. Recommended fan-out:

- **Phase A (sequential, do first)**: one agent scaffolds `pyproject.toml`,
  `config.py`, `db.py` (full schema + `latest_scores` view), the pydantic
  response models, and the CLI skeleton with no-op commands. This defines
  every interface the rest depends on. Nothing else starts until Phase A is
  committed and its tests pass.
- **Phase B (parallel, 4 subagents)** — each owns its module + its tests,
  touching ONLY its own files:
  1. `universe.py` (stages 1–2)
  2. `edgar.py` (stage 3)
  3. `shortlist.py` (stage 4)
  4. `score.py` (stage 5) — the largest; includes batch client, prompt
     builder, validation, retry
- **Phase C (parallel, 2 subagents)** after B merges:
  1. `qc.py` + anchor tests
  2. `indices.py` + `scripts/performance.py`
- **Phase D (sequential)**: integration agent runs the end-to-end fake-data
  test, then the real stages 1–4, then the pilot per §11, fixes seams,
  writes README.

Conflict rule: a subagent may not modify `db.py`, `config.py`, or another
module. If an interface change is needed, it stops and the change is made
in a dedicated commit first.

## 13. Non-goals

- No web UI, no API server, no scheduler/cron, no Docker.
- No fine-tuning or training of any model.
- No automated trading, backtesting beyond §8, or price-data infrastructure.
- No editing of the two input artefacts' substance (typo fixes fine).

## 14. Security notes

- API key only ever read from `.env`; the human will rotate it after the
  build. Never echo it, never write it into any file, log, or commit.
- SEC user agent must be set before any EDGAR call; fail fast with a clear
  message if missing.
