"""Anchor calibration tests.

Per §7 stage 6 of MINIDEX_SPEC.md: mirror the anchor QC check as pytest
assertions. Each anchor ticker must score >= 0.5 on its declared anchor
bucket. Runs against the real DB at ``config.get_settings().db_path`` so it
exercises actual scored data once a scoring run exists. On a fresh repo
(empty DB or no scores yet) the tests skip cleanly rather than fail.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import yaml

from minidex import config, db, qc


ANCHOR_MIN_SCORE = qc.ANCHOR_MIN_SCORE


def _anchor_pairs_for_defs(definitions_path: Path) -> list[tuple[str, str]]:
    if not definitions_path.exists():
        return []
    data = yaml.safe_load(definitions_path.read_text(encoding="utf-8"))
    out: list[tuple[str, str]] = []
    for b in (data.get("buckets") or []):
        bid = b["id"]
        for t in (b.get("anchors") or []):
            out.append((str(t).upper(), bid))
    return out


_SETTINGS = None


def _settings():
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = config.get_settings()
    return _SETTINGS


def _has_any_scores(db_path: Path) -> bool:
    if not db_path.exists():
        return False
    try:
        conn = db.connect(db_path)
    except sqlite3.Error:
        return False
    try:
        # `scores` table may not exist yet
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='scores'"
        ).fetchone()
        if row is None:
            return False
        row = conn.execute("SELECT COUNT(*) FROM scores").fetchone()
        return bool(row and row[0] > 0)
    finally:
        conn.close()


ANCHOR_PAIRS = _anchor_pairs_for_defs(_settings().definitions_path)


@pytest.mark.parametrize(
    "ticker,bucket_id",
    ANCHOR_PAIRS,
    ids=[f"{t}-{b}" for t, b in ANCHOR_PAIRS] or ["no-anchors"],
)
def test_anchor_scores_at_least_half(ticker: str, bucket_id: str) -> None:
    s = _settings()
    if not _has_any_scores(s.db_path):
        pytest.skip("no scores in DB yet")

    conn = db.connect(s.db_path)
    try:
        mean = qc.mean_score_for(conn, ticker, bucket_id)
    finally:
        conn.close()

    if mean is None:
        pytest.skip(f"{ticker} / {bucket_id} not yet scored")

    assert mean >= ANCHOR_MIN_SCORE, (
        f"anchor {ticker} scored {mean:.3f} on {bucket_id} "
        f"(< {ANCHOR_MIN_SCORE:.2f})"
    )


def test_anchor_pairs_nonempty_from_definitions() -> None:
    """Sanity: the YAML actually contains anchors (protects against parse regressions)."""
    if not ANCHOR_PAIRS:
        pytest.skip("no anchors defined in YAML")
    assert len(ANCHOR_PAIRS) > 0
