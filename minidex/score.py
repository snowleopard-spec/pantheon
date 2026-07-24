"""Stage 5 — LLM scoring via the Anthropic Message Batches API.

The public surface is three functions exposed via the CLI:
  * submit(ticker_list, assume_yes) - build & submit batch(es)
  * poll()                           - fetch results & insert into scores
  * retry()                          - resubmit failures from data/score_failures.json

Everything network-facing goes through the Anthropic SDK
(``client.messages.batches.{create, retrieve, results}``). Tests inject a
fake client via ``client_factory`` on submit/poll/retry.
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import yaml
from pydantic import ValidationError

from minidex import config, db, models

# ---- constants --------------------------------------------------------------

# Anthropic Message Batches API limits (per docs): 100k requests or 256 MB.
MAX_REQUESTS_PER_BATCH = 100_000
# Room for output; scoring responses are small JSON.
DEFAULT_MAX_TOKENS = 2048
# Rough Haiku 4.5 batch price ($/MTok) for the cost estimate. This is only a
# rough estimate — real billing is authoritative.
_ROUGH_PRICE_PER_MTOK_INPUT = 0.50
_ROUGH_PRICE_PER_MTOK_OUTPUT = 2.50

FAILURES_FILENAME = "score_failures.json"

_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)


# ---- data types -------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    cik: str
    ticker: str
    name: str
    fy: int
    accession: str
    item1_path: str
    segments_json: str
    bucket_ids: tuple[str, ...]


# ---- helpers: prompts, YAML, files -----------------------------------------


def _load_prompt_sections(prompt_path: Path) -> tuple[str, str]:
    """Return (system_prompt, user_template) parsed from prompts/scoring_prompt.md.

    Both are extracted from the first two triple-backtick fenced blocks
    following the ``## System prompt`` and ``## User prompt template``
    headings.
    """
    text = prompt_path.read_text(encoding="utf-8")

    def _fence_after(heading: str) -> str:
        idx = text.find(heading)
        if idx < 0:
            raise RuntimeError(f"Missing '{heading}' section in {prompt_path}")
        # find first ``` after the heading
        start = text.find("```", idx)
        if start < 0:
            raise RuntimeError(f"No fenced block found after '{heading}' in {prompt_path}")
        # skip the opening fence's line
        nl = text.find("\n", start)
        end = text.find("```", nl + 1)
        if end < 0:
            raise RuntimeError(f"Unterminated fenced block after '{heading}' in {prompt_path}")
        return text[nl + 1 : end].rstrip("\n")

    system = _fence_after("## System prompt")
    user_tpl = _fence_after("## User prompt template")
    return system, user_tpl


def _load_bucket_defs(definitions_path: Path) -> dict[str, dict[str, Any]]:
    data = yaml.safe_load(definitions_path.read_text(encoding="utf-8"))
    return {b["id"]: b for b in data["buckets"]}


def _format_segments(segments_json: str | None) -> str:
    """Return a human-readable multi-line table, or 'NOT AVAILABLE'."""
    if not segments_json:
        return "NOT AVAILABLE"
    try:
        rows = json.loads(segments_json)
    except (json.JSONDecodeError, TypeError):
        return "NOT AVAILABLE"
    if not rows:
        return "NOT AVAILABLE"
    lines = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        seg = r.get("segment", "?")
        rev = r.get("revenue", "?")
        period = r.get("period", "")
        suffix = f" [{period}]" if period else ""
        lines.append(f"- {seg}: {rev}{suffix}")
    return "\n".join(lines) if lines else "NOT AVAILABLE"


def _format_buckets_block(buckets: list[dict[str, Any]]) -> str:
    """Render the bucket blocks in the user prompt style: --- id / name / def / includes / excludes ---."""
    out: list[str] = []
    for b in buckets:
        includes = b.get("includes") or []
        excludes = b.get("excludes") or []
        includes_s = ", ".join(str(x) for x in includes) if isinstance(includes, list) else str(includes)
        excludes_s = ", ".join(str(x) for x in excludes) if isinstance(excludes, list) else str(excludes)
        definition = " ".join((b.get("definition") or "").split())
        out.append("---")
        out.append(f"id: {b['id']}")
        out.append(f"name: {b.get('name', '')}")
        out.append(f"definition: {definition}")
        out.append(f"includes: {includes_s}")
        out.append(f"excludes: {excludes_s}")
    out.append("---")
    return "\n".join(out)


def _read_item1(path: str | None, max_chars: int) -> str:
    if not path:
        return ""
    p = Path(path)
    if not p.is_absolute():
        p = config.get_settings().repo_root / p
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""
    if len(text) > max_chars:
        return text[:max_chars]
    return text


def _fill_user_template(
    template: str,
    *,
    ticker: str,
    company_name: str,
    cik: str,
    fy: int,
    item1_text: str,
    segment_table: str,
    buckets_block: str,
) -> str:
    """Fill the user-prompt template.

    The template contains a placeholder line ``{for each shortlisted bucket:}``
    followed by a sample bucket block. Everything from that line through the
    template's final ``---`` (i.e. the sample-block closing fence) is replaced
    by the actual per-bucket rendering.
    """
    # 1. simple {name} substitutions we do explicitly (str.format would trip
    #    on JSON schema braces in the template).
    subs = {
        "{ticker}": ticker,
        "{company_name}": company_name,
        "{cik}": cik,
        "{fy}": str(fy),
        "{item1_text}": item1_text,
        '{segment_table_or_"NOT AVAILABLE"}': segment_table,
    }
    out = template
    for k, v in subs.items():
        out = out.replace(k, v)

    # 2. replace the bucket-loop marker + its sample block with the real buckets.
    marker = "{for each shortlisted bucket:}"
    idx = out.find(marker)
    if idx >= 0:
        # find the closing --- of the sample block (there are two --- lines
        # bracketing the sample bucket immediately after the marker).
        first_dash = out.find("---", idx)
        second_dash = out.find("---", first_dash + 3) if first_dash >= 0 else -1
        if second_dash >= 0:
            end = second_dash + len("---")
            out = out[:idx] + buckets_block + out[end:]
    return out


# ---- DB loading -------------------------------------------------------------


def _latest_filing_row(conn: sqlite3.Connection, cik: str) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT accession, fy, filed_date, item1_path, segments_json
        FROM filings
        WHERE cik = ? AND item1_path IS NOT NULL AND item1_path != ''
        ORDER BY (fy IS NULL), fy DESC, filed_date DESC, accession DESC
        LIMIT 1
        """,
        (cik,),
    ).fetchone()


def load_candidates(
    conn: sqlite3.Connection,
    ticker_list: list[str] | None = None,
) -> list[Candidate]:
    """Return one Candidate per (company, latest filing) with its shortlisted buckets."""
    if ticker_list:
        wanted = {t.strip().upper() for t in ticker_list if t and t.strip()}
        placeholders = ",".join("?" * len(wanted))
        company_rows = conn.execute(
            f"SELECT cik, ticker, name FROM companies "
            f"WHERE is_candidate = 1 AND UPPER(ticker) IN ({placeholders})",
            tuple(wanted),
        ).fetchall()
    else:
        company_rows = conn.execute(
            "SELECT cik, ticker, name FROM companies WHERE is_candidate = 1"
        ).fetchall()

    out: list[Candidate] = []
    for c in company_rows:
        filing = _latest_filing_row(conn, c["cik"])
        if filing is None or filing["fy"] is None:
            continue
        bucket_rows = conn.execute(
            "SELECT bucket_id FROM shortlist WHERE cik = ? ORDER BY bucket_id",
            (c["cik"],),
        ).fetchall()
        bucket_ids = tuple(r["bucket_id"] for r in bucket_rows)
        if not bucket_ids:
            continue
        out.append(
            Candidate(
                cik=c["cik"],
                ticker=c["ticker"],
                name=c["name"] or c["ticker"],
                fy=int(filing["fy"]),
                accession=filing["accession"],
                item1_path=filing["item1_path"] or "",
                segments_json=filing["segments_json"] or "",
                bucket_ids=bucket_ids,
            )
        )
    return out


# ---- request construction --------------------------------------------------


def build_user_prompt(
    candidate: Candidate,
    *,
    user_template: str,
    bucket_defs: dict[str, dict[str, Any]],
    max_item1_chars: int,
) -> str:
    """Render the user prompt for a single candidate."""
    item1_text = _read_item1(candidate.item1_path, max_item1_chars)
    segments_table = _format_segments(candidate.segments_json)
    buckets = [bucket_defs[bid] for bid in candidate.bucket_ids if bid in bucket_defs]
    buckets_block = _format_buckets_block(buckets)
    return _fill_user_template(
        user_template,
        ticker=candidate.ticker,
        company_name=candidate.name,
        cik=candidate.cik,
        fy=candidate.fy,
        item1_text=item1_text,
        segment_table=segments_table,
        buckets_block=buckets_block,
    )


def build_batch_requests(
    candidates: list[Candidate],
    *,
    system_prompt: str,
    user_template: str,
    bucket_defs: dict[str, dict[str, Any]],
    model: str,
    max_item1_chars: int,
    runs: Iterable[int] = (1, 2),
    max_tokens: int = DEFAULT_MAX_TOKENS,
) -> list[dict[str, Any]]:
    """Return a plain-dict list of batch requests (SDK accepts dicts)."""
    reqs: list[dict[str, Any]] = []
    for cand in candidates:
        user = build_user_prompt(
            cand,
            user_template=user_template,
            bucket_defs=bucket_defs,
            max_item1_chars=max_item1_chars,
        )
        for run in runs:
            custom_id = f"{cand.ticker}_{cand.fy}_run{run}"
            reqs.append(
                {
                    "custom_id": custom_id,
                    "params": {
                        "model": model,
                        "max_tokens": max_tokens,
                        "temperature": 0,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user}],
                    },
                }
            )
    return reqs


def _chunk(lst: list[Any], size: int) -> list[list[Any]]:
    return [lst[i : i + size] for i in range(0, len(lst), size)]


# ---- cost estimate ----------------------------------------------------------


def _rough_input_tokens(req: dict[str, Any]) -> int:
    """~4 chars per token heuristic across system + user."""
    params = req["params"]
    sys_len = len(params.get("system") or "")
    msgs = params.get("messages") or []
    user_len = sum(len(m.get("content", "")) for m in msgs if isinstance(m, dict))
    return (sys_len + user_len) // 4


def estimate_cost(requests: list[dict[str, Any]], max_tokens: int) -> dict[str, float]:
    total_input = sum(_rough_input_tokens(r) for r in requests)
    total_output = len(requests) * max_tokens
    cost_in = total_input * _ROUGH_PRICE_PER_MTOK_INPUT / 1_000_000
    cost_out = total_output * _ROUGH_PRICE_PER_MTOK_OUTPUT / 1_000_000
    return {
        "n_requests": float(len(requests)),
        "input_tokens_est": float(total_input),
        "output_tokens_est_ceiling": float(total_output),
        "cost_usd_est": cost_in + cost_out,
    }


# ---- client & submit --------------------------------------------------------


def _default_client_factory() -> Any:
    import anthropic

    return anthropic.Anthropic(api_key=config.get_settings().anthropic_api_key)


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _confirm(assume_yes: bool, prompt: str = "Proceed? [type 'yes']: ") -> bool:
    if assume_yes:
        return True
    try:
        answer = input(prompt)
    except EOFError:
        return False
    return answer.strip().lower() == "yes"


def _submit_batches(
    client: Any,
    conn: sqlite3.Connection,
    requests: list[dict[str, Any]],
) -> list[str]:
    """Chunk requests, call the API, upsert batches rows. Returns batch ids."""
    batch_ids: list[str] = []
    for chunk in _chunk(requests, MAX_REQUESTS_PER_BATCH):
        batch = client.messages.batches.create(requests=chunk)
        batch_id = getattr(batch, "id", None) or (
            batch.get("id") if isinstance(batch, dict) else None
        )
        if not batch_id:
            raise RuntimeError(f"Anthropic API returned no batch id: {batch!r}")
        meta = {
            "n_requests": len(chunk),
            "custom_ids": [r["custom_id"] for r in chunk],
        }
        with db.transaction(conn):
            db.upsert_batch(
                conn,
                batch_id=batch_id,
                submitted_at=_now_iso(),
                status="in_progress",
                meta_json=json.dumps(meta),
            )
        batch_ids.append(batch_id)
    return batch_ids


def submit(
    ticker_list: list[str] | None = None,
    assume_yes: bool = False,
    *,
    client_factory: Callable[[], Any] = _default_client_factory,
    settings: config.Settings | None = None,
) -> list[str]:
    """Stage 5 submit — build batches for all (candidate, run) pairs and submit them.

    Returns the list of created batch ids.
    """
    s = settings or config.get_settings()
    system_prompt, user_template = _load_prompt_sections(s.prompt_path)
    bucket_defs = _load_bucket_defs(s.definitions_path)

    conn = db.connect(s.db_path)
    try:
        db.init_schema(conn)
        candidates = load_candidates(conn, ticker_list=ticker_list)
        if not candidates:
            print("score submit: no candidates with filings + shortlist rows; nothing to do.")
            return []

        reqs = build_batch_requests(
            candidates,
            system_prompt=system_prompt,
            user_template=user_template,
            bucket_defs=bucket_defs,
            model=s.batch_model,
            max_item1_chars=s.max_item1_chars,
        )

        est = estimate_cost(reqs, DEFAULT_MAX_TOKENS)
        print(
            f"score submit: {int(est['n_requests'])} requests "
            f"(~{int(est['input_tokens_est']):,} input tokens, "
            f"<= {int(est['output_tokens_est_ceiling']):,} output tokens); "
            f"rough cost estimate ${est['cost_usd_est']:.2f} "
            f"(model={s.batch_model}, 50% batch discount already applied)."
        )
        if not _confirm(assume_yes):
            print("score submit: aborted by user.")
            return []

        client = client_factory()
        batch_ids = _submit_batches(client, conn, reqs)
        print(f"score submit: created {len(batch_ids)} batch(es): {batch_ids}")
        return batch_ids
    finally:
        conn.close()


# ---- poll -------------------------------------------------------------------


def _strip_markdown_fence(text: str) -> str:
    """Strip an accidental ```json ... ``` fence around the model's JSON reply."""
    if text is None:
        return ""
    m = _FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


def _message_text(message: Any) -> str:
    """Concatenate all text blocks of an Anthropic Message response."""
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if content is None:
        return ""
    parts: list[str] = []
    for block in content:
        # block may be an SDK object or a dict
        typ = getattr(block, "type", None) or (block.get("type") if isinstance(block, dict) else None)
        if typ == "text":
            txt = getattr(block, "text", None) or (block.get("text") if isinstance(block, dict) else "")
            if txt:
                parts.append(txt)
    return "".join(parts)


def _parse_custom_id(cid: str) -> tuple[str, int, int] | None:
    """'TICKER_FY_runN' -> (ticker, fy, run) or None."""
    try:
        ticker, fy_s, run_s = cid.rsplit("_", 2)
        return ticker, int(fy_s), int(run_s.replace("run", ""))
    except Exception:
        return None


def _ticker_to_cik(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        r["ticker"].upper(): r["cik"]
        for r in conn.execute("SELECT ticker, cik FROM companies").fetchall()
    }


def _failures_path(s: config.Settings) -> Path:
    return s.data_dir / FAILURES_FILENAME


def _load_failures(s: config.Settings) -> list[dict[str, Any]]:
    p = _failures_path(s)
    if not p.exists():
        return []
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def _write_failures(s: config.Settings, failures: list[dict[str, Any]]) -> None:
    p = _failures_path(s)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(failures, indent=2), encoding="utf-8")


def _open_batches(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT batch_id, submitted_at, status, meta_json FROM batches "
        "WHERE status IS NULL OR status NOT IN ('ended', 'canceled', 'expired')"
    ).fetchall()


def _ingest_batch_results(
    conn: sqlite3.Connection,
    results_iter: Iterable[Any],
    *,
    prompt_version: str,
    default_model: str,
    ticker_cik: dict[str, str],
    failures: list[dict[str, Any]],
    seen_custom_ids: set[str],
) -> int:
    """Insert one score row per bucket for each successful result. Returns row count."""
    inserted = 0
    for result in results_iter:
        custom_id = getattr(result, "custom_id", None) or (
            result.get("custom_id") if isinstance(result, dict) else None
        )
        if not custom_id:
            continue
        seen_custom_ids.add(custom_id)

        result_obj = getattr(result, "result", None) or (
            result.get("result") if isinstance(result, dict) else None
        )
        rtype = getattr(result_obj, "type", None) or (
            result_obj.get("type") if isinstance(result_obj, dict) else None
        )

        if rtype != "succeeded":
            failures.append(
                {"custom_id": custom_id, "reason": f"result_type:{rtype}", "raw": None}
            )
            continue

        message = getattr(result_obj, "message", None) or (
            result_obj.get("message") if isinstance(result_obj, dict) else None
        )
        raw_text = _message_text(message)
        text = _strip_markdown_fence(raw_text)
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            failures.append(
                {"custom_id": custom_id, "reason": f"json_decode:{e.msg}", "raw": raw_text}
            )
            continue

        try:
            parsed = models.ScoringResponse.model_validate(payload)
        except ValidationError as e:
            failures.append(
                {"custom_id": custom_id, "reason": f"schema:{e.errors()[0]['msg']}", "raw": raw_text}
            )
            continue

        parts = _parse_custom_id(custom_id)
        if parts is None:
            failures.append(
                {"custom_id": custom_id, "reason": "custom_id_parse", "raw": raw_text}
            )
            continue
        ticker, fy, run = parts
        cik = ticker_cik.get(ticker.upper())
        if cik is None:
            failures.append(
                {"custom_id": custom_id, "reason": "unknown_ticker", "raw": raw_text}
            )
            continue

        # model_version: prefer whatever the API echoed, fall back to Settings default.
        api_model = getattr(message, "model", None) or (
            message.get("model") if isinstance(message, dict) else None
        )
        model_version = api_model or default_model
        now = _now_iso()

        with db.transaction(conn):
            for bs in parsed.scores:
                db.insert_score(
                    conn,
                    cik=cik,
                    ticker=ticker,
                    bucket_id=bs.bucket_id,
                    fy=parsed.fy or fy,
                    run=run,
                    score=bs.score,
                    confidence=bs.confidence,
                    rationale=bs.rationale,
                    evidence_type=bs.evidence_type,
                    pre_revenue=int(bool(parsed.pre_revenue)),
                    prompt_version=prompt_version,
                    model_version=model_version,
                    created_at=now,
                )
                inserted += 1
    return inserted


def poll(
    *,
    client_factory: Callable[[], Any] = _default_client_factory,
    settings: config.Settings | None = None,
) -> dict[str, Any]:
    """Fetch results for every open batch, insert scores, write failures file."""
    s = settings or config.get_settings()
    conn = db.connect(s.db_path)
    try:
        db.init_schema(conn)
        open_batches = _open_batches(conn)
        if not open_batches:
            print("score poll: no open batches.")
            return {"n_batches": 0, "n_inserted": 0, "n_failures": 0}

        client = client_factory()
        ticker_cik = _ticker_to_cik(conn)
        failures: list[dict[str, Any]] = []
        total_inserted = 0

        for row in open_batches:
            batch_id = row["batch_id"]
            batch = client.messages.batches.retrieve(batch_id)
            status = getattr(batch, "processing_status", None) or (
                batch.get("processing_status") if isinstance(batch, dict) else None
            )
            if status != "ended":
                print(f"score poll: batch {batch_id} still {status!r}; skipping.")
                continue

            seen: set[str] = set()
            try:
                meta = json.loads(row["meta_json"] or "{}")
            except json.JSONDecodeError:
                meta = {}
            expected: list[str] = list(meta.get("custom_ids") or [])

            results_iter = client.messages.batches.results(batch_id)
            inserted = _ingest_batch_results(
                conn,
                results_iter,
                prompt_version=s.prompt_version,
                default_model=s.batch_model,
                ticker_cik=ticker_cik,
                failures=failures,
                seen_custom_ids=seen,
            )
            total_inserted += inserted

            # Any request the batch didn't return counts as a failure.
            for cid in expected:
                if cid not in seen:
                    failures.append(
                        {"custom_id": cid, "reason": "missing_from_batch_results", "raw": None}
                    )

            with db.transaction(conn):
                db.upsert_batch(
                    conn,
                    batch_id=batch_id,
                    submitted_at=row["submitted_at"],
                    status="ended",
                    meta_json=row["meta_json"],
                )

        if failures:
            _write_failures(s, failures)
        print(
            f"score poll: inserted {total_inserted} score rows, "
            f"{len(failures)} failures (see {_failures_path(s) if failures else '-'})."
        )
        return {
            "n_batches": len(open_batches),
            "n_inserted": total_inserted,
            "n_failures": len(failures),
        }
    finally:
        conn.close()


# ---- retry ------------------------------------------------------------------


def retry(
    *,
    assume_yes: bool = True,
    client_factory: Callable[[], Any] = _default_client_factory,
    settings: config.Settings | None = None,
) -> list[str]:
    """Resubmit failures from data/score_failures.json as a new batch.

    We rebuild the request from scratch (same prompt construction as submit)
    for each failing custom_id we can still locate in the DB. On successful
    submission the failures file is cleared.
    """
    s = settings or config.get_settings()
    failures = _load_failures(s)
    if not failures:
        print("score retry: no failures to retry.")
        return []

    # Preserve run number and (ticker, fy) from the custom_id.
    wanted_ids: list[str] = [f["custom_id"] for f in failures if f.get("custom_id")]
    parsed: list[tuple[str, str, int, int]] = []  # (custom_id, ticker, fy, run)
    for cid in wanted_ids:
        p = _parse_custom_id(cid)
        if p is None:
            continue
        ticker, fy, run = p
        parsed.append((cid, ticker, fy, run))

    if not parsed:
        print("score retry: no parseable custom_ids in failures file.")
        return []

    tickers = sorted({t for _, t, _, _ in parsed})

    system_prompt, user_template = _load_prompt_sections(s.prompt_path)
    bucket_defs = _load_bucket_defs(s.definitions_path)

    conn = db.connect(s.db_path)
    try:
        db.init_schema(conn)
        candidates = load_candidates(conn, ticker_list=tickers)
        by_ticker = {c.ticker.upper(): c for c in candidates}

        # Build one request per failed custom_id, matching its run number.
        reqs: list[dict[str, Any]] = []
        skipped: list[str] = []
        for cid, ticker, fy, run in parsed:
            cand = by_ticker.get(ticker.upper())
            if cand is None:
                skipped.append(cid)
                continue
            user = build_user_prompt(
                cand,
                user_template=user_template,
                bucket_defs=bucket_defs,
                max_item1_chars=s.max_item1_chars,
            )
            reqs.append(
                {
                    "custom_id": cid,
                    "params": {
                        "model": s.batch_model,
                        "max_tokens": DEFAULT_MAX_TOKENS,
                        "temperature": 0,
                        "system": system_prompt,
                        "messages": [{"role": "user", "content": user}],
                    },
                }
            )

        if not reqs:
            print(f"score retry: nothing to submit (skipped {len(skipped)}).")
            return []

        est = estimate_cost(reqs, DEFAULT_MAX_TOKENS)
        print(
            f"score retry: {int(est['n_requests'])} requests "
            f"(~${est['cost_usd_est']:.2f} rough estimate)."
        )
        if not _confirm(assume_yes):
            print("score retry: aborted by user.")
            return []

        client = client_factory()
        batch_ids = _submit_batches(client, conn, reqs)
        _write_failures(s, [])  # clear on successful submission
        print(f"score retry: created {len(batch_ids)} batch(es): {batch_ids}")
        return batch_ids
    finally:
        conn.close()
