from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

from minidex import config, db, universe


@pytest.fixture()
def tmp_db(tmp_path: Path):
    p = tmp_path / "minidex.db"
    real = config.get_settings()
    stub = SimpleNamespace(
        sec_user_agent=real.sec_user_agent,
        db_path=p,
        definitions_path=real.definitions_path,
    )
    original = universe.config.get_settings
    universe.config.get_settings = lambda: stub  # type: ignore[assignment]
    try:
        yield p
    finally:
        universe.config.get_settings = original  # type: ignore[assignment]


# ---------- SIC inclusion ----------

def test_sic_include_semiconductors():
    assert universe._in_include("3674")  # NVDA / semiconductors
    assert universe._in_include("3670")
    assert universe._in_include("3679")


def test_sic_include_ranges():
    assert universe._in_include("3572")  # computers/storage
    assert universe._in_include("7372")  # software
    assert universe._in_include("7370")
    assert universe._in_include("3663")  # comms equipment
    assert universe._in_include("3621")  # electrical industrial apparatus
    assert universe._in_include("3511")  # engines/turbines
    assert universe._in_include("3825")  # measuring instruments
    assert universe._in_include("3690")  # misc electrical
    assert universe._in_include("4813")  # telecom


def test_sic_include_scalars():
    assert universe._in_include("6798")  # REITs
    assert universe._in_include("4911")  # electric services
    assert universe._in_include("4991")  # IPPs


def test_sic_exclude_out_of_range():
    assert not universe._in_include("6020")  # JPM commercial banks
    assert not universe._in_include("2834")  # PFE pharma
    assert not universe._in_include("5812")  # eating places
    assert not universe._in_include(None)
    assert not universe._in_include("")
    assert not universe._in_include("abc")


# ---------- Anchor loading ----------

def test_load_anchor_tickers_from_real_yaml():
    s = config.get_settings()
    anchors = universe._load_anchor_tickers(s.definitions_path)
    # A handful of anchors that must appear per definitions file.
    for t in ["NVDA", "MSFT", "EQIX", "VRT", "CLS", "AMZN", "GOOGL", "TSM"]:
        assert t in anchors, f"expected {t} in anchors"
    assert len(anchors) >= 40


def test_load_anchor_tickers_ignores_missing_field(tmp_path: Path):
    p = tmp_path / "defs.yaml"
    p.write_text(
        "buckets:\n"
        "  - id: a\n"
        "    name: A\n"
        "    anchors: [FOO, bar]\n"
        "  - id: b\n"
        "    name: B\n",
        encoding="utf-8",
    )
    got = universe._load_anchor_tickers(p)
    assert got == {"FOO", "BAR"}


# ---------- filter_candidates ----------

def _seed(conn, rows):
    for r in rows:
        db.upsert_company(conn, **r)
    conn.commit()


def test_filter_marks_sic_included_rows(tmp_db):
    conn = db.connect(tmp_db)
    db.init_schema(conn)
    _seed(conn, [
        {"cik": "0001045810", "ticker": "NVDA", "name": "NVIDIA", "sic": "3674"},
        {"cik": "0000789019", "ticker": "MSFT", "name": "Microsoft", "sic": "7372"},
        {"cik": "0001101239", "ticker": "EQIX", "name": "Equinix", "sic": "6798"},
        {"cik": "0001674101", "ticker": "VRT", "name": "Vertiv", "sic": "3620"},
        {"cik": "0000861884", "ticker": "CLS", "name": "Celestica", "sic": "3672"},
        {"cik": "0000019617", "ticker": "JPM", "name": "JPMorgan", "sic": "6020"},
        {"cik": "0000078003", "ticker": "PFE", "name": "Pfizer", "sic": "2834"},
    ])
    conn.close()

    universe.filter_candidates()

    conn = db.connect(tmp_db)
    got = {
        r["ticker"]: r["is_candidate"]
        for r in conn.execute("SELECT ticker, is_candidate FROM companies").fetchall()
    }
    assert got["NVDA"] == 1
    assert got["MSFT"] == 1
    assert got["EQIX"] == 1
    assert got["VRT"] == 1
    assert got["CLS"] == 1
    assert got["JPM"] == 0
    assert got["PFE"] == 0
    conn.close()


def test_filter_force_includes_anchor_tickers_regardless_of_sic(tmp_db):
    """A ticker in the YAML anchors set must be a candidate even if its SIC is
    outside the inclusion list."""
    conn = db.connect(tmp_db)
    db.init_schema(conn)
    _seed(conn, [
        # AMZN is an anchor (hyperscalers). Give it a nonsense SIC.
        {"cik": "0001018724", "ticker": "AMZN", "name": "Amazon", "sic": "5961"},
        # A non-anchor row with an out-of-list SIC — must stay 0.
        {"cik": "0000019617", "ticker": "JPM", "name": "JPMorgan", "sic": "6020"},
    ])
    conn.close()

    universe.filter_candidates()

    conn = db.connect(tmp_db)
    got = {
        r["ticker"]: r["is_candidate"]
        for r in conn.execute("SELECT ticker, is_candidate FROM companies").fetchall()
    }
    assert got["AMZN"] == 1
    assert got["JPM"] == 0
    conn.close()


def test_filter_is_idempotent_and_resets_stale_candidates(tmp_db):
    conn = db.connect(tmp_db)
    db.init_schema(conn)
    _seed(conn, [
        {"cik": "0000019617", "ticker": "JPM", "name": "JPMorgan",
         "sic": "6020", "is_candidate": 1},
        {"cik": "0001045810", "ticker": "NVDA", "name": "NVIDIA", "sic": "3674"},
    ])
    conn.close()

    universe.filter_candidates()
    universe.filter_candidates()  # second call must not double-apply / err

    conn = db.connect(tmp_db)
    got = {
        r["ticker"]: r["is_candidate"]
        for r in conn.execute("SELECT ticker, is_candidate FROM companies").fetchall()
    }
    assert got["JPM"] == 0  # reset from stale 1
    assert got["NVDA"] == 1
    conn.close()


# ---------- run() ----------

def test_run_upserts_tickers_and_enriches_sic(tmp_db):
    """Stage 1 mocked end-to-end: tickers dataframe + edgar.Company for SIC."""
    tickers_df = pd.DataFrame(
        [
            {"cik": 1045810, "ticker": "NVDA", "exchange": "Nasdaq", "company": "NVIDIA CORP"},
            {"cik": 789019, "ticker": "MSFT", "exchange": "Nasdaq", "company": "MICROSOFT CORP"},
        ]
    )

    sic_by_cik = {1045810: "3674", 789019: "7372"}

    def _fake_company(cik):
        m = MagicMock()
        m.sic = sic_by_cik.get(int(cik))
        return m

    fake_edgar = MagicMock()
    fake_edgar.get_company_tickers.return_value = tickers_df
    fake_edgar.Company.side_effect = _fake_company
    fake_edgar.set_identity = MagicMock()

    with patch.dict("sys.modules", {"edgar": fake_edgar}):
        universe.run()

    fake_edgar.set_identity.assert_called()

    conn = db.connect(tmp_db)
    got = {
        r["ticker"]: dict(r)
        for r in conn.execute("SELECT ticker, cik, name, sic, exchange FROM companies").fetchall()
    }
    assert set(got) == {"NVDA", "MSFT"}
    assert got["NVDA"]["sic"] == "3674"
    assert got["MSFT"]["sic"] == "7372"
    assert got["NVDA"]["cik"] == "0001045810"
    assert got["NVDA"]["exchange"] == "Nasdaq"
    conn.close()


def test_run_skips_sic_enrichment_when_already_populated(tmp_db):
    """Re-runs should not refetch SIC for companies that already have one."""
    conn = db.connect(tmp_db)
    db.init_schema(conn)
    db.upsert_company(
        conn, cik="0001045810", ticker="NVDA", name="NVIDIA CORP",
        sic="3674", exchange="Nasdaq",
    )
    conn.commit()
    conn.close()

    tickers_df = pd.DataFrame(
        [{"cik": 1045810, "ticker": "NVDA", "exchange": "Nasdaq", "company": "NVIDIA CORP"}]
    )

    fake_edgar = MagicMock()
    fake_edgar.get_company_tickers.return_value = tickers_df
    fake_edgar.Company = MagicMock(side_effect=AssertionError("should not be called"))
    fake_edgar.set_identity = MagicMock()

    with patch.dict("sys.modules", {"edgar": fake_edgar}):
        universe.run()

    fake_edgar.Company.assert_not_called()
