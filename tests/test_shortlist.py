from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from minidex import config, db, shortlist


# -----------------------------
# Fake sentence-transformer
# -----------------------------

class FakeEncoder:
    """Deterministic fake sentence-transformer.

    Each unique input text maps to a fixed unit vector, seeded from a hash of
    the text. Optionally, the caller can pre-register (text -> vector) so
    tests can construct specific similarity relationships.
    """

    dim = 8
    max_seq_length = 512

    def __init__(self, overrides: dict[str, np.ndarray] | None = None):
        self.overrides = overrides or {}
        self.tokenizer = None  # exercise the "no tokenizer" truncation branch

    def _vec_for(self, text: str) -> np.ndarray:
        if text in self.overrides:
            v = np.asarray(self.overrides[text], dtype=np.float64)
        else:
            seed = int.from_bytes(
                hashlib.sha256(text.encode("utf-8")).digest()[:4], "big"
            )
            rng = np.random.default_rng(seed)
            v = rng.standard_normal(self.dim)
        norm = np.linalg.norm(v)
        if norm == 0:
            v = np.ones(self.dim, dtype=np.float64)
            norm = np.linalg.norm(v)
        return v / norm

    def encode(self, texts, normalize_embeddings: bool = True):
        assert normalize_embeddings is True, "shortlist must request normalized embeddings"
        return np.vstack([self._vec_for(t) for t in texts])


def _make_settings(tmp_path: Path, threshold: float = 0.60, definitions_text: str | None = None):
    s = config.get_settings()
    defs_path = tmp_path / "defs.yaml"
    defs_path.write_text(definitions_text or DEFAULT_DEFS, encoding="utf-8")
    return replace(
        s,
        db_path=tmp_path / "minidex.db",
        definitions_path=defs_path,
        similarity_threshold=threshold,
    )


# -----------------------------
# Fixtures / data
# -----------------------------

DEFAULT_DEFS = """
buckets:
  - id: bucket_a
    name: Bucket A
    definition: definition-a
    includes: [a1, a2]
    anchors: [AAA]
  - id: bucket_b
    name: Bucket B
    definition: definition-b
    includes: [b1]
    anchors: [BBB]
  - id: bucket_c
    name: Bucket C
    definition: definition-c
    includes: []
    anchors: []
"""


def _seed_company_with_item1(conn, tmp_path: Path, cik: str, ticker: str, text: str) -> str:
    item1 = tmp_path / f"item1_{cik}.txt"
    item1.write_text(text, encoding="utf-8")
    db.upsert_company(conn, cik=cik, ticker=ticker, name=ticker, sic="7372", is_candidate=1)
    db.upsert_filing(
        conn,
        cik=cik,
        accession=f"acc-{cik}",
        fy=2024,
        filed_date="2025-01-15",
        item1_path=str(item1),
        item1_chars=len(text),
        segments_json="[]",
    )
    conn.commit()
    return str(item1)


# -----------------------------
# Unit tests: helpers
# -----------------------------

def test_bucket_embed_text_joins_definition_and_includes():
    b = {"definition": "core def", "includes": ["one", "two", "three"]}
    out = shortlist.bucket_embed_text(b)
    assert "core def" in out
    assert "one" in out and "two" in out and "three" in out


def test_load_bucket_defs(tmp_path: Path):
    p = tmp_path / "d.yaml"
    p.write_text(DEFAULT_DEFS, encoding="utf-8")
    got = shortlist.load_bucket_defs(p)
    assert [b["id"] for b in got] == ["bucket_a", "bucket_b", "bucket_c"]


def test_compute_pairs_above_threshold():
    sim = np.array(
        [
            [0.9, 0.4, 0.61],
            [0.1, 0.2, 0.3],
        ]
    )
    pairs = shortlist.compute_pairs(sim, ["c1", "c2"], ["b1", "b2", "b3"], threshold=0.6)
    pairs_set = {(c, b) for c, b, _ in pairs}
    assert pairs_set == {("c1", "b1"), ("c1", "b3")}


# -----------------------------
# End-to-end tests through run()
# -----------------------------

def test_run_writes_expected_pairs(tmp_path: Path):
    """Cosine sim math: given crafted embeddings, only pairs > threshold are inserted."""
    settings = _make_settings(tmp_path, threshold=0.60)

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_company_with_item1(conn, tmp_path, "cik_a", "AAA", "text-for-A")
    _seed_company_with_item1(conn, tmp_path, "cik_b", "BBB", "text-for-B")
    _seed_company_with_item1(conn, tmp_path, "cik_c", "CCC", "text-for-C")
    conn.close()

    e1 = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=float)
    e2 = np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=float)
    e3 = np.array([0, 0, 1, 0, 0, 0, 0, 0], dtype=float)

    # AAA aligns to bucket_a (sim=1); BBB to bucket_b (sim=1); CCC to bucket_c (sim=1).
    # No cross-pairs above the 0.60 threshold.
    overrides = {
        "text-for-A": e1,
        "text-for-B": e2,
        "text-for-C": e3,
        "definition-a a1, a2": e1,
        "definition-b b1": e2,
        "definition-c": e3,
    }
    encoder = FakeEncoder(overrides=overrides)
    result = shortlist.run(model_factory=lambda name: encoder, settings=settings)

    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT cik, bucket_id, similarity, source FROM shortlist ORDER BY cik, bucket_id"
    ).fetchall()
    conn.close()

    pair_set = {(r["cik"], r["bucket_id"]) for r in rows}
    # Anchors AAA->bucket_a and BBB->bucket_b are guaranteed. CCC->bucket_c is
    # via embed (sim=1.0). No other pairs above threshold.
    assert pair_set == {
        ("cik_a", "bucket_a"),
        ("cik_b", "bucket_b"),
        ("cik_c", "bucket_c"),
    }

    src = {(r["cik"], r["bucket_id"]): r["source"] for r in rows}
    assert src[("cik_a", "bucket_a")] == "anchor"
    assert src[("cik_b", "bucket_b")] == "anchor"
    assert src[("cik_c", "bucket_c")] == "embed"
    assert result["n_pairs"] == 3


def test_anchor_forced_when_similarity_below_threshold(tmp_path: Path, capsys):
    """Anchor pair with sim < threshold must still be written as source='anchor', and warn."""
    settings = _make_settings(tmp_path, threshold=0.60)

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_company_with_item1(conn, tmp_path, "cik_a", "AAA", "text-for-A")
    conn.close()

    # Force very low cosine similarity between AAA and bucket_a (its anchor).
    # Orthogonal vectors -> sim = 0.0, well below 0.60.
    v_company = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=float)
    v_bucket_a = np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=float)
    v_bucket_b = np.array([0, 0, 1, 0, 0, 0, 0, 0], dtype=float)
    v_bucket_c = np.array([0, 0, 0, 1, 0, 0, 0, 0], dtype=float)
    overrides = {
        "text-for-A": v_company,
        "definition-a a1, a2": v_bucket_a,
        "definition-b b1": v_bucket_b,
        "definition-c": v_bucket_c,
    }
    encoder = FakeEncoder(overrides=overrides)
    result = shortlist.run(model_factory=lambda name: encoder, settings=settings)

    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT cik, bucket_id, similarity, source FROM shortlist"
    ).fetchall()
    conn.close()

    assert len(rows) == 1
    r = rows[0]
    assert (r["cik"], r["bucket_id"]) == ("cik_a", "bucket_a")
    assert r["source"] == "anchor"
    assert r["similarity"] == pytest.approx(0.0, abs=1e-9)

    assert result["anchor_min_similarity"] == pytest.approx(0.0, abs=1e-9)

    captured = capsys.readouterr().out
    assert "WARNING" in captured
    assert "anchor" in captured.lower()


def test_anchor_overrides_embed_source(tmp_path: Path):
    """If an anchor pair is also above the embed threshold, source must be 'anchor'."""
    settings = _make_settings(tmp_path, threshold=0.60)

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_company_with_item1(conn, tmp_path, "cik_a", "AAA", "text-for-A")
    conn.close()

    v = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=float)
    overrides = {
        "text-for-A": v,
        "definition-a a1, a2": v,  # sim = 1.0 -> would be embed
        "definition-b b1": np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=float),
        "definition-c": np.array([0, 0, 1, 0, 0, 0, 0, 0], dtype=float),
    }
    encoder = FakeEncoder(overrides=overrides)
    shortlist.run(model_factory=lambda name: encoder, settings=settings)

    conn = db.connect(settings.db_path)
    r = conn.execute(
        "SELECT source, similarity FROM shortlist WHERE cik='cik_a' AND bucket_id='bucket_a'"
    ).fetchone()
    conn.close()
    assert r["source"] == "anchor"
    assert r["similarity"] == pytest.approx(1.0, abs=1e-9)


def test_pair_count_and_median_synthetic(tmp_path: Path):
    """Synthetic scenario: 4 companies, 3 buckets, threshold engineered so we
    expect a predictable pair count and median-buckets-per-company."""
    settings = _make_settings(tmp_path, threshold=0.50)

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    # Two anchors (AAA, BBB) plus two non-anchor companies.
    _seed_company_with_item1(conn, tmp_path, "cik_a", "AAA", "t-a")
    _seed_company_with_item1(conn, tmp_path, "cik_b", "BBB", "t-b")
    _seed_company_with_item1(conn, tmp_path, "cik_x", "XXX", "t-x")
    _seed_company_with_item1(conn, tmp_path, "cik_y", "YYY", "t-y")
    conn.close()

    # Vectors: pick these so specific pairs are above 0.50.
    # cik_a aligns to bucket_a (anchor). Also 0.6 sim to bucket_c.
    # cik_b aligns to bucket_b (anchor). Nothing else above threshold.
    # cik_x aligns to bucket_c only (sim=1.0).
    # cik_y aligns to nothing above threshold => zero embed pairs.
    a_axis = np.array([1, 0, 0], dtype=float)
    b_axis = np.array([0, 1, 0], dtype=float)
    c_axis = np.array([0, 0, 1], dtype=float)
    mix_ac = np.array([0.8, 0.0, 0.6])  # sim to a=0.8, sim to c=0.6
    mix_none = np.array([0.4, 0.4, 0.4])  # ~0.577 total; sims to axes = 0.577 each

    def _pad(v):
        out = np.zeros(FakeEncoder.dim, dtype=float)
        out[: len(v)] = v
        return out / np.linalg.norm(out)

    overrides = {
        "t-a": _pad(mix_ac),
        "t-b": _pad(b_axis),
        "t-x": _pad(c_axis),
        "t-y": np.zeros(FakeEncoder.dim, dtype=float),  # will get default random vec (see below)
        "definition-a a1, a2": _pad(a_axis),
        "definition-b b1": _pad(b_axis),
        "definition-c": _pad(c_axis),
    }
    # Force cik_y to have no above-threshold embed pairs by picking an axis
    # orthogonal to a/b/c axes.
    overrides["t-y"] = np.array([0, 0, 0, 1, 0, 0, 0, 0], dtype=float)

    encoder = FakeEncoder(overrides=overrides)
    result = shortlist.run(model_factory=lambda name: encoder, settings=settings)

    conn = db.connect(settings.db_path)
    rows = conn.execute("SELECT cik, bucket_id, source FROM shortlist").fetchall()
    per_company = {}
    for r in rows:
        per_company.setdefault(r["cik"], []).append((r["bucket_id"], r["source"]))
    conn.close()

    # cik_a: embed(bucket_a sim=0.8, bucket_c sim=0.6) + anchor(bucket_a)
    #        anchor wins on bucket_a, so 2 pairs total (bucket_a source=anchor, bucket_c source=embed)
    assert sorted(b for b, _ in per_company["cik_a"]) == ["bucket_a", "bucket_c"]
    src_map = dict(per_company["cik_a"])
    assert src_map["bucket_a"] == "anchor"
    assert src_map["bucket_c"] == "embed"

    # cik_b: anchor(bucket_b) only
    assert per_company["cik_b"] == [("bucket_b", "anchor")]

    # cik_x: embed(bucket_c)
    assert per_company["cik_x"] == [("bucket_c", "embed")]

    # cik_y: no pairs
    assert "cik_y" not in per_company

    assert result["n_pairs"] == 4
    # Median across companies that have pairs: [2, 1, 1] -> 1.0
    assert result["median_per_company"] == pytest.approx(1.0)


# -----------------------------
# Deep-score list
# -----------------------------

DEEP_DEFS = DEFAULT_DEFS + "\ndeep_score: [DDD]\n"


def _orthogonal_overrides():
    """Bucket vectors on axes 0-2; company vectors chosen per test."""
    return {
        "definition-a a1, a2": np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=float),
        "definition-b b1": np.array([0, 1, 0, 0, 0, 0, 0, 0], dtype=float),
        "definition-c": np.array([0, 0, 1, 0, 0, 0, 0, 0], dtype=float),
    }


def test_load_deep_score_tickers(tmp_path: Path):
    p = tmp_path / "d.yaml"
    p.write_text(DEEP_DEFS, encoding="utf-8")
    assert shortlist.load_deep_score_tickers(p) == ["DDD"]
    p.write_text(DEFAULT_DEFS, encoding="utf-8")
    assert shortlist.load_deep_score_tickers(p) == []


def test_deep_ticker_gets_all_buckets_regardless_of_similarity(tmp_path: Path):
    """A deep-score name below threshold on every bucket still gets every bucket."""
    settings = _make_settings(tmp_path, threshold=0.60, definitions_text=DEEP_DEFS)

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_company_with_item1(conn, tmp_path, "cik_d", "DDD", "text-for-D")
    conn.close()

    overrides = _orthogonal_overrides()
    # Orthogonal to every bucket axis: all similarities are 0.0.
    overrides["text-for-D"] = np.array([0, 0, 0, 1, 0, 0, 0, 0], dtype=float)

    encoder = FakeEncoder(overrides=overrides)
    result = shortlist.run(model_factory=lambda name: encoder, settings=settings)

    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT bucket_id, similarity, source FROM shortlist WHERE cik='cik_d' "
        "ORDER BY bucket_id"
    ).fetchall()
    conn.close()

    assert [r["bucket_id"] for r in rows] == ["bucket_a", "bucket_b", "bucket_c"]
    assert all(r["source"] == "deep" for r in rows)
    assert all(r["similarity"] == pytest.approx(0.0, abs=1e-9) for r in rows)
    assert result["n_deep_pairs"] == 3


def test_anchor_source_wins_over_deep(tmp_path: Path):
    """A deep-score name that is also an anchor keeps 'anchor' on its anchor bucket."""
    defs = DEFAULT_DEFS + "\ndeep_score: [AAA]\n"  # AAA anchors bucket_a
    settings = _make_settings(tmp_path, threshold=0.60, definitions_text=defs)

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_company_with_item1(conn, tmp_path, "cik_a", "AAA", "text-for-A")
    conn.close()

    overrides = _orthogonal_overrides()
    overrides["text-for-A"] = np.array([0, 0, 0, 1, 0, 0, 0, 0], dtype=float)

    encoder = FakeEncoder(overrides=overrides)
    shortlist.run(model_factory=lambda name: encoder, settings=settings)

    conn = db.connect(settings.db_path)
    src = {
        r["bucket_id"]: r["source"]
        for r in conn.execute("SELECT bucket_id, source FROM shortlist").fetchall()
    }
    conn.close()

    assert src == {"bucket_a": "anchor", "bucket_b": "deep", "bucket_c": "deep"}


def test_deep_ticker_not_a_candidate_is_skipped(tmp_path: Path, capsys):
    settings = _make_settings(tmp_path, threshold=0.60, definitions_text=DEEP_DEFS)

    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    _seed_company_with_item1(conn, tmp_path, "cik_x", "XXX", "text-for-X")
    conn.close()

    encoder = FakeEncoder()
    result = shortlist.run(model_factory=lambda name: encoder, settings=settings)

    assert result["n_deep_pairs"] == 0
    assert "deep-score ticker DDD" in capsys.readouterr().out


def test_no_candidates_does_not_crash(tmp_path: Path):
    settings = _make_settings(tmp_path)
    conn = db.connect(settings.db_path)
    db.init_schema(conn)
    conn.close()

    # model_factory should never be called if there are zero candidates.
    def _boom(name):
        raise AssertionError("model should not load when no candidates")

    result = shortlist.run(model_factory=_boom, settings=settings)
    assert result["n_pairs"] == 0
    assert result["n_candidates"] == 0
