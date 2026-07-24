from __future__ import annotations

from pathlib import Path

import yaml

from minidex import config, db

# Inclusion list of SIC ranges / codes for Stage 2. Err inclusive.
# Each entry is either an int (exact SIC) or a (lo, hi) inclusive range.
SIC_INCLUDE: list[int | tuple[int, int]] = [
    (3510, 3519),  # engines & turbines
    (3620, 3629),  # electrical industrial apparatus
    (3660, 3669),  # communications equipment
    (3670, 3679),  # electronic components (incl. 3674 semiconductors)
    (3690, 3699),  # misc electrical machinery / equipment
    (3570, 3579),  # computers & office / storage (357x)
    (3820, 3829),  # measuring & controlling instruments (incl. 3825)
    (4810, 4819),  # telephone / telecom
    4911,          # electric services
    4991,          # cogeneration / small power producers
    6798,          # REITs
    (7370, 7379),  # computer services / software (incl. 7372, 7370, 7371, 7372, 7374, 7379)
    (3600, 3600),  # broad electrical & electronic machinery header code
]


def _in_include(sic: str | None) -> bool:
    if not sic:
        return False
    try:
        n = int(sic)
    except (TypeError, ValueError):
        return False
    for item in SIC_INCLUDE:
        if isinstance(item, tuple):
            lo, hi = item
            if lo <= n <= hi:
                return True
        elif n == item:
            return True
    return False


def _load_anchor_tickers(path: Path) -> set[str]:
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for b in doc.get("buckets", []) or []:
        for t in b.get("anchors", []) or []:
            out.add(str(t).strip().upper())
    return out


def _pad_cik(cik) -> str:
    return str(int(cik)).zfill(10)


def _ticker_rank(ticker: str) -> tuple[int, int]:
    """Preference key when a CIK has multiple ticker rows.

    Lower is better. Prefer common-stock forms (no dash, no 'W'/'WT'/'WS'
    warrant suffix, no preferred `-P?` variants) and, tie-breaker, shorter.
    """
    t = ticker.upper()
    penalty = 0
    if "-" in t or "." in t:
        penalty += 10
    if t.endswith(("W", "WT", "WS")) and len(t) > 3:
        penalty += 5
    if t.endswith("F") and len(t) == 5:  # foreign OTC "F" suffix (e.g. STMEF, TSMWF)
        penalty += 3
    return (penalty, len(t))


def _dedupe_by_cik(rows: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for r in rows:
        cik = _pad_cik(r["cik"])
        cand = {**r, "cik": cik, "ticker": str(r["ticker"]).upper()}
        prev = best.get(cik)
        if prev is None or _ticker_rank(cand["ticker"]) < _ticker_rank(prev["ticker"]):
            best[cik] = cand
    return list(best.values())


def _set_identity() -> None:
    import edgar

    edgar.set_identity(config.get_settings().sec_user_agent)


def run() -> None:
    """Stage 1: pull SEC company_tickers and enrich SIC from EDGAR submissions."""
    import edgar

    s = config.get_settings()
    _set_identity()

    tickers_df = edgar.get_company_tickers()
    rows = _dedupe_by_cik(tickers_df.to_dict("records"))

    with db.connect(s.db_path) as conn:
        db.init_schema(conn)

        # Load existing SICs so we can skip re-fetching enrichment on rerun.
        existing_sic = {
            r["cik"]: r["sic"]
            for r in conn.execute("SELECT cik, sic FROM companies").fetchall()
        }

        with db.transaction(conn):
            for r in rows:
                db.upsert_company(
                    conn,
                    cik=r["cik"],
                    ticker=r["ticker"],
                    name=r.get("company"),
                    exchange=r.get("exchange"),
                )

        total = len(rows)
        print(f"universe: upserted {total} companies; enriching SIC...")

        enriched = 0
        with db.transaction(conn):
            for i, r in enumerate(rows, 1):
                cik = r["cik"]
                if existing_sic.get(cik):
                    continue
                try:
                    company = edgar.Company(int(cik))
                    sic = company.sic
                except Exception as exc:  # noqa: BLE001
                    print(f"universe: SIC fetch failed for cik={cik}: {exc}")
                    continue
                if sic:
                    db.upsert_company(conn, cik=cik, ticker=r["ticker"], sic=str(sic))
                    enriched += 1
                if i % 250 == 0:
                    print(f"universe: enriched {enriched}/{i}")

        print(f"universe: done. companies={total}, newly enriched SIC={enriched}")


def filter_candidates() -> None:
    """Stage 2: mark is_candidate=1 for SIC-included rows plus all anchor tickers."""
    s = config.get_settings()
    anchors = _load_anchor_tickers(s.definitions_path)

    with db.connect(s.db_path) as conn:
        db.init_schema(conn)
        with db.transaction(conn):
            # Reset then re-apply so re-runs reflect current rules.
            conn.execute("UPDATE companies SET is_candidate = 0")

            for row in conn.execute("SELECT cik, ticker, sic FROM companies").fetchall():
                include = _in_include(row["sic"]) or (row["ticker"] or "").upper() in anchors
                if include:
                    conn.execute(
                        "UPDATE companies SET is_candidate = 1 WHERE cik = ?", (row["cik"],)
                    )

        n = conn.execute("SELECT COUNT(*) FROM companies WHERE is_candidate = 1").fetchone()[0]
        print(f"filter: {n} candidates marked (anchors={len(anchors)})")
