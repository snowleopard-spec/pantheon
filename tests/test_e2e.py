"""End-to-end integration test per spec §10.

Drives stages 4 → 7 with 3 bundled fake filings and a stubbed scorer,
exercising the module seams that real runs will use. No network, no
model download, no Anthropic API calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
import yaml

from minidex import config, db, indices, qc, shortlist


FAKE_ITEM1_TEXTS = {
    "NVDA": (
        "NVIDIA Corporation designs graphics processing units and system-on-a-chip "
        "products for gaming, data center, professional visualization and automotive "
        "markets. Revenue is driven by sales of accelerated computing platforms, GPUs "
        "and networking silicon. Our fabless business model outsources wafer "
        "fabrication to third-party foundries."
    ),
    "AMAT": (
        "Applied Materials provides manufacturing equipment, services and software "
        "to the semiconductor, display and related industries. Revenue comes from "
        "deposition, etch, inspection and metrology systems sold to semiconductor "
        "manufacturers, together with associated services and spare parts."
    ),
    "EQIX": (
        "Equinix operates data centers globally, offering interconnection and colocation "
        "services to enterprises, cloud providers and networks. Revenue is derived from "
        "recurring colocation, cross-connects and managed services in our International "
        "Business Exchange facilities across 30+ countries."
    ),
}


def _seed_companies_and_filings(conn, raw_dir: Path) -> dict[str, str]:
    """Insert 3 companies with is_candidate=1, one filing each, one Item 1 file each."""
    fake_ciks = {"NVDA": "0000001045", "AMAT": "0000006951", "EQIX": "0001101239"}
    for ticker, cik in fake_ciks.items():
        db.upsert_company(
            conn,
            cik=cik,
            ticker=ticker,
            name=f"{ticker} Inc.",
            sic="3674" if ticker != "EQIX" else "6798",
            exchange="NASDAQ",
            is_candidate=1,
            market_cap=1_000_000_000.0 * (1 if ticker == "EQIX" else 5),
            market_cap_asof="2024-12-31",
        )
        item1_path = raw_dir / f"{ticker}_item1.txt"
        item1_path.write_text(FAKE_ITEM1_TEXTS[ticker], encoding="utf-8")
        db.upsert_filing(
            conn,
            cik=cik,
            accession=f"acc-{ticker}",
            fy=2024,
            filed_date="2025-01-15",
            item1_path=str(item1_path),
            item1_chars=len(FAKE_ITEM1_TEXTS[ticker]),
            segments_json=json.dumps([]),
        )
    conn.commit()
    return fake_ciks


class _FakeEncoder:
    """Sentence-transformer stand-in.

    Emits a deterministic 8-dim unit vector per input string. Bucket texts and
    Item 1 texts are matched via hand-tuned vectors so NVDA aligns with
    fabless_chip_design, AMAT with semicap_equipment, EQIX with data_centers.
    """

    max_seq_length = 512
    tokenizer = None  # short-circuits the truncation helper

    _VECTORS: dict[str, np.ndarray] = {}

    def encode(self, texts, normalize_embeddings=True, **_):
        out = []
        for t in texts:
            v = self._VECTORS.get(t)
            if v is None:
                # deterministic hash-based unit vector, orthogonal-ish to seeded ones
                seed = abs(hash(t)) % (2**31)
                rng = np.random.default_rng(seed)
                v = rng.standard_normal(8)
                v = v / np.linalg.norm(v)
            out.append(v)
        return np.asarray(out)

    @classmethod
    def seed(cls, text: str, vec: list[float]) -> None:
        arr = np.asarray(vec, dtype=float)
        cls._VECTORS[text] = arr / np.linalg.norm(arr)


def _insert_fake_scores(conn, cik_by_ticker: dict[str, str]) -> None:
    """Stubbed scorer — write plausible run-1 + run-2 scores per (ticker, bucket)."""
    now = datetime.now(timezone.utc).isoformat()
    # (ticker, bucket_id, run1, run2, confidence, evidence_type)
    fixtures = [
        ("NVDA", "fabless_chip_design", 0.90, 0.85, "high", "segment_data"),
        ("NVDA", "networking_silicon", 0.20, 0.25, "medium", "description_only"),
        ("AMAT", "semicap_equipment", 0.95, 0.92, "high", "segment_data"),
        ("EQIX", "data_centers", 0.88, 0.86, "high", "segment_data"),
    ]
    for ticker, bucket_id, r1, r2, conf, ev in fixtures:
        cik = cik_by_ticker[ticker]
        for run, sc in ((1, r1), (2, r2)):
            db.insert_score(
                conn,
                cik=cik,
                ticker=ticker,
                bucket_id=bucket_id,
                fy=2024,
                run=run,
                score=sc,
                confidence=conf,
                rationale=f"{ticker} rationale run {run}",
                evidence_type=ev,
                pre_revenue=0,
                prompt_version="1.0",
                model_version="claude-haiku-4-5",
                created_at=now,
            )
    conn.commit()


def _make_test_settings(tmp_path: Path) -> config.Settings:
    """Build a Settings pinned to tmp_path but referencing the real def+prompt."""
    real = config.get_settings()  # fixture in conftest supplies fake secrets
    data_dir = tmp_path / "data"
    raw_dir = data_dir / "raw"
    outputs_dir = tmp_path / "outputs"
    for d in (data_dir, raw_dir, outputs_dir):
        d.mkdir(parents=True, exist_ok=True)
    return config.Settings(
        anthropic_api_key=real.anthropic_api_key,
        sec_user_agent=real.sec_user_agent,
        repo_root=real.repo_root,
        data_dir=data_dir,
        raw_dir=raw_dir,
        outputs_dir=outputs_dir,
        db_path=data_dir / "minidex.db",
        definitions_path=real.definitions_path,
        prompt_path=real.prompt_path,
        embedding_model="fake-encoder",
        similarity_threshold=0.60,
        score_floor=0.10,
        batch_model=real.batch_model,
        prompt_version=real.prompt_version,
        max_item1_chars=real.max_item1_chars,
    )


@pytest.fixture()
def bucket_ids_from_yaml() -> list[str]:
    with open(config.get_settings().definitions_path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return [b["id"] for b in data["buckets"]]


def test_e2e_stages_4_through_7(tmp_path: Path, bucket_ids_from_yaml: list[str]) -> None:
    settings = _make_test_settings(tmp_path)

    # Seed stages 1-3 outputs directly.
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    cik_by_ticker = _seed_companies_and_filings(conn, settings.raw_dir)
    conn.close()

    # Prime FakeEncoder so specific (candidate, bucket) pairs land above threshold.
    # Use unit vectors: canonical e_i basis directions per bucket, and per-ticker
    # vectors that align with their intended bucket(s).
    e = {name: [0.0] * 8 for name in ("nvda", "amat", "eqix")}
    e["nvda"][0] = 1.0
    e["amat"][1] = 1.0
    e["eqix"][2] = 1.0

    # Get the bucket embed texts the module will actually pass in.
    buckets = shortlist.load_bucket_defs(settings.definitions_path)
    for b in buckets:
        text = shortlist.bucket_embed_text(b)
        vec = [0.0] * 8
        if b["id"] == "fabless_chip_design":
            vec[0] = 1.0
        elif b["id"] == "semicap_equipment":
            vec[1] = 1.0
        elif b["id"] == "data_centers":
            vec[2] = 1.0
        else:
            # Off-axis, tiny random contribution — well below threshold.
            vec[7] = 0.01
        _FakeEncoder.seed(text, vec)

    _FakeEncoder.seed(FAKE_ITEM1_TEXTS["NVDA"], e["nvda"])
    _FakeEncoder.seed(FAKE_ITEM1_TEXTS["AMAT"], e["amat"])
    _FakeEncoder.seed(FAKE_ITEM1_TEXTS["EQIX"], e["eqix"])

    # ---- Stage 4: shortlist ----
    summary = shortlist.run(model_factory=lambda _name: _FakeEncoder(), settings=settings)
    assert summary["n_candidates"] == 3
    assert summary["n_pairs"] >= 3  # at least the three self-alignments

    # ---- Stage 5: stubbed scorer ----
    conn = db.connect(settings.db_path)
    _insert_fake_scores(conn, cik_by_ticker)
    conn.close()

    # ---- Stage 6: qc ----
    qc_path = qc.run(settings=settings)
    report = qc_path.read_text(encoding="utf-8")
    assert "# mini-dex QC report" in report or "QC" in report
    # NVDA's fabless anchor should PASS (mean 0.875)
    assert "NVDA" in report
    # A borderline row exists (NVDA on networking_silicon mean 0.225)
    assert "networking_silicon" in report or "Borderline" in report

    # ---- Stage 7: build ----
    result = indices.build("2025-01-15", settings=settings)
    out_dir = result["output_dir"]
    csv_path = out_dir / "minidex_weights.csv"
    parquet_path = out_dir / "minidex_weights.parquet"
    manifest_path = out_dir / "manifest.json"
    assert csv_path.exists() and parquet_path.exists() and manifest_path.exists()

    df = pd.read_csv(csv_path)
    # weight columns each sum to 1.0 per bucket
    for wcol in ("weight_cap_score", "weight_equal", "weight_score"):
        sums = df.groupby("bucket_id")[wcol].sum()
        assert ((sums - 1.0).abs() < 1e-6).all(), f"{wcol} sums: {sums.to_dict()}"

    # Every scored bucket appears (NVDA:networking_silicon has score 0.225 >= 0.10 floor)
    scored_buckets = {"fabless_chip_design", "networking_silicon", "semicap_equipment", "data_centers"}
    assert scored_buckets <= set(df["bucket_id"].unique())

    manifest = json.loads(manifest_path.read_text())
    assert manifest["asof"] == "2025-01-15"
    assert manifest["n_companies_scored"] == 3
    assert manifest["prompt_version"] == "1.0"
    assert manifest["model_version"] == "claude-haiku-4-5"
    assert len(manifest["definitions_sha256"]) == 64
    assert len(manifest["prompt_sha256"]) == 64
    assert manifest["filing_fy_histogram"] == {"2024": 3}
