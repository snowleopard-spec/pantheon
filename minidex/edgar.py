"""Stage 3 — fetch latest 10-K/20-F per candidate and cache Item 1 + segments.

Idempotent: re-running skips (cik, accession) pairs already in `filings`, and
does zero network I/O when everything is already cached.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Iterable

from minidex import config, db

logger = logging.getLogger(__name__)

FALLBACK_CHARS = 40_000
FALLBACK_SUFFIX = "_fallback"


# ---------------------------------------------------------------------------
# edgartools wrappers (thin so tests can monkeypatch)
# ---------------------------------------------------------------------------
def _ensure_identity(user_agent: str) -> None:
    """Set the SEC user agent for edgartools before any network call."""
    import edgar

    edgar.set_identity(user_agent)


def _get_company(cik: str) -> Any:
    import edgar

    return edgar.Company(cik)


def _latest_annual_filing(company: Any) -> Any | None:
    """Return the newest 10-K filing, or 20-F if there is no 10-K, else None."""
    for form in ("10-K", "20-F"):
        try:
            filings = company.get_filings(form=form)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("get_filings(form=%s) failed: %s", form, exc)
            continue
        if filings is None:
            continue
        try:
            latest = filings.latest(1)
        except Exception:
            latest = None
        if latest is None:
            # Some edgartools versions return an empty Filings; fall back to iter.
            try:
                latest = next(iter(filings), None)
            except Exception:
                latest = None
        if latest is not None:
            return latest
    return None


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------
def _extract_item1(filing: Any) -> tuple[str, bool]:
    """Return (item1_text, used_fallback).

    Tries `filing.obj().business` first (works for 10-K/20-F wrappers), then
    falls back to the first FALLBACK_CHARS of the filing body.
    """
    text = ""
    try:
        obj = filing.obj()
    except Exception as exc:
        logger.debug("filing.obj() failed: %s", exc)
        obj = None
    if obj is not None:
        for attr in ("business", "item_1", "item1"):
            try:
                val = getattr(obj, attr, None)
            except Exception:
                val = None
            if val:
                text = str(val).strip()
                if text:
                    return text, False

    # Fallback: raw text of the filing.
    body = ""
    for method in ("text", "markdown"):
        try:
            fn = getattr(filing, method, None)
            body = fn() if callable(fn) else (fn or "")
        except Exception:
            body = ""
        if body:
            break
    return body[:FALLBACK_CHARS].strip(), True


def _extract_segments(filing: Any) -> list[dict]:
    """Best-effort segment revenue extraction. Empty list on any failure.

    Looks for XBRL facts tagged with segment axis members. Different filings
    expose this differently; we probe a couple of common shapes and give up
    gracefully otherwise.
    """
    try:
        xbrl = filing.xbrl()
    except Exception:
        return []
    if xbrl is None:
        return []

    # Preferred path: xbrl.query() for revenue-like concepts by segment.
    try:
        facts = xbrl.facts  # may be dict-like or a helper object
    except Exception:
        facts = None

    results: list[dict] = []
    try:
        # Try common shapes without assuming edgartools internals.
        iterable: Iterable = []
        if facts is None:
            iterable = []
        elif hasattr(facts, "query"):
            iterable = list(facts.query(concept="Revenues"))
        elif isinstance(facts, dict):
            iterable = list(facts.values())
        else:
            iterable = list(facts)
        for fact in iterable:
            seg = _fact_segment(fact)
            rev = _fact_number(fact)
            period = _fact_period(fact)
            if seg and rev is not None:
                results.append({"segment": seg, "revenue": float(rev), "period": period})
    except Exception as exc:
        logger.debug("segment extraction failed: %s", exc)
        return []
    return results


def _fact_segment(fact: Any) -> str | None:
    for attr in ("segment", "member", "dimension"):
        val = getattr(fact, attr, None)
        if val:
            return str(val)
    if isinstance(fact, dict):
        for k in ("segment", "member", "dimension"):
            if fact.get(k):
                return str(fact[k])
    return None


def _fact_number(fact: Any) -> float | None:
    for attr in ("value", "numeric_value", "amount"):
        val = getattr(fact, attr, None)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                continue
    if isinstance(fact, dict):
        for k in ("value", "numeric_value", "amount"):
            if fact.get(k) is not None:
                try:
                    return float(fact[k])
                except (TypeError, ValueError):
                    continue
    return None


def _fact_period(fact: Any) -> str | None:
    for attr in ("period", "end_date", "period_end"):
        val = getattr(fact, attr, None)
        if val:
            return str(val)
    if isinstance(fact, dict):
        for k in ("period", "end_date", "period_end"):
            if fact.get(k):
                return str(fact[k])
    return None


def _extract_market_cap(company: Any) -> tuple[float, str] | None:
    """Return (market_cap, asof_yyyy_mm_dd) if derivable from company facts.

    Uses shares outstanding × any recent price found via edgartools. Skips
    silently on any failure.
    """
    try:
        facts = company.get_facts()
    except Exception as exc:
        logger.debug("get_facts failed: %s", exc)
        return None
    if facts is None:
        return None

    shares = _pick_recent_fact(facts, ("CommonStockSharesOutstanding", "EntityCommonStockSharesOutstanding"))
    price = _pick_recent_fact(facts, ("SharePrice", "ClosePrice"))
    market_cap_direct = _pick_recent_fact(facts, ("MarketCapitalization",))

    if market_cap_direct is not None:
        value, asof = market_cap_direct
        return float(value), _fmt_date(asof)

    if shares is not None and price is not None:
        s_val, s_asof = shares
        p_val, _ = price
        return float(s_val) * float(p_val), _fmt_date(s_asof)

    return None


def _pick_recent_fact(facts: Any, concepts: tuple[str, ...]) -> tuple[float, str] | None:
    """Extract (value, asof) for the most recent occurrence of any concept."""
    for concept in concepts:
        rows = _facts_rows(facts, concept)
        best: tuple[float, str] | None = None
        for row in rows:
            val = _fact_number(row)
            asof = _fact_period(row)
            if val is None or asof is None:
                continue
            if best is None or asof > best[1]:
                best = (val, asof)
        if best is not None:
            return best
    return None


def _facts_rows(facts: Any, concept: str) -> list[Any]:
    """Return facts for a given concept across a few known shapes."""
    try:
        if hasattr(facts, "query"):
            return list(facts.query(concept=concept))
        if hasattr(facts, "get_fact"):
            r = facts.get_fact(concept)
            return list(r) if r is not None else []
        if isinstance(facts, dict):
            v = facts.get(concept)
            if v is None:
                return []
            return list(v) if isinstance(v, list) else [v]
    except Exception:
        return []
    return []


def _fmt_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    s = str(value)
    return s[:10]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
def run() -> None:
    settings = config.get_settings()
    _ensure_identity(settings.sec_user_agent)

    with db.connect(settings.db_path) as conn:
        db.init_schema(conn)
        candidates = conn.execute(
            "SELECT cik, ticker, name FROM companies WHERE is_candidate = 1"
        ).fetchall()

        existing_pairs = {
            (row["cik"], row["accession"])
            for row in conn.execute("SELECT cik, accession FROM filings").fetchall()
        }
        seen_ciks_with_filing = {cik for (cik, _) in existing_pairs}

        n_candidates = len(candidates)
        n_new_filings = 0
        n_fallback = 0
        n_skipped = 0
        n_market_cap = 0

        for cand in candidates:
            cik = cand["cik"]
            ticker = cand["ticker"]

            # Idempotency: if this CIK already has any filing row, skip network.
            if cik in seen_ciks_with_filing:
                n_skipped += 1
                continue

            try:
                company = _get_company(cik)
            except Exception as exc:
                logger.warning("[%s] Company lookup failed: %s", ticker, exc)
                continue

            filing = _latest_annual_filing(company)
            if filing is None:
                logger.info("[%s] no 10-K or 20-F found", ticker)
                continue

            accession = str(getattr(filing, "accession_number", "") or "")
            if not accession:
                logger.warning("[%s] filing missing accession_number", ticker)
                continue

            if (cik, accession) in existing_pairs:
                n_skipped += 1
                continue

            used_fallback = _process_and_store(
                conn=conn,
                settings=settings,
                cik=cik,
                ticker=ticker,
                filing=filing,
                accession=accession,
            )
            if used_fallback:
                n_fallback += 1
            n_new_filings += 1
            existing_pairs.add((cik, accession))
            seen_ciks_with_filing.add(cik)

            mc = _extract_market_cap(company)
            if mc is not None:
                value, asof = mc
                db.upsert_company(
                    conn, cik=cik, ticker=ticker, market_cap=value, market_cap_asof=asof
                )
                n_market_cap += 1

            conn.commit()

        logger.info(
            "fetch: candidates=%d new_filings=%d fallback=%d skipped=%d market_cap=%d",
            n_candidates,
            n_new_filings,
            n_fallback,
            n_skipped,
            n_market_cap,
        )


def _item1_path(raw_dir: Path, accession: str, used_fallback: bool) -> Path:
    suffix = f"{FALLBACK_SUFFIX}.txt" if used_fallback else ".txt"
    return raw_dir / f"{accession}_item1{suffix}"


def _process_and_store(
    *,
    conn,
    settings,
    cik: str,
    ticker: str,
    filing: Any,
    accession: str,
) -> bool:
    """Extract text + segments and write filings row + raw text file.

    Returns True if Item 1 parsing fell back to the raw filing body.
    """
    text, used_fallback = _extract_item1(filing)
    segments = _extract_segments(filing)

    out_path = _item1_path(settings.raw_dir, accession, used_fallback)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(text or "", encoding="utf-8")

    fy = _period_year(filing)
    filed_date = _fmt_date(getattr(filing, "filing_date", "") or "")

    segments_json = json.dumps(segments, separators=(",", ":"))

    db.upsert_filing(
        conn,
        cik=cik,
        accession=accession,
        fy=fy,
        filed_date=filed_date,
        item1_path=str(out_path),
        item1_chars=len(text or ""),
        segments_json=segments_json,
    )
    return used_fallback


def _period_year(filing: Any) -> int | None:
    for attr in ("period_of_report", "filing_date"):
        val = getattr(filing, attr, None)
        if not val:
            continue
        s = _fmt_date(val)
        if len(s) >= 4 and s[:4].isdigit():
            return int(s[:4])
    return None
