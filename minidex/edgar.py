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
    edgar = config.require_scoring("edgar")

    edgar.set_identity(user_agent)


def _get_company(cik: str) -> Any:
    edgar = config.require_scoring("edgar")

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


_REVENUE_CONCEPTS = frozenset({
    "us-gaap:Revenues",
    "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    "us-gaap:RevenueFromContractWithCustomerIncludingAssessedTax",
    "us-gaap:SalesRevenueNet",
})

_SEG_DIM_COL = "dim_us-gaap_StatementBusinessSegmentsAxis"


def _extract_total_revenue(company: Any) -> dict | None:
    """Fallback for single-segment issuers: total FY revenue via curated concept.

    Returns a single {segment, revenue, period} dict or None. Tries the two
    most recent fiscal years since a company's latest filing may still be
    the prior FY around annual-report season.
    """
    try:
        facts = company.get_facts()
    except Exception as exc:
        logger.debug("get_facts failed in total-revenue fallback: %s", exc)
        return None
    if facts is None:
        return None
    from datetime import date

    year = date.today().year
    for fy in (year, year - 1, year - 2):
        try:
            r = facts.get_concept("revenue", period=f"{fy}-FY", return_metadata=True)
        except Exception:
            r = None
        if not r or r.get("value") is None:
            continue
        val = float(r["value"])
        # Sanity gate: reject values above $1T. Walmart (largest US-listed by
        # revenue) is under $700B; anything past $1T is a unit / currency error
        # (seen on IFRS filers with base-currency multipliers, e.g. ARQQ).
        if val > 1_000_000_000_000:
            continue
        period = r.get("period_end")
        return {
            "segment": "Total revenue",
            "revenue": val,
            "period": str(period) if period is not None else "",
        }
    return None


def _extract_segments(filing: Any, company: Any = None) -> list[dict]:
    """Segment revenue per reporting segment for the newest fiscal year.

    Reads the filing's XBRL facts as a DataFrame with dimensions, filters to
    revenue concepts tagged with the StatementBusinessSegmentsAxis, and
    returns one row per segment for the most recent period_end. For
    single-segment issuers (no segment axis in XBRL), falls back to a single
    'Total revenue' entry via the curated `revenue` concept on company facts
    if ``company`` is provided.
    """
    def _fallback() -> list[dict]:
        if company is None:
            return []
        r = _extract_total_revenue(company)
        return [r] if r else []

    try:
        xbrl = filing.xbrl()
    except Exception as exc:
        logger.debug("filing.xbrl() failed: %s", exc)
        return _fallback()
    if xbrl is None:
        return _fallback()

    try:
        df = xbrl.query(include_dimensions=True).to_dataframe()
    except Exception as exc:
        logger.debug("xbrl.query().to_dataframe() failed: %s", exc)
        return _fallback()
    if df is None or len(df) == 0 or _SEG_DIM_COL not in df.columns:
        return _fallback()

    mask = df["concept"].isin(_REVENUE_CONCEPTS) & df[_SEG_DIM_COL].notna()
    seg_df = df[mask]
    if len(seg_df) == 0:
        return _fallback()

    # Newest period_end only — one row per segment.
    try:
        latest_period = seg_df["period_end"].max()
    except Exception:
        return []
    seg_df = seg_df[seg_df["period_end"] == latest_period]

    out: list[dict] = []
    seen: set[str] = set()
    for _, row in seg_df.iterrows():
        key = row.get(_SEG_DIM_COL)  # e.g. "nvda:ComputeAndNetworkingSegmentMember"
        display = row.get("dimension_label") or row.get("dimension_member_label") or key
        val = row.get("numeric_value")
        if key is None or val is None or display is None:
            continue
        key = str(key).strip()
        display = str(display).strip()
        if not key or key in seen or not display:
            continue
        try:
            revenue = float(val)
        except (TypeError, ValueError):
            continue
        seen.add(key)
        out.append(
            {
                "segment": display,
                "revenue": revenue,
                "period": str(latest_period) if latest_period is not None else "",
            }
        )
    return out


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


def _extract_market_cap(company: Any, ticker: str) -> tuple[float, str] | None:
    """Return (market_cap, asof_yyyy_mm_dd) from EDGAR shares × yfinance price.

    SEC XBRL doesn't publish market cap or share price, so we multiply the
    curated `common_shares_outstanding` fact from edgartools by the last
    close price from yfinance. Fails silently on any missing input.
    """
    try:
        facts = company.get_facts()
    except Exception as exc:
        logger.debug("get_facts failed: %s", exc)
        return None
    if facts is None:
        return None

    try:
        shares_meta = facts.get_concept("common_shares_outstanding", return_metadata=True)
    except Exception as exc:
        logger.debug("get_concept(common_shares_outstanding) failed: %s", exc)
        return None
    if not shares_meta or shares_meta.get("value") is None:
        return None
    shares_val = float(shares_meta["value"])
    shares_asof = shares_meta.get("period_end") or shares_meta.get("filing_date")

    price, price_asof = _latest_close_yf(ticker)
    if price is None:
        return None
    asof = price_asof or _fmt_date(shares_asof) or ""
    return shares_val * float(price), asof


def _latest_close_yf(ticker: str) -> tuple[float | None, str | None]:
    """Last close via yfinance. Lazy import; returns (None, None) on any failure."""
    try:
        import yfinance as yf
    except ImportError:
        logger.debug("yfinance not installed; skipping market cap for %s", ticker)
        return None, None
    try:
        hist = yf.Ticker(ticker).history(period="5d", auto_adjust=False)
        if hist is None or hist.empty:
            return None, None
        row = hist.iloc[-1]
        return float(row["Close"]), row.name.date().isoformat()
    except Exception as exc:
        logger.debug("yfinance history failed for %s: %s", ticker, exc)
        return None, None


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
def run(ticker_list: list[str] | None = None) -> None:
    settings = config.get_settings()
    _ensure_identity(settings.sec_user_agent)
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(message)s")

    with db.connect(settings.db_path) as conn:
        db.init_schema(conn)
        if ticker_list:
            allowed = {t.strip().upper() for t in ticker_list if t.strip()}
            placeholders = ",".join("?" * len(allowed))
            candidates = conn.execute(
                f"SELECT cik, ticker, name FROM companies "
                f"WHERE is_candidate = 1 AND UPPER(ticker) IN ({placeholders})",
                tuple(allowed),
            ).fetchall()
            missing = allowed - {r["ticker"].upper() for r in candidates}
            if missing:
                print(f"fetch: skipping {len(missing)} tickers not in candidates: {sorted(missing)}")
        else:
            candidates = conn.execute(
                "SELECT cik, ticker, name FROM companies WHERE is_candidate = 1"
            ).fetchall()

        existing_pairs = {
            (row["cik"], row["accession"])
            for row in conn.execute("SELECT cik, accession FROM filings").fetchall()
        }
        seen_ciks_with_filing = {cik for (cik, _) in existing_pairs}
        ciks_with_mcap = {
            r["cik"] for r in conn.execute(
                "SELECT cik FROM companies WHERE market_cap IS NOT NULL"
            ).fetchall()
        }
        # (cik, accession) rows whose segments are still empty and worth re-attempting.
        empty_seg_rows = {
            (r["cik"], r["accession"])
            for r in conn.execute(
                "SELECT cik, accession FROM filings WHERE segments_json IS NULL OR segments_json IN ('', '[]')"
            ).fetchall()
        }

        n_candidates = len(candidates)
        n_new_filings = 0
        n_fallback = 0
        n_skipped = 0
        n_market_cap = 0
        n_segments_backfilled = 0

        print(f"fetch: {n_candidates} candidates to process ({len(seen_ciks_with_filing)} already cached)")

        for idx, cand in enumerate(candidates, 1):
            cik = cand["cik"]
            ticker = cand["ticker"]

            filing_cached = cik in seen_ciks_with_filing
            need_mcap = cik not in ciks_with_mcap
            need_segments = any(cik == p[0] for p in empty_seg_rows)
            if filing_cached and not need_mcap and not need_segments:
                n_skipped += 1
                continue

            try:
                company = _get_company(cik)
            except Exception as exc:
                logger.warning("[%s] Company lookup failed: %s", ticker, exc)
                continue

            filing_for_backfill = None
            if not filing_cached:
                filing = _latest_annual_filing(company)
                if filing is None:
                    logger.info("[%s] no 10-K or 20-F found", ticker)
                elif not getattr(filing, "accession_number", None):
                    logger.warning("[%s] filing missing accession_number", ticker)
                else:
                    accession = str(filing.accession_number)
                    if (cik, accession) in existing_pairs:
                        n_skipped += 1
                    else:
                        used_fallback = _process_and_store(
                            conn=conn, settings=settings, cik=cik,
                            ticker=ticker, filing=filing, accession=accession,
                            company=company,
                        )
                        if used_fallback:
                            n_fallback += 1
                        n_new_filings += 1
                        existing_pairs.add((cik, accession))
                        seen_ciks_with_filing.add(cik)
            elif need_segments:
                filing_for_backfill = _latest_annual_filing(company)

            if filing_for_backfill is not None:
                segments = _extract_segments(filing_for_backfill, company=company)
                if segments:
                    accession = str(getattr(filing_for_backfill, "accession_number", "") or "")
                    if accession:
                        conn.execute(
                            "UPDATE filings SET segments_json = ? WHERE cik = ? AND accession = ?",
                            (json.dumps(segments, separators=(",", ":")), cik, accession),
                        )
                        n_segments_backfilled += 1

            mc = _extract_market_cap(company, ticker) if need_mcap else None
            if mc is not None:
                value, asof = mc
                db.upsert_company(
                    conn, cik=cik, ticker=ticker, market_cap=value, market_cap_asof=asof
                )
                n_market_cap += 1

            conn.commit()

            if idx % 10 == 0 or idx == n_candidates:
                print(
                    f"fetch: {idx}/{n_candidates} processed | "
                    f"new={n_new_filings} skipped={n_skipped} fallback={n_fallback} "
                    f"mcap={n_market_cap} seg_bf={n_segments_backfilled}"
                )

        print(
            f"fetch: done. candidates={n_candidates} new_filings={n_new_filings} "
            f"fallback={n_fallback} skipped={n_skipped} market_cap={n_market_cap} "
            f"segments_backfilled={n_segments_backfilled}"
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
    company: Any = None,
) -> bool:
    """Extract text + segments and write filings row + raw text file.

    Returns True if Item 1 parsing fell back to the raw filing body.
    """
    text, used_fallback = _extract_item1(filing)
    segments = _extract_segments(filing, company=company)

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
