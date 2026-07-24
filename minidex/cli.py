from __future__ import annotations

import typer

from minidex import config, db

app = typer.Typer(add_completion=False, no_args_is_help=True, help="mini-dex pipeline CLI")

score_app = typer.Typer(help="Stage 5: LLM scoring via Anthropic batch API")
app.add_typer(score_app, name="score")

seg_app = typer.Typer(help="Stage 3.5 (optional): LLM extraction of revenue disaggregation")
app.add_typer(seg_app, name="fetch-segments-llm")


@seg_app.command("submit")
def seg_submit(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip cost confirmation prompt."),
) -> None:
    """Submit LLM segment refinement batch for filings without proper splits."""
    from minidex import llm_segments as _mod

    _mod.submit(assume_yes=yes)


@seg_app.command("poll")
def seg_poll() -> None:
    """Poll open segment batches and write results back to filings.segments_json."""
    from minidex import llm_segments as _mod

    _mod.poll()


def _init_db() -> None:
    s = config.get_settings()
    with db.connect(s.db_path) as conn:
        db.init_schema(conn)


@app.command()
def init() -> None:
    """Create the SQLite schema."""
    _init_db()
    typer.echo("Schema initialized.")


@app.command()
def universe() -> None:
    """Stage 1: pull SEC company_tickers into `companies`."""
    from minidex import universe as _mod

    _mod.run()


@app.command()
def filter() -> None:
    """Stage 2: mark SIC-included companies as candidates."""
    from minidex import universe as _mod

    _mod.filter_candidates()


@app.command()
def fetch(
    tickers: str = typer.Option(None, help="Comma-separated ticker allowlist (pilot mode)."),
) -> None:
    """Stage 3: pull 10-K/20-F Item 1 + segments per candidate."""
    from minidex import edgar as _mod

    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    _mod.run(ticker_list=ticker_list)


@app.command()
def shortlist() -> None:
    """Stage 4: embed Item 1 vs bucket definitions; build candidate pairs."""
    from minidex import shortlist as _mod

    _mod.run()


@score_app.command("submit")
def score_submit(
    tickers: str = typer.Option(None, help="Comma-separated ticker allowlist (pilot mode)."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip cost confirmation prompt."),
) -> None:
    """Submit Stage 5 batches (runs 1 and 2)."""
    from minidex import score as _mod

    ticker_list = [t.strip().upper() for t in tickers.split(",")] if tickers else None
    _mod.submit(ticker_list=ticker_list, assume_yes=yes)


@score_app.command("poll")
def score_poll() -> None:
    """Poll open batches and ingest results into `scores`."""
    from minidex import score as _mod

    _mod.poll()


@score_app.command("retry")
def score_retry() -> None:
    """Resubmit failures from data/score_failures.json as a new batch."""
    from minidex import score as _mod

    _mod.retry()


@app.command()
def qc() -> None:
    """Stage 6: write outputs/qc_report.md."""
    from minidex import qc as _mod

    _mod.run()


@app.command()
def build(
    asof: str = typer.Option(..., help="As-of date, YYYY-MM-DD."),
) -> None:
    """Stage 7: freeze weighted index compositions."""
    from minidex import indices as _mod

    _mod.build(asof=asof)


if __name__ == "__main__":
    app()
