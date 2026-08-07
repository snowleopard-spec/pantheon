# mini-dex

Classifies US-listed tech/AI companies into 22 non-mutually-exclusive thematic
sub-indices, producing frozen weighted compositions as flat files. Batch
pipeline, not a service.

Full spec: [`docs/Context/PANTHEON_SPEC.md`](docs/Context/PANTHEON_SPEC.md).

## Setup

```bash
uv sync
cp .env.example .env      # fill in ANTHROPIC_API_KEY and SEC_USER_AGENT
uv run minidex init       # create SQLite schema
```

## Configuration

Secrets live in `.env` (`ANTHROPIC_API_KEY`, `SEC_USER_AGENT`).

Tuning knobs live in `config.json` at the repo root (committed) — edit this
file to change defaults for `embedding_model`, `similarity_threshold`,
`score_floor`, `batch_model`, `max_item1_chars`, `anchor_min_weight`. Its
`report` block holds the returns-report knobs (`sharpe_window`,
`benchmark_ticker`, `weight_col`, `weight_cap_m`); the `_valid_values` key
inside it documents what each accepts.

Precedence for tuning knobs is env var (`MINIDEX_*`) > `config.json` >
hardcoded default. This lets droplet-side deploys override individual knobs
via environment without editing the file.

## Run sequence

```bash
uv run minidex universe   # stage 1: SEC company tickers
uv run minidex filter     # stage 2: SIC candidate filter
uv run minidex fetch      # stage 3: 10-K Item 1 + segments
uv run minidex fetch-segments-llm submit             # stage 3.5 (optional, paid):
uv run minidex fetch-segments-llm poll               #   LLM segment fallback
uv run minidex shortlist  # stage 4: embedding shortlist
uv run minidex score submit --tickers NVDA,AMD,...   # stage 5 (paid)
uv run minidex score poll
uv run minidex score retry   # resubmit failures from data/score_failures.json
uv run minidex qc                                    # stage 6
uv run minidex build --asof 2025-01-15               # stage 7
```

A `minidex build` run writes its dated archive to `outputs/<asof>/` as
`minidex_weights.{csv,parquet}` + `manifest.json`. The canonical frozen weights
live at `definitions/minidex_weights.csv` (tracked in git; this is what
`scripts/pull_prices.py` and `scripts/bucket_returns.py` read by default) —
promote a new run by copying its CSV over the definitions copy. Both `data/`
and `outputs/` are git-ignored.

Downstream, `scripts/pull_prices.py` fetches Polygon daily closes for the
weights tickers plus the config benchmark, and `scripts/bucket_returns.py`
renders the interactive returns report (Sharpe column, pinned QQQ benchmark
row, median-constituent lines, per-constituent price charts) to
`outputs/bucket_returns.html`, copying `docs/ARCHITECTURE.html` alongside it.

### Coherence report

`uv run python scripts/coherence.py` tests whether each bucket actually
*trades* like a sub-index: mean pairwise correlation of members'
market-residual returns vs size-matched random draws from non-members.
Writes `outputs/coherence.html` (sortable stats + bucket-ordered heatmap)
and `outputs/coherence.csv`. Knobs in the `config.json` `coherence` block
(freq, window, draws, seed); spec: `docs/Context/COHERENCE_SPEC_1.md`.

## Tests

```bash
uv run pytest
```

Runs offline — all network and API calls are mocked.
