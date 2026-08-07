# Pantheon v2.5 — Deep-Score List (Opus 5 for Diversified Names)

**Status:** spec finalized 2026-08-07 · **to be built in a fresh session** · branch `v2.5-deep-score` · tag `v2.5` on completion
**Brief:** the FPGA/PQC audit (PROGRESS_REPORT §17 note) found that INTC was never LLM-scored at all and AMD only via its fabless anchor — their Item 1 texts are so broad they embed close to nothing, so the shortlist gate starves the scorer. Fix chosen by the user (over manual hardcoding and over always-score-with-Haiku): a **deep-score list** of names that bypass the embedding shortlist and are scored against **all 22 buckets by a stronger model**.

## 1. The idea in one paragraph

`deep_score` is a small, named list (seeded: **INTC, AMD**) defined in `definitions/minidex_definitions.yaml` next to the anchors. Members skip the embedding-similarity gate entirely — they are shortlisted against every bucket unconditionally — and their scoring batch requests run on **Claude Opus 5** instead of Haiku. Everything else is unchanged: same universe/filter/fetch stages, same scoring prompt (v1.6), same two-run averaging, same QC, same score floor at build time. The list is data, not code; adding the next structurally-misjudged conglomerate is a one-line YAML edit plus a `score submit --tickers X` run.

## 2. Resolved decisions (do not relitigate)

| # | Decision |
|---|---|
| D1 | **Model: `claude-opus-5`**, configured as `deep_score_model` in `config.json` (env `MINIDEX_DEEP_SCORE_MODEL` > config > default, via the existing `minidex/config.py` pattern). Fable 5 rejected for this job: 2× the price, requires 30-day org data retention, safety classifiers can refuse batch requests (and the server-side fallback parameter is rejected on the Batches API); its edge is long-horizon agentic work, not one structured scoring call. Opus 5 batch cost for the seed pair: ~**$0.15** per full re-score (4 requests, 50% batch discount). |
| D2 | **List location: `definitions/minidex_definitions.yaml`** (`deep_score: [INTC, AMD]`), beside the anchors — index-definition data belongs with index definitions. Unlike anchors, deep-score membership grants **no privileges**: no QC score expectation, no 5% weight floor, no shortlist-similarity records — membership in an index is still earned through the score and the floor. |
| D3 | **Scope of bypass: shortlist only.** Deep names must still be universe candidates with a fetched filing (INTC and AMD both are). The bypass adds all 22 (company, bucket) pairs at the shortlist stage, flagged with a provenance marker so QC/debugging can tell forced pairs from embedding survivors. |
| D4 | **Same prompt, mixed batch.** Scoring uses the unmodified v1.6 prompt for cross-model comparability. The Batches API allows per-request models, so deep-name requests ride in the same batch submission as any Haiku requests — no separate pipeline. |
| D5 | **Opus 5 request shape differs from Haiku's** (per the claude-api reference, verified 2026-08-07): **omit `temperature` entirely** (Opus 5 returns 400 on any sampling parameter; Haiku requests keep temperature 0 — see `docs/Explainers/TEMPERATURE_EXPLAINED.md`), and set **`max_tokens` ≥ 16000** because thinking is on by default and `max_tokens` caps thinking + JSON output together. Handle `stop_reason == "refusal"` in the poll stage as a failed request (log loudly), not a crash. |
| D6 | **Explicit precedence, not string-ordering luck.** The `latest_scores` view picks rows by string-comparing `fy\|prompt_version\|model_version` — `claude-opus-5` happens to beat `claude-haiku-4-5` alphabetically, but e.g. `claude-fable-5` would lose. Do not rely on this: when the poll stage ingests deep-model rows for a company, it **deletes that company's rows from other models at the same prompt_version** (logging the count). History is preserved by the git-tracked promoted-run archives, not by dead DB rows. |
| D7 | **Expectation setting:** deep scores may well land below the 0.25 floor (Intel Foundry's external revenue is a small fraction of Intel's total; Rule 4 zeroes sub-0.10 fractions). That is success, not failure — the goal is that these names get *judged*, the search panel shows the judgments (INTC currently shows as never assessed), and future floor changes or annual re-scores inherit the coverage. AMD's data-center segment is the one plausible index entry. |
| D8 | **Comparability caveat (log in OUTPUT_COLUMNS):** scores from different models are not perfectly calibrated against each other. Mitigations: shared prompt, two-run averaging, QC disagreement checks, and the list is reserved for names the embedding path structurally cannot see — it is not a general quality-upgrade lever. `model_version` in the weights CSV shows provenance per row, as it always has. |

## 3. File plan

| File | Change |
|---|---|
| `definitions/minidex_definitions.yaml` (edit) | New top-level `deep_score:` list, seeded `[INTC, AMD]`, with a comment stating the admission criterion (diversified names whose Item 1 embeds too broadly for the shortlist gate). |
| `minidex/config.py` (edit) | `deep_score_model` setting (default `"claude-opus-5"`), resolved env > config.json > default like its siblings. |
| `config.json` (edit) | `"deep_score_model": "claude-opus-5"` (top-level, beside `batch_model`). |
| `minidex/shortlist.py` (edit) | Load `deep_score` from the definitions yaml; for those tickers emit all-bucket pairs unconditionally with a provenance flag (mirror how anchor pairs are forced today). |
| `minidex/score.py` (edit) | Per-request model: deep-list tickers get `deep_score_model`, others `batch_model`. For deep requests: no `temperature`, `max_tokens` ≥ 16000. Poll: refusal handling per D5; supersession deletes per D6. Cost estimate in the submit confirmation should price each request at its own model's rates. |
| `tests/` (edit) | shortlist: deep names produce 22 pairs regardless of similarity; score: request construction per model (temperature present/absent, max_tokens, correct model id), supersession delete on ingest, refusal handled as failure. Offline — mock the batch client as existing tests do. |
| `docs/Explainers/OUTPUT_COLUMNS.md` (edit) | Short note under the scores explanation: deep-score list, model provenance, D8 caveat. |
| `docs/Context/PROGRESS_REPORT.md` | §20 build log at the usual cadence. |

No new files beyond docs. No pipeline-order changes, no DB schema changes, no report/frontend changes (the search panel picks up new scores automatically).

## 4. Build & run sequence (for the fresh session)

1. Branch `v2.5-deep-score`; implement per §3; suite green offline.
2. `minidex filter` (no-op for these names — both already candidates), `minidex shortlist` (adds the forced pairs), `minidex score submit --tickers INTC,AMD -y` (4 requests, ~$0.15), poll, QC.
3. `minidex build --asof <date>`; promote; `pull_prices` (no new tickers unless a bucket admission changes membership); render; verify INTC/AMD appear in search with full score sets and correct `model_version`.
4. PROGRESS_REPORT §20, merge, tag `v2.5`, droplet pull + refresh, phone check.

## 5. Acceptance criteria

1. `latest_scores` shows 22 rows each for INTC and AMD with `model_version = claude-opus-5` (AMD's old Haiku fabless row superseded per D6).
2. The search panel shows both names with full score sets (member or below-floor as the scores dictate).
3. Suite green; no behavior change for non-deep names (Haiku requests byte-identical to before).
4. Total spend for the seed run under $1.

## 6. Out of scope

- Growing the list beyond the seed pair (user's call, one YAML line each).
- Re-scoring the broad universe with a stronger model; any prompt changes; changing the shortlist threshold.
- The queued items from earlier builds: report-assets refactor, forced-shortlist-pairs mechanism (this spec supersedes the need for it in its ticker-level form).
