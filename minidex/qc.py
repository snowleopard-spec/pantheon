"""Stage 6 — quality-control report.

Reads scores from the SQLite DB and writes a human-readable
``outputs/qc_report.md`` covering:

  1. Anchor check (mean score per (anchor ticker, anchor bucket) >= 0.3)
  2. Run disagreement (|run1 - run2| > 0.2)
  3. Borderline rows (mean score in [0.10, 0.30])
  4. Low-confidence highs (confidence == 'low' and mean score >= 0.3)
  5. Summary counts per bucket at the score floor (0.10)

The report is intended for human review; formatting is small Markdown tables.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from minidex import config, db


BORDERLINE_LO = 0.10
BORDERLINE_HI = 0.30
LOW_CONF_HIGH_THRESHOLD = 0.3
RUN_DISAGREEMENT_THRESHOLD = 0.2
ANCHOR_MIN_SCORE = 0.3
BUCKET_MEMBER_FLOOR = 0.10


# ---------------------------------------------------------------------------
# Definitions helpers
# ---------------------------------------------------------------------------


def _load_buckets(definitions_path: Path) -> list[dict[str, Any]]:
    data = yaml.safe_load(definitions_path.read_text(encoding="utf-8"))
    return list(data.get("buckets") or [])


def _anchor_pairs(buckets: Iterable[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Return [(ticker, bucket_id, bucket_name), ...] for every anchor."""
    out: list[tuple[str, str, str]] = []
    for b in buckets:
        bid = b["id"]
        name = b.get("name", bid)
        for t in (b.get("anchors") or []):
            out.append((str(t).upper(), bid, name))
    return out


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AnchorResult:
    ticker: str
    bucket_id: str
    bucket_name: str
    mean_score: float | None  # None => not yet scored
    confidence: str | None


def anchor_scores(
    conn: sqlite3.Connection,
    buckets: Iterable[dict[str, Any]],
) -> list[AnchorResult]:
    """For each anchor (ticker, bucket) look up the row in latest_scores.

    Matching is done by ticker rather than cik because the anchors YAML
    only knows tickers.
    """
    results: list[AnchorResult] = []
    for ticker, bucket_id, bucket_name in _anchor_pairs(buckets):
        row = conn.execute(
            """
            SELECT score, confidence
            FROM latest_scores
            WHERE UPPER(ticker) = ? AND bucket_id = ?
            """,
            (ticker, bucket_id),
        ).fetchone()
        if row is None:
            results.append(AnchorResult(ticker, bucket_id, bucket_name, None, None))
        else:
            results.append(
                AnchorResult(
                    ticker=ticker,
                    bucket_id=bucket_id,
                    bucket_name=bucket_name,
                    mean_score=float(row["score"]),
                    confidence=row["confidence"],
                )
            )
    return results


def mean_score_for(
    conn: sqlite3.Connection, ticker: str, bucket_id: str
) -> float | None:
    """Return the mean latest score for a given ticker / bucket, or None."""
    row = conn.execute(
        """
        SELECT score
        FROM latest_scores
        WHERE UPPER(ticker) = ? AND bucket_id = ?
        """,
        (ticker.upper(), bucket_id),
    ).fetchone()
    if row is None:
        return None
    return float(row["score"])


def run_disagreements(
    conn: sqlite3.Connection, threshold: float = RUN_DISAGREEMENT_THRESHOLD
) -> list[dict[str, Any]]:
    """List (ticker, cik, bucket_id, run1, run2, delta) with |run1 - run2| > threshold.

    Uses the newest (fy, prompt_version, model_version) per (cik, bucket_id).
    """
    rows = conn.execute(
        """
        WITH newest AS (
          SELECT cik, bucket_id,
                 MAX(fy || '|' || prompt_version || '|' || model_version) AS key
          FROM scores
          GROUP BY cik, bucket_id
        ),
        picked AS (
          SELECT s.*
          FROM scores s
          JOIN newest n
            ON s.cik = n.cik
           AND s.bucket_id = n.bucket_id
           AND (s.fy || '|' || s.prompt_version || '|' || s.model_version) = n.key
        )
        SELECT
          cik,
          bucket_id,
          MAX(ticker) AS ticker,
          MAX(CASE WHEN run = 1 THEN score END) AS run1,
          MAX(CASE WHEN run = 2 THEN score END) AS run2
        FROM picked
        GROUP BY cik, bucket_id
        HAVING run1 IS NOT NULL
           AND run2 IS NOT NULL
           AND ABS(run1 - run2) > ?
        ORDER BY ABS(run1 - run2) DESC
        """,
        (threshold,),
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "cik": r["cik"],
                "bucket_id": r["bucket_id"],
                "ticker": r["ticker"],
                "run1": float(r["run1"]),
                "run2": float(r["run2"]),
                "delta": abs(float(r["run1"]) - float(r["run2"])),
            }
        )
    return out


def borderline_rows(
    conn: sqlite3.Connection,
    lo: float = BORDERLINE_LO,
    hi: float = BORDERLINE_HI,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, cik, bucket_id, score, confidence
        FROM latest_scores
        WHERE score >= ? AND score <= ?
        ORDER BY score, ticker, bucket_id
        """,
        (lo, hi),
    ).fetchall()
    return [
        {
            "ticker": r["ticker"],
            "cik": r["cik"],
            "bucket_id": r["bucket_id"],
            "score": float(r["score"]),
            "confidence": r["confidence"],
        }
        for r in rows
    ]


def low_confidence_highs(
    conn: sqlite3.Connection, threshold: float = LOW_CONF_HIGH_THRESHOLD
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT ticker, cik, bucket_id, score, confidence
        FROM latest_scores
        WHERE confidence = 'low' AND score >= ?
        ORDER BY score DESC, ticker
        """,
        (threshold,),
    ).fetchall()
    return [
        {
            "ticker": r["ticker"],
            "cik": r["cik"],
            "bucket_id": r["bucket_id"],
            "score": float(r["score"]),
            "confidence": r["confidence"],
        }
        for r in rows
    ]


def bucket_member_counts(
    conn: sqlite3.Connection,
    buckets: Iterable[dict[str, Any]],
    floor: float = BUCKET_MEMBER_FLOOR,
) -> list[tuple[str, str, int]]:
    """Return [(bucket_id, bucket_name, count_of_members_at_or_above_floor), ...]."""
    counts: list[tuple[str, str, int]] = []
    for b in buckets:
        bid = b["id"]
        name = b.get("name", bid)
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM latest_scores WHERE bucket_id = ? AND score >= ?",
            (bid, floor),
        ).fetchone()
        counts.append((bid, name, int(row["n"]) if row else 0))
    return counts


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def _fmt_score(x: float | None) -> str:
    return f"{x:.3f}" if x is not None else "n/a"


def render_report(
    *,
    anchors: list[AnchorResult],
    disagreements: list[dict[str, Any]],
    borderlines: list[dict[str, Any]],
    low_conf_highs: list[dict[str, Any]],
    bucket_counts: list[tuple[str, str, int]],
) -> str:
    lines: list[str] = []
    lines.append("# mini-dex QC report")
    lines.append("")

    # ---- 1. Anchor check ---------------------------------------------------
    lines.append("## 1. Anchor check")
    lines.append("")
    lines.append(
        f"Anchor tickers must score >= {ANCHOR_MIN_SCORE:.2f} on their anchor bucket."
    )
    lines.append("")

    fails: list[AnchorResult] = []
    passes: list[AnchorResult] = []
    skips: list[AnchorResult] = []
    for a in anchors:
        if a.mean_score is None:
            skips.append(a)
        elif a.mean_score < ANCHOR_MIN_SCORE:
            fails.append(a)
        else:
            passes.append(a)

    if fails:
        lines.append("### FAIL")
        lines.append("")
        lines.append("| ticker | bucket | mean_score | confidence |")
        lines.append("|---|---|---|---|")
        for a in fails:
            lines.append(
                f"| FAIL {a.ticker} | {a.bucket_id} | {_fmt_score(a.mean_score)} "
                f"| {a.confidence or ''} |"
            )
        lines.append("")
    else:
        lines.append("### FAIL")
        lines.append("")
        lines.append("(none)")
        lines.append("")

    lines.append(f"### PASS ({len(passes)})")
    lines.append("")
    if passes:
        lines.append("| ticker | bucket | mean_score | confidence |")
        lines.append("|---|---|---|---|")
        for a in passes:
            lines.append(
                f"| PASS {a.ticker} | {a.bucket_id} | {_fmt_score(a.mean_score)} "
                f"| {a.confidence or ''} |"
            )
    else:
        lines.append("(none)")
    lines.append("")

    lines.append(f"### SKIP ({len(skips)}) — not yet scored")
    lines.append("")
    if skips:
        for a in skips:
            lines.append(f"- SKIP {a.ticker} / {a.bucket_id}")
    else:
        lines.append("(none)")
    lines.append("")

    # ---- 2. Run disagreement ----------------------------------------------
    lines.append("## 2. Run disagreement")
    lines.append("")
    lines.append(
        f"Pairs where |run1 - run2| > {RUN_DISAGREEMENT_THRESHOLD:.2f}."
    )
    lines.append("")
    if disagreements:
        lines.append("| ticker | bucket | run1 | run2 | |delta| |")
        lines.append("|---|---|---|---|---|")
        for d in disagreements:
            lines.append(
                f"| {d['ticker']} | {d['bucket_id']} | "
                f"{d['run1']:.3f} | {d['run2']:.3f} | {d['delta']:.3f} |"
            )
    else:
        lines.append("(none)")
    lines.append("")

    # ---- 3. Borderline -----------------------------------------------------
    lines.append("## 3. Borderline")
    lines.append("")
    lines.append(f"Mean score in [{BORDERLINE_LO:.2f}, {BORDERLINE_HI:.2f}].")
    lines.append("")
    if borderlines:
        lines.append("| ticker | bucket | mean_score | confidence |")
        lines.append("|---|---|---|---|")
        for b in borderlines:
            lines.append(
                f"| {b['ticker']} | {b['bucket_id']} | "
                f"{b['score']:.3f} | {b['confidence']} |"
            )
    else:
        lines.append("(none)")
    lines.append("")

    # ---- 4. Low-confidence highs ------------------------------------------
    lines.append("## 4. Low-confidence highs")
    lines.append("")
    lines.append(
        f"Rows where confidence == 'low' AND mean score >= {LOW_CONF_HIGH_THRESHOLD:.2f}."
    )
    lines.append("")
    if low_conf_highs:
        lines.append("| ticker | bucket | mean_score | confidence |")
        lines.append("|---|---|---|---|")
        for r in low_conf_highs:
            lines.append(
                f"| {r['ticker']} | {r['bucket_id']} | "
                f"{r['score']:.3f} | {r['confidence']} |"
            )
    else:
        lines.append("(none)")
    lines.append("")

    # ---- 5. Bucket summary -------------------------------------------------
    lines.append("## 5. Bucket member counts")
    lines.append("")
    lines.append(f"Members with mean score >= {BUCKET_MEMBER_FLOOR:.2f}.")
    lines.append("")
    lines.append("| bucket_id | bucket_name | members |")
    lines.append("|---|---|---|")
    for bid, name, n in bucket_counts:
        lines.append(f"| {bid} | {name} | {n} |")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run(*, settings=None) -> Path:
    """Generate outputs/qc_report.md. Returns the written path."""
    s = settings if settings is not None else config.get_settings()
    buckets = _load_buckets(s.definitions_path)

    conn = db.connect(s.db_path)
    try:
        db.init_schema(conn)
        anchors = anchor_scores(conn, buckets)
        disagreements = run_disagreements(conn)
        borderlines = borderline_rows(conn)
        lch = low_confidence_highs(conn)
        counts = bucket_member_counts(conn, buckets)
    finally:
        conn.close()

    report = render_report(
        anchors=anchors,
        disagreements=disagreements,
        borderlines=borderlines,
        low_conf_highs=lch,
        bucket_counts=counts,
    )

    s.outputs_dir.mkdir(parents=True, exist_ok=True)
    out_path = s.outputs_dir / "qc_report.md"
    out_path.write_text(report, encoding="utf-8")

    # small stdout summary
    fails = [a for a in anchors if a.mean_score is not None and a.mean_score < ANCHOR_MIN_SCORE]
    skips = [a for a in anchors if a.mean_score is None]
    print(
        f"qc: wrote {out_path} "
        f"anchors_fail={len(fails)} anchors_skip={len(skips)} "
        f"disagreements={len(disagreements)} borderline={len(borderlines)} "
        f"low_conf_highs={len(lch)}"
    )
    return out_path
