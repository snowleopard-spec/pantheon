from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from minidex import config, db, edgar


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def settings_tmp(tmp_path: Path, monkeypatch):
    """Redirect settings.raw_dir and settings.db_path into a tmp tree."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    db_path = tmp_path / "minidex.db"

    real_settings = config.get_settings()
    fake = SimpleNamespace(
        sec_user_agent=real_settings.sec_user_agent,
        raw_dir=raw_dir,
        db_path=db_path,
    )

    # Provide a stand-in that still exposes cache_clear so the autouse
    # conftest teardown does not blow up.
    def _fake_get_settings():
        return fake

    _fake_get_settings.cache_clear = lambda: None  # type: ignore[attr-defined]
    monkeypatch.setattr(config, "get_settings", _fake_get_settings)
    yield fake


@pytest.fixture()
def conn(settings_tmp):
    c = db.connect(settings_tmp.db_path)
    db.init_schema(c)
    yield c
    c.close()


def _seed_candidate(
    conn, cik: str, ticker: str, is_candidate: int = 1, market_cap: float | None = None
) -> None:
    kwargs = dict(
        cik=cik, ticker=ticker, name=f"{ticker} Inc", sic="7372",
        is_candidate=is_candidate,
    )
    if market_cap is not None:
        kwargs["market_cap"] = market_cap
        kwargs["market_cap_asof"] = "2025-01-01"
    db.upsert_company(conn, **kwargs)
    conn.commit()


def _make_filing(
    *,
    accession: str = "0000000000-00-000000",
    filing_date: str = "2025-02-14",
    period_of_report: str = "2024-12-31",
    business_text: str | None = "This is Item 1 business text " * 200,
    raw_text: str = "RAW BODY " * 10_000,
    xbrl=None,
) -> MagicMock:
    filing = MagicMock(name=f"Filing({accession})")
    filing.accession_number = accession
    filing.filing_date = filing_date
    filing.period_of_report = period_of_report
    filing.text.return_value = raw_text
    filing.markdown.return_value = ""

    if business_text is not None:
        obj = MagicMock(name="TenK")
        obj.business = business_text
        filing.obj.return_value = obj
    else:
        obj = MagicMock(name="TenK")
        # Explicitly set attrs to None so getattr returns None
        obj.business = None
        obj.item_1 = None
        obj.item1 = None
        filing.obj.return_value = obj

    if xbrl is None:
        filing.xbrl.return_value = None
    else:
        filing.xbrl.return_value = xbrl
    return filing


def _install_edgar(monkeypatch, *, company_by_cik):
    """Stub the edgartools surface used by edgar.py."""
    calls = {"set_identity": 0, "companies": 0}

    def fake_set_identity(ua: str) -> None:
        calls["set_identity"] += 1

    def fake_get_company(cik: str):
        calls["companies"] += 1
        return company_by_cik[cik]

    monkeypatch.setattr(edgar, "_ensure_identity", fake_set_identity)
    monkeypatch.setattr(edgar, "_get_company", fake_get_company)
    return calls


def _company_with_filings(filings: list, facts=None) -> MagicMock:
    company = MagicMock(name="Company")
    filings_obj = MagicMock(name="Filings")
    filings_obj.latest.return_value = filings[0] if filings else None
    filings_obj.__iter__ = lambda self: iter(filings)
    company.get_filings.return_value = filings_obj
    company.get_facts.return_value = facts
    return company


# ---------------------------------------------------------------------------
# Unit tests for helpers
# ---------------------------------------------------------------------------
def test_extract_item1_prefers_business_text():
    filing = _make_filing(business_text="Real Item 1 text about the business.")
    text, fallback = edgar._extract_item1(filing)
    assert fallback is False
    assert "Real Item 1" in text


def test_extract_item1_falls_back_to_raw_body_when_business_empty():
    filing = _make_filing(business_text=None, raw_text="X" * 100_000)
    text, fallback = edgar._extract_item1(filing)
    assert fallback is True
    assert len(text) == edgar.FALLBACK_CHARS


def test_extract_item1_falls_back_when_obj_raises():
    filing = _make_filing(business_text="ignored")
    filing.obj.side_effect = RuntimeError("no obj")
    text, fallback = edgar._extract_item1(filing)
    assert fallback is True
    assert text  # raw body fallback used


def test_extract_segments_returns_empty_on_no_xbrl():
    filing = _make_filing(xbrl=None)
    assert edgar._extract_segments(filing) == []


def _make_xbrl_df(rows: list[dict]):
    """Build a mock xbrl object whose .query(...).to_dataframe() returns a DataFrame."""
    import pandas as pd
    df = pd.DataFrame(rows)
    query = MagicMock()
    query.to_dataframe.return_value = df
    xbrl = MagicMock()
    xbrl.query.return_value = query
    return xbrl


def test_extract_segments_returns_newest_period_per_segment():
    rows = [
        {
            "concept": "us-gaap:Revenues", "numeric_value": 100.0,
            "period_end": "2024-12-31", "dimension_label": "DataCenter",
            "dim_us-gaap_StatementBusinessSegmentsAxis": "co:DCSegmentMember",
        },
        {
            "concept": "us-gaap:Revenues", "numeric_value": 50.0,
            "period_end": "2024-12-31", "dimension_label": "Gaming",
            "dim_us-gaap_StatementBusinessSegmentsAxis": "co:GamingSegmentMember",
        },
        {  # older period — should be filtered out
            "concept": "us-gaap:Revenues", "numeric_value": 80.0,
            "period_end": "2023-12-31", "dimension_label": "DataCenter",
            "dim_us-gaap_StatementBusinessSegmentsAxis": "co:DCSegmentMember",
        },
    ]
    filing = _make_filing(xbrl=_make_xbrl_df(rows))
    segs = edgar._extract_segments(filing)
    assert len(segs) == 2
    labels = {s["segment"] for s in segs}
    assert labels == {"DataCenter", "Gaming"}
    dc = next(s for s in segs if s["segment"] == "DataCenter")
    assert dc["revenue"] == 100.0
    assert dc["period"] == "2024-12-31"
    # JSON round-trip
    import json as _json
    _json.dumps(segs)


def test_extract_segments_ignores_undimensioned_revenue():
    rows = [
        {
            "concept": "us-gaap:Revenues", "numeric_value": 200.0,
            "period_end": "2024-12-31", "dimension_label": None,
            "dim_us-gaap_StatementBusinessSegmentsAxis": None,
        }
    ]
    filing = _make_filing(xbrl=_make_xbrl_df(rows))
    assert edgar._extract_segments(filing) == []


def test_extract_segments_swallows_exceptions():
    xbrl = MagicMock()
    xbrl.query.side_effect = RuntimeError("boom")
    filing = _make_filing(xbrl=xbrl)
    assert edgar._extract_segments(filing) == []


def test_extract_segments_falls_back_to_total_revenue_for_single_segment():
    # XBRL has no segment axis → fallback triggers when company provided.
    filing = _make_filing(xbrl=_make_xbrl_df([]))
    facts = MagicMock()
    facts.get_concept.return_value = {
        "value": 9_005_700_000.0, "period_end": "2025-12-31",
        "tag_used": "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
    }
    company = MagicMock()
    company.get_facts.return_value = facts
    segs = edgar._extract_segments(filing, company=company)
    assert len(segs) == 1
    assert segs[0]["segment"] == "Total revenue"
    assert segs[0]["revenue"] == 9_005_700_000.0
    assert segs[0]["period"] == "2025-12-31"


def test_extract_segments_fallback_rejects_absurd_values():
    # Foreign-filer unit / currency errors sometimes surface trillion+ values.
    filing = _make_filing(xbrl=_make_xbrl_df([]))
    facts = MagicMock()
    facts.get_concept.return_value = {
        "value": 2_894_307_700_000.0, "period_end": "2024-12-31",
    }
    company = MagicMock()
    company.get_facts.return_value = facts
    assert edgar._extract_segments(filing, company=company) == []


def test_extract_segments_fallback_none_when_no_company():
    filing = _make_filing(xbrl=_make_xbrl_df([]))
    assert edgar._extract_segments(filing) == []


def test_extract_market_cap_shares_times_yf_price(monkeypatch):
    facts = MagicMock()
    facts.get_concept.return_value = {
        "value": 2_000_000.0,
        "period_end": "2025-02-15",
    }
    company = MagicMock()
    company.get_facts.return_value = facts

    monkeypatch.setattr(edgar, "_latest_close_yf", lambda t: (50.0, "2025-03-01"))
    mc = edgar._extract_market_cap(company, "AAA")
    assert mc == (2_000_000.0 * 50.0, "2025-03-01")


def test_extract_market_cap_returns_none_when_shares_missing(monkeypatch):
    facts = MagicMock()
    facts.get_concept.return_value = None
    company = MagicMock()
    company.get_facts.return_value = facts
    monkeypatch.setattr(edgar, "_latest_close_yf", lambda t: (50.0, "2025-03-01"))
    assert edgar._extract_market_cap(company, "AAA") is None


def test_extract_market_cap_returns_none_when_price_missing(monkeypatch):
    facts = MagicMock()
    facts.get_concept.return_value = {"value": 100.0, "period_end": "2025-01-01"}
    company = MagicMock()
    company.get_facts.return_value = facts
    monkeypatch.setattr(edgar, "_latest_close_yf", lambda t: (None, None))
    assert edgar._extract_market_cap(company, "AAA") is None


def test_extract_market_cap_returns_none_when_facts_error():
    company = MagicMock()
    company.get_facts.side_effect = RuntimeError("no facts")
    assert edgar._extract_market_cap(company, "AAA") is None


def test_latest_annual_filing_falls_back_to_20f():
    company = MagicMock()

    empty = MagicMock()
    empty.latest.return_value = None
    empty.__iter__ = lambda self: iter([])

    twenty_f = MagicMock()
    filing = _make_filing(accession="20F-1")
    twenty_f.latest.return_value = filing
    twenty_f.__iter__ = lambda self: iter([filing])

    def get_filings(form):
        return {"10-K": empty, "20-F": twenty_f}[form]

    company.get_filings.side_effect = get_filings
    assert edgar._latest_annual_filing(company) is filing


# ---------------------------------------------------------------------------
# Integration-ish tests for run()
# ---------------------------------------------------------------------------
def test_run_populates_filings_and_writes_raw_file(settings_tmp, conn, monkeypatch):
    _seed_candidate(conn, "0000001", "AAA")

    filing = _make_filing(
        accession="0001-24-000001",
        business_text="Item 1 Business " * 500,
    )
    facts = MagicMock()
    facts.get_concept.return_value = {"value": 30_000_000.0, "period_end": "2025-01-15"}

    company = _company_with_filings([filing], facts=facts)
    _install_edgar(monkeypatch, company_by_cik={"0000001": company})
    monkeypatch.setattr(edgar, "_latest_close_yf", lambda t: (50.0, "2025-02-01"))

    edgar.run()

    # Re-open connection (run() closes/commits its own conn)
    c2 = db.connect(settings_tmp.db_path)
    row = c2.execute("SELECT * FROM filings WHERE cik='0000001'").fetchone()
    assert row is not None
    assert row["accession"] == "0001-24-000001"
    assert row["item1_chars"] > 2_000
    assert row["fy"] == 2024
    assert row["filed_date"] == "2025-02-14"

    raw_file = Path(row["item1_path"])
    assert raw_file.exists()
    assert raw_file.parent == settings_tmp.raw_dir
    assert "_fallback" not in raw_file.name
    assert raw_file.read_text().startswith("Item 1 Business")

    # market cap populated: 30M shares × $50 = $1.5B
    comp = c2.execute("SELECT market_cap, market_cap_asof FROM companies WHERE cik='0000001'").fetchone()
    assert comp["market_cap"] == 1_500_000_000.0
    assert comp["market_cap_asof"] == "2025-02-01"
    c2.close()


def test_run_uses_fallback_suffix_when_item1_missing(settings_tmp, conn, monkeypatch):
    _seed_candidate(conn, "0000002", "BBB")

    filing = _make_filing(
        accession="0002-24-000001",
        business_text=None,
        raw_text="FALLBACK " * 20_000,
    )
    company = _company_with_filings([filing])
    _install_edgar(monkeypatch, company_by_cik={"0000002": company})

    edgar.run()

    c2 = db.connect(settings_tmp.db_path)
    row = c2.execute("SELECT * FROM filings WHERE cik='0000002'").fetchone()
    assert row is not None
    raw_file = Path(row["item1_path"])
    assert raw_file.exists()
    assert edgar.FALLBACK_SUFFIX in raw_file.name
    assert row["item1_chars"] == edgar.FALLBACK_CHARS
    c2.close()


def test_run_segments_json_is_valid_json(settings_tmp, conn, monkeypatch):
    _seed_candidate(conn, "0000003", "CCC")

    seg_rows = [{
        "concept": "us-gaap:Revenues", "numeric_value": 250.0,
        "period_end": "2024-12-31", "dimension_label": "Cloud",
        "dim_us-gaap_StatementBusinessSegmentsAxis": "co:CloudMember",
    }]
    filing = _make_filing(accession="0003-24-1", xbrl=_make_xbrl_df(seg_rows))
    company = _company_with_filings([filing])
    _install_edgar(monkeypatch, company_by_cik={"0000003": company})

    edgar.run()

    c2 = db.connect(settings_tmp.db_path)
    row = c2.execute("SELECT segments_json FROM filings WHERE cik='0000003'").fetchone()
    assert row is not None
    parsed = json.loads(row["segments_json"])
    assert isinstance(parsed, list)
    assert parsed[0]["segment"] == "Cloud"
    assert parsed[0]["revenue"] == 250.0
    c2.close()


def test_run_is_idempotent_and_makes_zero_network_calls_on_rerun(
    settings_tmp, conn, monkeypatch
):
    _seed_candidate(conn, "0000004", "DDD", market_cap=1.23e9)

    seg_rows = [{
        "concept": "us-gaap:Revenues", "numeric_value": 42.0,
        "period_end": "2024-12-31", "dimension_label": "OneSegment",
        "dim_us-gaap_StatementBusinessSegmentsAxis": "co:OneMember",
    }]
    filing = _make_filing(accession="0004-24-1", xbrl=_make_xbrl_df(seg_rows))
    facts = MagicMock()
    facts.get_concept.return_value = {"value": 1.0, "period_end": "2025-01-01"}
    company = _company_with_filings([filing], facts=facts)
    calls = _install_edgar(monkeypatch, company_by_cik={"0000004": company})
    monkeypatch.setattr(edgar, "_latest_close_yf", lambda t: (1.0, "2025-01-01"))

    edgar.run()
    assert calls["companies"] == 1
    assert calls["set_identity"] == 1

    # Second run: filing cached, market_cap set, segments non-empty → no Company lookup.
    edgar.run()
    assert calls["companies"] == 1, "second run should not fetch anything"
    assert calls["set_identity"] == 2  # identity set every run, cheap and local


def test_run_skips_non_candidates(settings_tmp, conn, monkeypatch):
    _seed_candidate(conn, "0000005", "EEE", is_candidate=0)

    calls = _install_edgar(monkeypatch, company_by_cik={})
    edgar.run()
    assert calls["companies"] == 0

    c2 = db.connect(settings_tmp.db_path)
    n = c2.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    assert n == 0
    c2.close()


def test_run_handles_company_with_no_annual_filing(settings_tmp, conn, monkeypatch):
    _seed_candidate(conn, "0000006", "FFF")

    company = MagicMock()
    empty = MagicMock()
    empty.latest.return_value = None
    empty.__iter__ = lambda self: iter([])
    company.get_filings.return_value = empty

    _install_edgar(monkeypatch, company_by_cik={"0000006": company})
    edgar.run()  # should not raise

    c2 = db.connect(settings_tmp.db_path)
    n = c2.execute("SELECT COUNT(*) FROM filings WHERE cik='0000006'").fetchone()[0]
    assert n == 0
    c2.close()
