"""Tests for the optional LLM segment-extraction flow.

All Anthropic SDK calls are mocked via a client_factory seam. No network.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from minidex import config, db, llm_segments


# ---- fake Anthropic client -------------------------------------------------


class _MsgContent:
    def __init__(self, text: str):
        self.text = text


class _Message:
    def __init__(self, text: str, model: str = "claude-haiku-4-5"):
        self.content = [_MsgContent(text)]
        self.model = model


class _Result:
    def __init__(self, type: str = "succeeded", message: _Message | None = None):
        self.type = type
        self.message = message


class _Individual:
    def __init__(self, custom_id: str, result: _Result):
        self.custom_id = custom_id
        self.result = result


class _Batch:
    def __init__(self, id: str = "b_test", processing_status: str = "ended"):
        self.id = id
        self.processing_status = processing_status


class _Batches:
    def __init__(self):
        self.created = []
        self.results_map: dict[str, list[_Individual]] = {}
        self.retrieve_status: str = "ended"

    def create(self, *, requests):
        self.created.append(requests)
        return _Batch(id="b_test")

    def retrieve(self, batch_id: str):
        return _Batch(id=batch_id, processing_status=self.retrieve_status)

    def results(self, batch_id: str):
        return iter(self.results_map.get(batch_id, []))


class _Messages:
    def __init__(self):
        self.batches = _Batches()


class _FakeClient:
    def __init__(self):
        self.messages = _Messages()


# ---- fixtures --------------------------------------------------------------


@pytest.fixture()
def scored_settings(tmp_path: Path) -> config.Settings:
    real = config.get_settings()
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    for d in (data_dir, raw_dir, tmp_path / "outputs"):
        d.mkdir(parents=True, exist_ok=True)
    return config.Settings(
        anthropic_api_key=real.anthropic_api_key,
        sec_user_agent=real.sec_user_agent,
        repo_root=real.repo_root,
        data_dir=data_dir,
        raw_dir=raw_dir,
        outputs_dir=tmp_path / "outputs",
        db_path=data_dir / "test.db",
        definitions_path=real.definitions_path,
        prompt_path=real.prompt_path,
        embedding_model=real.embedding_model,
        similarity_threshold=real.similarity_threshold,
        score_floor=real.score_floor,
        batch_model=real.batch_model,
        prompt_version=real.prompt_version,
        max_item1_chars=real.max_item1_chars,
    )


def _seed(settings: config.Settings) -> None:
    with db.connect(settings.db_path) as conn:
        db.init_schema(conn)
        # AMD — single-segment fallback (needs refinement)
        db.upsert_company(conn, cik="0000002488", ticker="AMD", name="AMD Inc", sic="3674", is_candidate=1)
        (settings.raw_dir / "amd.txt").write_text(
            "AMD is a fabless semiconductor company. Data Center revenue was $12.6 billion in FY2025. "
            "Client revenue was $7.1 billion. Gaming revenue was $2.6 billion. Embedded revenue was $3.6 billion.",
            encoding="utf-8",
        )
        db.upsert_filing(
            conn, cik="0000002488", accession="amd-1", fy=2025, filed_date="2026-02-04",
            item1_path=str(settings.raw_dir / "amd.txt"), item1_chars=1000,
            segments_json='[{"segment":"Total revenue","revenue":34639000000.0,"period":"2025-12-27"}]',
        )
        # NVDA — real multi-segment (should NOT be a candidate)
        db.upsert_company(conn, cik="0001045810", ticker="NVDA", name="NVIDIA", sic="3674", is_candidate=1)
        db.upsert_filing(
            conn, cik="0001045810", accession="nvda-1", fy=2026, filed_date="2026-03-01",
            item1_path=str(settings.raw_dir / "nvda.txt"), item1_chars=1000,
            segments_json='[{"segment":"Compute & Networking","revenue":193000000000.0,"period":"2026-01-25"},{"segment":"Graphics","revenue":22000000000.0,"period":"2026-01-25"}]',
        )
        # OKLO — no segments at all (needs refinement)
        db.upsert_company(conn, cik="0001849380", ticker="OKLO", name="Oklo Inc", sic="4911", is_candidate=1)
        (settings.raw_dir / "oklo.txt").write_text(
            "Oklo is developing small modular nuclear reactors. We are pre-revenue.",
            encoding="utf-8",
        )
        db.upsert_filing(
            conn, cik="0001849380", accession="oklo-1", fy=2024, filed_date="2025-03-01",
            item1_path=str(settings.raw_dir / "oklo.txt"), item1_chars=100,
            segments_json="[]",
        )
        conn.commit()


# ---- tests -----------------------------------------------------------------


def test_candidates_picks_only_empty_or_total_only(scored_settings):
    _seed(scored_settings)
    with db.connect(scored_settings.db_path) as conn:
        cands = llm_segments._candidates(conn)
    tickers = sorted(c["ticker"] for c in cands)
    assert tickers == ["AMD", "OKLO"]  # NVDA is excluded


def test_format_total_revenue_line():
    js = '[{"segment":"Total revenue","revenue":100.0,"period":"2025-12-31"}]'
    line = llm_segments._format_total_revenue_line(js)
    assert "Total revenue" in line and "$100" in line and "2025-12-31" in line
    assert llm_segments._format_total_revenue_line("[]") == "NOT AVAILABLE"
    assert llm_segments._format_total_revenue_line(None) == "NOT AVAILABLE"


def test_submit_builds_one_request_per_candidate(scored_settings):
    _seed(scored_settings)
    fake = _FakeClient()
    llm_segments.submit(assume_yes=True, client_factory=lambda: fake, settings=scored_settings)

    assert len(fake.messages.batches.created) == 1
    requests = fake.messages.batches.created[0]
    ids = {r["custom_id"] for r in requests}
    assert ids == {"seg_AMD_2025", "seg_OKLO_2024"}
    # AMD's user prompt includes the total revenue anchor
    amd_req = next(r for r in requests if r["custom_id"] == "seg_AMD_2025")
    assert "Total revenue" in amd_req["params"]["messages"][0]["content"]
    assert amd_req["params"]["temperature"] == 0

    # batch row persisted with kind='llm_segments'
    with db.connect(scored_settings.db_path) as conn:
        row = conn.execute("SELECT batch_id, status, meta_json FROM batches").fetchone()
    assert row["status"] == "in_progress"
    assert '"kind":"llm_segments"' in row["meta_json"]


def test_submit_no_op_when_no_candidates(scored_settings):
    # Prime DB with only fully-segmented filings.
    with db.connect(scored_settings.db_path) as conn:
        db.init_schema(conn)
        db.upsert_company(conn, cik="0001", ticker="X", name="X", sic="3674", is_candidate=1)
        db.upsert_filing(
            conn, cik="0001", accession="x-1", fy=2024, filed_date="2025-01-01",
            item1_path="x.txt", item1_chars=100,
            segments_json='[{"segment":"A","revenue":1.0,"period":"2024"}, {"segment":"B","revenue":2.0,"period":"2024"}]',
        )
        conn.commit()
    fake = _FakeClient()
    llm_segments.submit(assume_yes=True, client_factory=lambda: fake, settings=scored_settings)
    assert fake.messages.batches.created == []


def test_poll_writes_segments_back_and_marks_batch_ended(scored_settings):
    _seed(scored_settings)
    fake = _FakeClient()
    # Pre-populate a batch row that poll can find.
    with db.connect(scored_settings.db_path) as conn:
        db.upsert_batch(
            conn, batch_id="b_test", submitted_at="2026-07-24T00:00:00Z",
            status="in_progress",
            meta_json='{"kind":"llm_segments","custom_ids":["seg_AMD_2025","seg_OKLO_2024"]}',
        )
        conn.commit()

    good_json = json.dumps({
        "ticker": "AMD", "fy": 2025,
        "segments": [
            {"segment": "Data Center", "revenue_usd": 12600000000.0, "period": "2025-12-27"},
            {"segment": "Client", "revenue_usd": 7100000000.0, "period": "2025-12-27"},
        ],
    })
    # OKLO correctly returns []
    empty_json = json.dumps({"ticker": "OKLO", "fy": 2024, "segments": []})

    fake.messages.batches.results_map["b_test"] = [
        _Individual("seg_AMD_2025", _Result("succeeded", _Message(good_json))),
        _Individual("seg_OKLO_2024", _Result("succeeded", _Message(empty_json))),
    ]

    llm_segments.poll(client_factory=lambda: fake, settings=scored_settings)

    with db.connect(scored_settings.db_path) as conn:
        amd_row = conn.execute(
            "SELECT segments_json FROM filings WHERE cik='0000002488'"
        ).fetchone()
        oklo_row = conn.execute(
            "SELECT segments_json FROM filings WHERE cik='0001849380'"
        ).fetchone()
        batch_row = conn.execute(
            "SELECT status FROM batches WHERE batch_id='b_test'"
        ).fetchone()

    amd_segs = json.loads(amd_row["segments_json"])
    assert len(amd_segs) == 2
    assert {s["segment"] for s in amd_segs} == {"Data Center", "Client"}
    assert amd_segs[0]["revenue"] == 12600000000.0

    # OKLO returned empty → left unchanged (still [])
    assert oklo_row["segments_json"] == "[]"

    assert batch_row["status"] == "ended"


def test_poll_handles_fenced_json():
    fake = _FakeClient()
    fenced = "```json\n" + json.dumps({
        "ticker": "AMD", "fy": 2025,
        "segments": [{"segment": "X", "revenue_usd": 1.0, "period": "2025"}],
    }) + "\n```"
    parsed = llm_segments.LLMSegmentsResponse.model_validate_json(
        llm_segments._FENCE_RE.match(fenced).group(1).strip()
    )
    assert parsed.segments[0].segment == "X"


def test_poll_skips_in_progress_batch(scored_settings):
    _seed(scored_settings)
    fake = _FakeClient()
    fake.messages.batches.retrieve_status = "in_progress"
    with db.connect(scored_settings.db_path) as conn:
        db.upsert_batch(
            conn, batch_id="b_ip", submitted_at="2026-07-24T00:00:00Z",
            status="in_progress", meta_json='{"kind":"llm_segments","custom_ids":[]}',
        )
        conn.commit()
    llm_segments.poll(client_factory=lambda: fake, settings=scored_settings)
    with db.connect(scored_settings.db_path) as conn:
        row = conn.execute("SELECT status FROM batches WHERE batch_id='b_ip'").fetchone()
    assert row["status"] == "in_progress"  # unchanged


def test_poll_ignores_batches_of_other_kinds(scored_settings):
    _seed(scored_settings)
    fake = _FakeClient()
    with db.connect(scored_settings.db_path) as conn:
        db.upsert_batch(
            conn, batch_id="b_other", submitted_at="2026-07-24T00:00:00Z",
            status="in_progress", meta_json='{"kind":"score","custom_ids":[]}',
        )
        conn.commit()
    llm_segments.poll(client_factory=lambda: fake, settings=scored_settings)
    # Nothing raised, and the other batch is untouched.
    with db.connect(scored_settings.db_path) as conn:
        row = conn.execute("SELECT status FROM batches WHERE batch_id='b_other'").fetchone()
    assert row["status"] == "in_progress"
