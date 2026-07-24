# mini-dex

Classifies US-listed tech/AI companies into 22 non-mutually-exclusive thematic
sub-indices, producing frozen weighted compositions as flat files. Batch
pipeline, not a service.

Full spec: [`docs/MINIDEX_SPEC.md`](docs/MINIDEX_SPEC.md).

## Setup

```bash
uv sync
cp .env.example .env      # fill in ANTHROPIC_API_KEY and SEC_USER_AGENT
uv run minidex init       # create SQLite schema
```

## Run sequence

```bash
uv run minidex universe   # stage 1: SEC company tickers
uv run minidex filter     # stage 2: SIC candidate filter
uv run minidex fetch      # stage 3: 10-K Item 1 + segments
uv run minidex shortlist  # stage 4: embedding shortlist
uv run minidex score submit --tickers NVDA,AMD,...   # stage 5 (paid)
uv run minidex score poll
uv run minidex qc                                    # stage 6
uv run minidex build --asof 2025-01-15               # stage 7
```

Outputs land in `outputs/<asof>/` as `minidex_weights.{csv,parquet}` +
`manifest.json`. Both `data/` and `outputs/` are git-ignored.

## Tests

```bash
uv run pytest
```

Runs offline — all network and API calls are mocked.
