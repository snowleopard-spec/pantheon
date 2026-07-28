"""Optional Stage 3.5 — LLM extraction of revenue disaggregation.

For filings where the deterministic XBRL extractor returned nothing or only a
'Total revenue' single-segment fallback, ask a Haiku-class model to find
explicit dollar or percentage revenue splits stated anywhere in the 10-K
text. Strict prompt: return [] rather than fabricate.

Submits via the Anthropic Message Batches API (same infra as score.py).
Writes results back into filings.segments_json, replacing the empty or
total-only row.
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from minidex import config, db

# ---- prompt -----------------------------------------------------------------

SYSTEM_PROMPT = """You are extracting quantified revenue disaggregation from a US-listed company's 10-K annual report. Your only job is to find sentences that explicitly quantify revenue by product line, geography, end-market, or customer type.

Rules:
1. ONLY extract statements the filing directly makes with dollar amounts, or with percentages of total revenue that can be converted to dollars.
2. DO NOT infer, estimate, or extrapolate. If the filing describes product lines qualitatively without numbers, do not include them.
3. If the filing only states "we operate as one reportable segment" and gives no product-line, geographic, or customer quantification anywhere in the text, return an empty segments list.
4. When percentages are given, convert to USD using the stated total revenue.
5. Prefer the most recent fiscal year's numbers.
6. Deduplicate: don't repeat the same dimension under multiple labels.
7. Respond with ONLY a JSON object matching the schema. No prose, no markdown fences.
"""

USER_TEMPLATE = """COMPANY
Ticker: {ticker}
Name: {company_name}
Fiscal year: {fy}

TOTAL REVENUE (from prior XBRL extraction, for reference)
{total_revenue_line}

10-K TEXT (Item 1 Business Description, may be truncated)
{item1_text}

TASK
Extract quantified revenue disaggregation per the system rules.

OUTPUT SCHEMA (respond with exactly this JSON structure):
{{
  "ticker": "{ticker}",
  "fy": {fy},
  "segments": [
    {{"segment": "string label", "revenue_usd": 0, "period": "YYYY-MM-DD"}}
  ]
}}

If no quantified disaggregation exists in the text, return "segments": [].
"""

DEFAULT_MAX_TOKENS = 1024
_ROUGH_PRICE_PER_MTOK_INPUT = 0.50
_ROUGH_PRICE_PER_MTOK_OUTPUT = 2.50
_FENCE_RE = re.compile(r"^\s*```(?:json)?\s*\n?(.*?)\n?```\s*$", re.DOTALL | re.IGNORECASE)
BATCH_KIND = "llm_segments"

# ---- schema -----------------------------------------------------------------


class Segment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    segment: str
    revenue_usd: float = Field(ge=0)
    period: str


class LLMSegmentsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str
    fy: int
    segments: list[Segment]


# ---- DB helpers -------------------------------------------------------------


def _candidates(conn: sqlite3.Connection) -> list[dict]:
    """Filings that need LLM refinement: no segments or only a Total revenue row."""
    rows = conn.execute(
        """
        SELECT c.cik, c.ticker, c.name, f.fy, f.item1_path, f.segments_json, f.accession
        FROM companies c JOIN filings f ON f.cik = c.cik
        WHERE c.is_candidate = 1
          AND (
            f.segments_json IS NULL
            OR f.segments_json IN ('', '[]')
            OR (f.segments_json LIKE '%"Total revenue"%' AND f.segments_json NOT LIKE '%,{%')
          )
        """
    ).fetchall()
    return [dict(r) for r in rows]


def _format_total_revenue_line(current_segments_json: str | None) -> str:
    if not current_segments_json or current_segments_json == "[]":
        return "NOT AVAILABLE"
    try:
        segs = json.loads(current_segments_json)
    except (json.JSONDecodeError, TypeError):
        return "NOT AVAILABLE"
    if not isinstance(segs, list) or not segs:
        return "NOT AVAILABLE"
    r = segs[0]
    return f"{r.get('segment', '?')}: ${r.get('revenue', 0):,.0f} (period {r.get('period', '')})"


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
    return text[:max_chars]


def _build_request(cand: dict, item1: str, total_line: str, model: str) -> dict:
    ticker = cand["ticker"]
    fy = cand["fy"] or 0
    user = USER_TEMPLATE.format(
        ticker=ticker,
        company_name=cand["name"] or ticker,
        fy=fy,
        item1_text=item1,
        total_revenue_line=total_line,
    )
    return {
        "custom_id": f"seg_{ticker}_{fy}",
        "params": {
            "model": model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0,
        },
    }


# ---- submit / poll ----------------------------------------------------------


def submit(
    *,
    assume_yes: bool = False,
    client_factory: Callable[[], Any] | None = None,
    settings: Any = None,
) -> None:
    s = settings if settings is not None else config.get_settings()
    conn = db.connect(s.db_path)
    try:
        db.init_schema(conn)
        cands = _candidates(conn)
        if not cands:
            print("llm_segments submit: no candidates needing refinement.")
            return

        requests: list[dict] = []
        for c in cands:
            item1 = _read_item1(c["item1_path"], s.max_item1_chars)
            total_line = _format_total_revenue_line(c["segments_json"])
            requests.append(_build_request(c, item1, total_line, s.batch_model))

        n = len(requests)
        est_input = sum(
            (len(r["params"]["system"]) + len(r["params"]["messages"][0]["content"])) // 4
            for r in requests
        )
        est_output_ceiling = n * DEFAULT_MAX_TOKENS
        cost = (
            est_input / 1e6 * _ROUGH_PRICE_PER_MTOK_INPUT
            + est_output_ceiling / 1e6 * _ROUGH_PRICE_PER_MTOK_OUTPUT
        )
        print(
            f"llm_segments submit: {n} requests, ~{est_input:,} input tokens, "
            f"<= {est_output_ceiling:,} output tokens; rough cost ${cost:.2f} "
            f"(model={s.batch_model}, 50% batch discount already applied)."
        )
        if not assume_yes:
            resp = input("Type 'yes' to submit: ")
            if resp.strip().lower() != "yes":
                print("llm_segments submit: cancelled.")
                return

        if client_factory is None:
            anthropic = config.require_scoring("anthropic")

            client = anthropic.Anthropic()
        else:
            client = client_factory()

        batch = client.messages.batches.create(requests=requests)
        meta = {
            "custom_ids": [r["custom_id"] for r in requests],
            "kind": BATCH_KIND,
        }
        db.upsert_batch(
            conn,
            batch_id=batch.id,
            submitted_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            status="in_progress",
            meta_json=json.dumps(meta, separators=(",", ":")),
        )
        conn.commit()
        print(f"llm_segments submit: created batch {batch.id}")
    finally:
        conn.close()


def poll(
    *,
    client_factory: Callable[[], Any] | None = None,
    settings: Any = None,
) -> None:
    s = settings if settings is not None else config.get_settings()
    conn = db.connect(s.db_path)
    try:
        rows = conn.execute(
            "SELECT batch_id, meta_json FROM batches "
            "WHERE status NOT IN ('ended', 'canceled', 'expired')"
        ).fetchall()
        open_seg_batches: list[tuple[str, dict]] = []
        for r in rows:
            try:
                meta = json.loads(r["meta_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if meta.get("kind") == BATCH_KIND:
                open_seg_batches.append((r["batch_id"], meta))
        if not open_seg_batches:
            print("llm_segments poll: no open segment batches.")
            return

        if client_factory is None:
            anthropic = config.require_scoring("anthropic")

            client = anthropic.Anthropic()
        else:
            client = client_factory()

        for batch_id, meta in open_seg_batches:
            b = client.messages.batches.retrieve(batch_id)
            status = getattr(b, "processing_status", "")
            if status != "ended":
                print(f"llm_segments poll: {batch_id} status={status}, skipping.")
                continue

            n_updated = 0
            n_empty = 0
            n_failures = 0
            for individual in client.messages.batches.results(batch_id):
                cid = getattr(individual, "custom_id", "") or ""
                result = getattr(individual, "result", None)
                if result is None or getattr(result, "type", "") != "succeeded":
                    n_failures += 1
                    continue
                msg = getattr(result, "message", None)
                if msg is None:
                    n_failures += 1
                    continue

                text_parts: list[str] = []
                for block in getattr(msg, "content", []) or []:
                    txt = getattr(block, "text", None)
                    if txt:
                        text_parts.append(txt)
                raw = "".join(text_parts).strip()
                m = _FENCE_RE.match(raw)
                if m:
                    raw = m.group(1).strip()

                try:
                    parsed = LLMSegmentsResponse.model_validate_json(raw)
                except ValidationError:
                    n_failures += 1
                    continue

                # custom_id = seg_{TICKER}_{FY}
                parts = cid.split("_")
                if len(parts) < 3 or parts[0] != "seg":
                    n_failures += 1
                    continue
                ticker = "_".join(parts[1:-1])
                try:
                    fy = int(parts[-1])
                except ValueError:
                    n_failures += 1
                    continue

                if not parsed.segments:
                    n_empty += 1
                    continue

                out = [
                    {"segment": seg.segment, "revenue": seg.revenue_usd, "period": seg.period}
                    for seg in parsed.segments
                ]
                conn.execute(
                    """
                    UPDATE filings SET segments_json = ?
                     WHERE cik IN (SELECT cik FROM companies WHERE UPPER(ticker) = UPPER(?))
                       AND fy = ?
                    """,
                    (json.dumps(out, separators=(",", ":")), ticker, fy),
                )
                n_updated += 1

            conn.execute(
                "UPDATE batches SET status = 'ended' WHERE batch_id = ?", (batch_id,)
            )
            conn.commit()
            print(
                f"llm_segments poll: batch {batch_id} → updated={n_updated} "
                f"empty={n_empty} failures={n_failures}"
            )
    finally:
        conn.close()
