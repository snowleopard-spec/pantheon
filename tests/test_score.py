from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import pytest

from minidex import config, db, score


# ---------- fixtures --------------------------------------------------------


@pytest.fixture()
def scored_settings(tmp_path: Path, monkeypatch) -> config.Settings:
    """A Settings instance with tmp_path-backed data_dir/db_path."""
    real = config.get_settings()
    tmp_data = tmp_path / "data"
    tmp_data.mkdir()
    s = config.Settings(
        anthropic_api_key=real.anthropic_api_key,
        sec_user_agent=real.sec_user_agent,
        repo_root=real.repo_root,
        data_dir=tmp_data,
        raw_dir=tmp_data / "raw",
        outputs_dir=tmp_path / "outputs",
        db_path=tmp_data / "minidex.db",
        definitions_path=real.definitions_path,
        prompt_path=real.prompt_path,
        embedding_model=real.embedding_model,
        similarity_threshold=real.similarity_threshold,
        score_floor=real.score_floor,
        batch_model=real.batch_model,
        prompt_version=real.prompt_version,
        max_item1_chars=real.max_item1_chars,
    )
    return s


def _seed_db(s: config.Settings, tickers: list[tuple[str, str, str, int, list[str]]]) -> None:
    """tickers: list of (cik, ticker, name, fy, bucket_ids)."""
    item1_dir = s.data_dir / "raw"
    item1_dir.mkdir(exist_ok=True)
    conn = db.connect(s.db_path)
    try:
        db.init_schema(conn)
        for cik, ticker, name, fy, buckets in tickers:
            item1_path = item1_dir / f"{cik}_item1.txt"
            item1_path.write_text(f"Item 1 for {ticker}. " * 50, encoding="utf-8")
            db.upsert_company(
                conn, cik=cik, ticker=ticker, name=name, sic="3674",
                is_candidate=1, market_cap=1e9, market_cap_asof="2025-01-01",
            )
            db.upsert_filing(
                conn, cik=cik, accession=f"acc_{cik}", fy=fy,
                filed_date="2025-02-01",
                item1_path=str(item1_path),
                item1_chars=1000,
                segments_json=json.dumps(
                    [{"segment": "core", "revenue": 1000, "period": "FY"}]
                ),
            )
            for bid in buckets:
                db.upsert_shortlist(
                    conn, cik=cik, bucket_id=bid, similarity=0.72, source="embed"
                )
        conn.commit()
    finally:
        conn.close()


# ---------- fake Anthropic client ------------------------------------------


class _Msg:
    def __init__(self, text: str, model: str = "claude-haiku-4-5", stop_reason: str = "end_turn"):
        self.content = [type("B", (), {"type": "text", "text": text})]
        self.model = model
        self.stop_reason = stop_reason


class _Res:
    def __init__(self, rtype: str, message: Any = None, error: Any = None):
        self.type = rtype
        self.message = message
        self.error = error


class _Individual:
    def __init__(self, custom_id: str, result: _Res):
        self.custom_id = custom_id
        self.result = result


class _Batch:
    def __init__(self, id_: str, processing_status: str = "in_progress"):
        self.id = id_
        self.processing_status = processing_status


class FakeBatches:
    def __init__(self):
        self.created: list[list[dict[str, Any]]] = []
        self.retrieve_calls: list[str] = []
        self.results_map: dict[str, list[_Individual]] = {}
        self.status_map: dict[str, str] = {}
        self._next_id = 0

    def create(self, *, requests):
        # requests may be an iterator or a list of dicts
        chunk = list(requests)
        self.created.append(chunk)
        self._next_id += 1
        bid = f"msgbatch_test_{self._next_id}"
        return _Batch(bid, processing_status="in_progress")

    def retrieve(self, batch_id: str):
        self.retrieve_calls.append(batch_id)
        return _Batch(batch_id, processing_status=self.status_map.get(batch_id, "ended"))

    def results(self, batch_id: str) -> Iterable[_Individual]:
        return iter(self.results_map.get(batch_id, []))


class FakeClient:
    def __init__(self):
        self.messages = type("M", (), {"batches": FakeBatches()})


def _valid_response_json(ticker: str, fy: int, bucket_ids: list[str]) -> str:
    return json.dumps(
        {
            "ticker": ticker,
            "fy": fy,
            "pre_revenue": False,
            "scores": [
                {
                    "bucket_id": b,
                    "score": 0.7,
                    "confidence": "high",
                    "rationale": f"{ticker} derives revenue from {b}.",
                    "evidence_type": "segment_data",
                }
                for b in bucket_ids
            ],
        }
    )


# ---------- prompt-construction tests --------------------------------------


def test_load_prompt_sections_contains_rules_and_template(scored_settings):
    sys_p, user_tpl = score._load_prompt_sections(scored_settings.prompt_path)
    assert "classification engine" in sys_p.lower()
    assert "1. SCORE = REVENUE FRACTION" in sys_p
    assert "{ticker}" in user_tpl
    assert "{item1_text}" in user_tpl
    assert "OUTPUT SCHEMA" in user_tpl


def test_build_user_prompt_fills_placeholders_and_buckets(scored_settings):
    _seed_db(
        scored_settings,
        [("0000010", "NVDA", "NVIDIA Corp", 2024, ["fabless_chip_design", "networking"])],
    )
    conn = db.connect(scored_settings.db_path)
    try:
        cands = score.load_candidates(conn)
    finally:
        conn.close()
    assert len(cands) == 1
    cand = cands[0]
    sys_p, tpl = score._load_prompt_sections(scored_settings.prompt_path)
    bucket_defs = score._load_bucket_defs(scored_settings.definitions_path)

    user = score.build_user_prompt(
        cand,
        user_template=tpl,
        bucket_defs=bucket_defs,
        max_item1_chars=1000,
    )
    assert "NVDA" in user
    assert "NVIDIA Corp" in user
    assert "0000010" in user
    assert "2024" in user
    # Item 1 text is truncated but present
    assert "Item 1 for NVDA" in user
    # Bucket blocks rendered
    assert "id: fabless_chip_design" in user
    assert "id: networking" in user
    # Excluded bucket not present
    assert "id: cybersecurity" not in user
    # Segment table populated (not NOT AVAILABLE)
    assert "NOT AVAILABLE" not in user
    # Loop marker is gone
    assert "{for each shortlisted bucket:}" not in user


def test_build_user_prompt_segments_not_available(scored_settings):
    _seed_db(scored_settings, [("0000011", "AAA", "Alpha", 2024, ["fabless_chip_design"])])
    conn = db.connect(scored_settings.db_path)
    try:
        # Wipe segments so we exercise the NOT AVAILABLE branch
        conn.execute("UPDATE filings SET segments_json = ''")
        conn.commit()
        cands = score.load_candidates(conn)
    finally:
        conn.close()
    _, tpl = score._load_prompt_sections(scored_settings.prompt_path)
    bd = score._load_bucket_defs(scored_settings.definitions_path)
    user = score.build_user_prompt(cands[0], user_template=tpl, bucket_defs=bd, max_item1_chars=1000)
    assert "NOT AVAILABLE" in user


def test_build_batch_requests_creates_two_runs_per_candidate(scored_settings):
    _seed_db(
        scored_settings,
        [
            ("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"]),
            ("0000020", "AMD", "AMD", 2024, ["fabless_chip_design"]),
        ],
    )
    conn = db.connect(scored_settings.db_path)
    try:
        cands = score.load_candidates(conn)
    finally:
        conn.close()
    sys_p, tpl = score._load_prompt_sections(scored_settings.prompt_path)
    bd = score._load_bucket_defs(scored_settings.definitions_path)
    reqs = score.build_batch_requests(
        cands,
        system_prompt=sys_p,
        user_template=tpl,
        bucket_defs=bd,
        model="claude-haiku-4-5",
        max_item1_chars=1000,
    )
    assert len(reqs) == 4  # 2 candidates x 2 runs
    ids = {r["custom_id"] for r in reqs}
    assert ids == {"NVDA_2024_run1", "NVDA_2024_run2", "AMD_2024_run1", "AMD_2024_run2"}
    for r in reqs:
        assert r["params"]["temperature"] == 0
        assert r["params"]["system"] == sys_p
        assert r["params"]["model"] == "claude-haiku-4-5"
        assert r["params"]["messages"][0]["role"] == "user"


def test_load_candidates_respects_ticker_list(scored_settings):
    _seed_db(
        scored_settings,
        [
            ("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"]),
            ("0000020", "AMD", "AMD", 2024, ["fabless_chip_design"]),
        ],
    )
    conn = db.connect(scored_settings.db_path)
    try:
        only_nvda = score.load_candidates(conn, ticker_list=["nvda"])
    finally:
        conn.close()
    assert [c.ticker for c in only_nvda] == ["NVDA"]


# ---------- deep-score list -------------------------------------------------


DEEP_DEFS_YAML = """
buckets:
  - id: fabless_chip_design
    name: Fabless
    definition: chip design
    includes: []
    excludes: []
    anchors: []
deep_score: [AMD]
"""


def _deep_settings(scored_settings, tmp_path: Path):
    """scored_settings with a definitions yaml whose deep_score list is [AMD]."""
    from dataclasses import replace

    defs_path = tmp_path / "deep_defs.yaml"
    defs_path.write_text(DEEP_DEFS_YAML, encoding="utf-8")
    return replace(scored_settings, definitions_path=defs_path)


def test_build_batch_requests_deep_shape(scored_settings):
    """Deep tickers get the deep model, no temperature, 16k max_tokens; others unchanged."""
    _seed_db(
        scored_settings,
        [
            ("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"]),
            ("0000020", "AMD", "AMD", 2024, ["fabless_chip_design"]),
        ],
    )
    conn = db.connect(scored_settings.db_path)
    try:
        cands = score.load_candidates(conn)
    finally:
        conn.close()
    sys_p, tpl = score._load_prompt_sections(scored_settings.prompt_path)
    bd = score._load_bucket_defs(scored_settings.definitions_path)

    common = dict(
        system_prompt=sys_p,
        user_template=tpl,
        bucket_defs=bd,
        model="claude-haiku-4-5",
        max_item1_chars=1000,
    )
    reqs = score.build_batch_requests(
        cands, deep_tickers={"AMD"}, deep_model="claude-opus-5", **common
    )
    by_id = {r["custom_id"]: r["params"] for r in reqs}

    for cid in ("AMD_2024_run1", "AMD_2024_run2"):
        p = by_id[cid]
        assert p["model"] == "claude-opus-5"
        assert "temperature" not in p
        assert p["max_tokens"] == score.DEEP_MAX_TOKENS
        assert p["max_tokens"] >= 16_000

    for cid in ("NVDA_2024_run1", "NVDA_2024_run2"):
        p = by_id[cid]
        assert p["model"] == "claude-haiku-4-5"
        assert p["temperature"] == 0
        assert p["max_tokens"] == score.DEFAULT_MAX_TOKENS

    # Non-deep requests are byte-identical to a build with no deep list at all.
    plain = {r["custom_id"]: r["params"] for r in score.build_batch_requests(cands, **common)}
    assert by_id["NVDA_2024_run1"] == plain["NVDA_2024_run1"]
    assert by_id["NVDA_2024_run2"] == plain["NVDA_2024_run2"]


def test_submit_routes_deep_tickers_from_yaml(scored_settings, tmp_path):
    """submit() reads deep_score from the definitions yaml and routes per request."""
    s = _deep_settings(scored_settings, tmp_path)
    _seed_db(
        s,
        [
            ("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"]),
            ("0000020", "AMD", "AMD", 2024, ["fabless_chip_design"]),
        ],
    )
    fake = FakeClient()
    batch_ids = score.submit(assume_yes=True, client_factory=lambda: fake, settings=s)
    assert len(batch_ids) == 1

    submitted = {r["custom_id"]: r["params"] for r in fake.messages.batches.created[0]}
    assert submitted["AMD_2024_run1"]["model"] == s.deep_score_model
    assert "temperature" not in submitted["AMD_2024_run1"]
    assert submitted["AMD_2024_run1"]["max_tokens"] == score.DEEP_MAX_TOKENS
    assert submitted["NVDA_2024_run1"]["model"] == s.batch_model
    assert submitted["NVDA_2024_run1"]["temperature"] == 0


def test_poll_refusal_goes_to_failures(scored_settings, capsys):
    _seed_db(scored_settings, [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"])])
    _prime_batch(scored_settings, "b_ref", ["NVDA_2024_run1"])

    fake = FakeClient()
    fake.messages.batches.results_map["b_ref"] = [
        _Individual(
            "NVDA_2024_run1",
            _Res("succeeded", message=_Msg("", model="claude-opus-5", stop_reason="refusal")),
        ),
    ]

    result = score.poll(client_factory=lambda: fake, settings=scored_settings)
    assert result["n_inserted"] == 0
    assert result["n_failures"] == 1
    failures = json.loads((scored_settings.data_dir / score.FAILURES_FILENAME).read_text())
    assert failures[0]["reason"] == "refusal"
    assert "REFUSED" in capsys.readouterr().out


def test_poll_deep_model_supersedes_other_models(scored_settings):
    """Ingesting deep-model rows deletes the same company's other-model rows
    at the same prompt_version, and leaves other companies untouched."""
    _seed_db(
        scored_settings,
        [
            ("0000020", "AMD", "AMD", 2024, ["fabless_chip_design"]),
            ("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"]),
        ],
    )
    pv = scored_settings.prompt_version
    conn = db.connect(scored_settings.db_path)
    try:
        with db.transaction(conn):
            for cik, ticker in (("0000020", "AMD"), ("0000010", "NVDA")):
                for run in (1, 2):
                    db.insert_score(
                        conn, cik=cik, ticker=ticker, bucket_id="fabless_chip_design",
                        fy=2024, run=run, score=0.5, confidence="high", rationale="old",
                        evidence_type="segment_data", pre_revenue=0,
                        prompt_version=pv, model_version="claude-haiku-4-5",
                        created_at="2026-01-01T00:00:00Z",
                    )
    finally:
        conn.close()

    _prime_batch(scored_settings, "b_deep", ["AMD_2024_run1", "AMD_2024_run2"])
    fake = FakeClient()
    fake.messages.batches.results_map["b_deep"] = [
        _Individual(
            f"AMD_2024_run{run}",
            _Res(
                "succeeded",
                message=_Msg(
                    _valid_response_json("AMD", 2024, ["fabless_chip_design"]),
                    model=scored_settings.deep_score_model,
                ),
            ),
        )
        for run in (1, 2)
    ]

    result = score.poll(client_factory=lambda: fake, settings=scored_settings)
    assert result["n_inserted"] == 2
    assert result["n_failures"] == 0

    conn = db.connect(scored_settings.db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, model_version, COUNT(*) AS n FROM scores "
            "GROUP BY ticker, model_version ORDER BY ticker"
        ).fetchall()
    finally:
        conn.close()
    got = {(r["ticker"], r["model_version"]): r["n"] for r in rows}
    # AMD's haiku rows are gone; only the deep-model rows remain.
    assert got == {
        ("AMD", scored_settings.deep_score_model): 2,
        ("NVDA", "claude-haiku-4-5"): 2,
    }


# ---------- fence stripping -------------------------------------------------


def test_strip_markdown_fence_variants():
    assert score._strip_markdown_fence('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert score._strip_markdown_fence('```\n{"a": 1}\n```') == '{"a": 1}'
    assert score._strip_markdown_fence('{"a": 1}') == '{"a": 1}'
    assert score._strip_markdown_fence("   ```json\n{\"a\":1}\n```   ") == '{"a":1}'


# ---------- submit + confirmation ------------------------------------------


def test_submit_assume_yes_creates_batch(scored_settings, monkeypatch, capsys):
    _seed_db(
        scored_settings,
        [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design", "networking"])],
    )
    fake = FakeClient()
    batch_ids = score.submit(
        assume_yes=True,
        client_factory=lambda: fake,
        settings=scored_settings,
    )
    assert len(batch_ids) == 1
    # Two runs per candidate
    assert len(fake.messages.batches.created) == 1
    assert len(fake.messages.batches.created[0]) == 2
    cids = {r["custom_id"] for r in fake.messages.batches.created[0]}
    assert cids == {"NVDA_2024_run1", "NVDA_2024_run2"}

    # Batch row inserted with in_progress status
    conn = db.connect(scored_settings.db_path)
    try:
        rows = conn.execute("SELECT batch_id, status, meta_json FROM batches").fetchall()
    finally:
        conn.close()
    assert len(rows) == 1
    assert rows[0]["status"] == "in_progress"
    meta = json.loads(rows[0]["meta_json"])
    assert meta["n_requests"] == 2
    assert set(meta["custom_ids"]) == cids

    # Cost message printed
    out = capsys.readouterr().out
    assert "requests" in out and "cost estimate" in out


def test_submit_confirmation_declines(scored_settings, monkeypatch):
    _seed_db(scored_settings, [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"])])
    fake = FakeClient()
    monkeypatch.setattr("builtins.input", lambda *a, **kw: "no")
    batch_ids = score.submit(
        assume_yes=False,
        client_factory=lambda: fake,
        settings=scored_settings,
    )
    assert batch_ids == []
    assert fake.messages.batches.created == []


def test_submit_refuses_when_item1_unreadable(scored_settings):
    """A candidate whose item1_path doesn't resolve must block the whole submit."""
    _seed_db(scored_settings, [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"])])
    conn = db.connect(scored_settings.db_path)
    try:
        conn.execute("UPDATE filings SET item1_path = '/nonexistent/machine/path.txt'")
        conn.commit()
    finally:
        conn.close()

    fake = FakeClient()
    with pytest.raises(SystemExit, match="REFUSING.*NVDA"):
        score.submit(assume_yes=True, client_factory=lambda: fake, settings=scored_settings)
    assert fake.messages.batches.created == []


def test_submit_no_candidates_is_noop(scored_settings):
    conn = db.connect(scored_settings.db_path)
    try:
        db.init_schema(conn)
    finally:
        conn.close()
    fake = FakeClient()
    batch_ids = score.submit(
        assume_yes=True,
        client_factory=lambda: fake,
        settings=scored_settings,
    )
    assert batch_ids == []


def test_submit_ticker_list_filters(scored_settings):
    _seed_db(
        scored_settings,
        [
            ("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"]),
            ("0000020", "AMD", "AMD", 2024, ["fabless_chip_design"]),
        ],
    )
    fake = FakeClient()
    score.submit(
        ticker_list=["NVDA"],
        assume_yes=True,
        client_factory=lambda: fake,
        settings=scored_settings,
    )
    cids = {r["custom_id"] for r in fake.messages.batches.created[0]}
    assert cids == {"NVDA_2024_run1", "NVDA_2024_run2"}


# ---------- poll: valid / malformed / schema failures ---------------------


def _prime_batch(scored_settings, batch_id: str, custom_ids: list[str]) -> None:
    conn = db.connect(scored_settings.db_path)
    try:
        db.init_schema(conn)
        with db.transaction(conn):
            db.upsert_batch(
                conn,
                batch_id=batch_id,
                submitted_at="2025-02-01T00:00:00Z",
                status="in_progress",
                meta_json=json.dumps({"n_requests": len(custom_ids), "custom_ids": custom_ids}),
            )
    finally:
        conn.close()


def test_poll_ingests_valid_response(scored_settings):
    _seed_db(
        scored_settings,
        [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design", "networking"])],
    )
    _prime_batch(scored_settings, "b1", ["NVDA_2024_run1", "NVDA_2024_run2"])

    fake = FakeClient()
    fake.messages.batches.results_map["b1"] = [
        _Individual(
            "NVDA_2024_run1",
            _Res(
                "succeeded",
                message=_Msg(_valid_response_json("NVDA", 2024, ["fabless_chip_design", "networking"])),
            ),
        ),
        _Individual(
            "NVDA_2024_run2",
            _Res(
                "succeeded",
                message=_Msg(
                    "```json\n" + _valid_response_json("NVDA", 2024, ["fabless_chip_design", "networking"]) + "\n```"
                ),
            ),
        ),
    ]

    result = score.poll(client_factory=lambda: fake, settings=scored_settings)
    assert result["n_inserted"] == 4  # 2 buckets * 2 runs
    assert result["n_failures"] == 0

    conn = db.connect(scored_settings.db_path)
    try:
        rows = conn.execute(
            "SELECT ticker, bucket_id, run, score, model_version, prompt_version "
            "FROM scores ORDER BY bucket_id, run"
        ).fetchall()
        batch_status = conn.execute(
            "SELECT status FROM batches WHERE batch_id='b1'"
        ).fetchone()["status"]
    finally:
        conn.close()
    assert batch_status == "ended"
    assert len(rows) == 4
    # Both runs present per bucket (PK includes run — no dedupe).
    per_bucket_runs = {}
    for r in rows:
        per_bucket_runs.setdefault(r["bucket_id"], set()).add(r["run"])
    for runs in per_bucket_runs.values():
        assert runs == {1, 2}
    for r in rows:
        assert r["model_version"] == "claude-haiku-4-5"
        assert r["prompt_version"] == scored_settings.prompt_version


def test_poll_malformed_and_schema_invalid_go_to_failures(scored_settings):
    _seed_db(
        scored_settings,
        [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"])],
    )
    _prime_batch(scored_settings, "b2", ["NVDA_2024_run1", "NVDA_2024_run2"])

    fake = FakeClient()
    fake.messages.batches.results_map["b2"] = [
        _Individual(
            "NVDA_2024_run1",
            _Res("succeeded", message=_Msg("this is not JSON at all!")),
        ),
        _Individual(
            "NVDA_2024_run2",
            _Res(
                "succeeded",
                message=_Msg(
                    json.dumps({
                        "ticker": "NVDA",
                        "fy": 2024,
                        "pre_revenue": False,
                        "scores": [
                            {
                                "bucket_id": "fabless_chip_design",
                                "score": 1.5,  # out of range -> ValidationError
                                "confidence": "high",
                                "rationale": "x",
                                "evidence_type": "segment_data",
                            }
                        ],
                    })
                ),
            ),
        ),
    ]

    result = score.poll(client_factory=lambda: fake, settings=scored_settings)
    assert result["n_inserted"] == 0
    assert result["n_failures"] == 2

    conn = db.connect(scored_settings.db_path)
    try:
        n_scores = conn.execute("SELECT COUNT(*) FROM scores").fetchone()[0]
    finally:
        conn.close()
    assert n_scores == 0

    failures_path = scored_settings.data_dir / score.FAILURES_FILENAME
    assert failures_path.exists()
    failures = json.loads(failures_path.read_text())
    assert {f["custom_id"] for f in failures} == {"NVDA_2024_run1", "NVDA_2024_run2"}
    reasons = " ".join(f["reason"] for f in failures)
    assert "json_decode" in reasons or "schema" in reasons


def test_poll_missing_custom_id_goes_to_failures(scored_settings):
    _seed_db(scored_settings, [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"])])
    _prime_batch(scored_settings, "b3", ["NVDA_2024_run1", "NVDA_2024_run2"])

    fake = FakeClient()
    # Only run1 comes back; run2 is missing.
    fake.messages.batches.results_map["b3"] = [
        _Individual(
            "NVDA_2024_run1",
            _Res(
                "succeeded",
                message=_Msg(_valid_response_json("NVDA", 2024, ["fabless_chip_design"])),
            ),
        ),
    ]

    result = score.poll(client_factory=lambda: fake, settings=scored_settings)
    assert result["n_inserted"] == 1
    assert result["n_failures"] == 1

    failures = json.loads((scored_settings.data_dir / score.FAILURES_FILENAME).read_text())
    assert failures[0]["custom_id"] == "NVDA_2024_run2"
    assert failures[0]["reason"] == "missing_from_batch_results"


def test_poll_errored_result_goes_to_failures(scored_settings):
    _seed_db(scored_settings, [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"])])
    _prime_batch(scored_settings, "b4", ["NVDA_2024_run1"])

    fake = FakeClient()
    fake.messages.batches.results_map["b4"] = [
        _Individual("NVDA_2024_run1", _Res("errored", error={"type": "server_error"})),
    ]

    result = score.poll(client_factory=lambda: fake, settings=scored_settings)
    assert result["n_failures"] == 1
    failures = json.loads((scored_settings.data_dir / score.FAILURES_FILENAME).read_text())
    assert failures[0]["reason"].startswith("result_type:errored")


def test_poll_skips_batch_still_in_progress(scored_settings):
    _seed_db(scored_settings, [("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"])])
    _prime_batch(scored_settings, "b5", ["NVDA_2024_run1"])

    fake = FakeClient()
    fake.messages.batches.status_map["b5"] = "in_progress"

    result = score.poll(client_factory=lambda: fake, settings=scored_settings)
    assert result["n_inserted"] == 0
    # batch stays open
    conn = db.connect(scored_settings.db_path)
    try:
        status = conn.execute("SELECT status FROM batches WHERE batch_id='b5'").fetchone()["status"]
    finally:
        conn.close()
    assert status == "in_progress"


# ---------- retry -----------------------------------------------------------


def test_retry_resubmits_only_failures(scored_settings):
    _seed_db(
        scored_settings,
        [
            ("0000010", "NVDA", "NVIDIA", 2024, ["fabless_chip_design"]),
            ("0000020", "AMD", "AMD", 2024, ["fabless_chip_design"]),
        ],
    )
    # Simulate a prior poll that left one failure for NVDA run2 only.
    failures = [{"custom_id": "NVDA_2024_run2", "reason": "json_decode:oops", "raw": "bad"}]
    (scored_settings.data_dir / score.FAILURES_FILENAME).write_text(json.dumps(failures))

    fake = FakeClient()
    batch_ids = score.retry(
        assume_yes=True,
        client_factory=lambda: fake,
        settings=scored_settings,
    )
    assert len(batch_ids) == 1
    submitted = fake.messages.batches.created[0]
    assert len(submitted) == 1
    assert submitted[0]["custom_id"] == "NVDA_2024_run2"
    # Failures file cleared
    remaining = json.loads((scored_settings.data_dir / score.FAILURES_FILENAME).read_text())
    assert remaining == []


def test_retry_noop_when_no_failures(scored_settings):
    fake = FakeClient()
    batch_ids = score.retry(
        assume_yes=True,
        client_factory=lambda: fake,
        settings=scored_settings,
    )
    assert batch_ids == []
    assert fake.messages.batches.created == []
