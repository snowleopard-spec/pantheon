"""Stage 7 — freeze weighted index compositions.

Reads the `latest_scores` view (mean score across runs 1-2 for the newest
(fy, prompt_version, model_version) per (cik, bucket_id)), applies a score
floor, joins to `companies.market_cap` and bucket display names from the
YAML, and writes:

    outputs/<asof>/minidex_weights.csv
    outputs/<asof>/minidex_weights.parquet
    outputs/<asof>/manifest.json

Per bucket, three weight schemes are produced:
    weight_cap_score = normalise(score * market_cap)
    weight_equal     = 1 / n
    weight_score     = normalise(score)

Missing market_cap is treated as 0 for weight_cap_score; if *every* member
of a bucket is missing market_cap, we fall back to equal weights for that
bucket's cap-score column and print a warning.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml

from minidex import config, db

# Column order for CSV / parquet, per spec §7 stage 7.
_OUTPUT_COLUMNS = [
    "bucket_id",
    "bucket_name",
    "ticker",
    "cik",
    "score",
    "confidence",
    "market_cap",
    "weight_cap_score",
    "weight_equal",
    "weight_score",
    "rationale_run1",
    "rationale_run2",
    "fy",
    "prompt_version",
    "model_version",
]


def _load_bucket_names(definitions_path: Path) -> dict[str, str]:
    data = yaml.safe_load(definitions_path.read_text(encoding="utf-8"))
    return {b["id"]: b.get("name", b["id"]) for b in data["buckets"]}


def _load_anchor_pairs(definitions_path: Path) -> set[tuple[str, str]]:
    """(ticker.upper(), bucket_id) for every anchor in the YAML."""
    data = yaml.safe_load(definitions_path.read_text(encoding="utf-8"))
    out: set[tuple[str, str]] = set()
    for b in data.get("buckets", []) or []:
        bid = b["id"]
        for t in (b.get("anchors") or []):
            out.add((str(t).strip().upper(), bid))
    return out


def _load_scored(
    conn: sqlite3.Connection,
    score_floor: float,
    anchor_pairs: set[tuple[str, str]] | None = None,
) -> pd.DataFrame:
    """Read latest_scores >= floor joined to companies.market_cap.

    Anchor pairs are ALWAYS included regardless of score, so a below-floor
    anchor still lands in its bucket for weight-floor treatment downstream.
    Adds an `is_anchor` bool column.
    """
    sql = """
    SELECT
      ls.cik            AS cik,
      ls.bucket_id      AS bucket_id,
      COALESCE(ls.ticker, c.ticker) AS ticker,
      ls.fy             AS fy,
      ls.prompt_version AS prompt_version,
      ls.model_version  AS model_version,
      ls.score          AS score,
      ls.confidence     AS confidence,
      ls.rationale_run1 AS rationale_run1,
      ls.rationale_run2 AS rationale_run2,
      c.market_cap      AS market_cap
    FROM latest_scores ls
    LEFT JOIN companies c ON c.cik = ls.cik
    """
    df = pd.read_sql_query(sql, conn)
    pairs = anchor_pairs or set()
    df["is_anchor"] = df.apply(
        lambda r: (str(r["ticker"] or "").upper(), r["bucket_id"]) in pairs,
        axis=1,
    )
    keep = (df["score"] >= score_floor) | df["is_anchor"]
    return df[keep].reset_index(drop=True)


def _modal_or_mixed(values: pd.Series) -> str:
    """Return the single unique non-null value; 'mixed' if >1; '' if none."""
    uniq = [v for v in values.dropna().unique().tolist() if v not in ("", None)]
    if not uniq:
        return ""
    if len(uniq) == 1:
        return str(uniq[0])
    return "mixed"


def _apply_anchor_floor(
    weights: pd.Series, is_anchor: pd.Series, min_weight: float
) -> pd.Series:
    """Hard-fix below-floor anchors at min_weight; scale the rest to fit.

    Preserves the invariant that weights sum to 1.0. Only anchors whose
    pre-floor weight is strictly below ``min_weight`` are fixed at exactly
    ``min_weight``. All other members (above-floor anchors AND non-anchors)
    are scaled proportionally so the total equals 1.0.

    If the fixed portion alone would exceed or equal 1.0 (over-constrained,
    e.g. too many below-floor anchors), distribute 1.0 equally across the
    fixed anchors and zero everyone else, and print a warning.
    """
    if min_weight <= 0 or not is_anchor.any():
        return weights

    out = weights.copy().astype(float)

    # Step 1: identify below-floor anchors (these get hard-fixed at the floor).
    below_floor_anchors = is_anchor & (out < min_weight)
    others = ~below_floor_anchors

    # Step 2: reserve the fixed portion.
    n_fixed = int(below_floor_anchors.sum())
    reserved = n_fixed * min_weight

    if reserved >= 1.0:
        # Over-constrained: fixed anchors alone consume >= the whole bucket.
        # Distribute 1.0 equally across the fixed anchors; zero everyone else.
        print(
            f"WARNING: anchor floor {min_weight} across {n_fixed} below-floor "
            f"anchors reserves {reserved:.4f} >= 1.0; distributing equally "
            "across those anchors and zeroing all other members."
        )
        out.loc[others] = 0.0
        out.loc[below_floor_anchors] = 1.0 / n_fixed
        return out

    # Step 3: hard-fix below-floor anchors at exactly the floor.
    out.loc[below_floor_anchors] = min_weight

    # Step 4: scale "everyone else" proportionally into the remaining pool.
    remaining_pool = 1.0 - reserved
    others_sum = out.loc[others].sum()
    if others_sum > 0:
        out.loc[others] = out.loc[others] * (remaining_pool / others_sum)
    return out


def _compute_weights(df: pd.DataFrame, anchor_min_weight: float = 0.0) -> pd.DataFrame:
    """Add weight_cap_score, weight_equal, weight_score columns.

    Assumes df is grouped-in-place by bucket_id. Missing market_cap is
    treated as 0 for the cap-score weight. If a bucket has zero total
    cap*score (e.g. all market caps missing), it falls back to equal
    weights and a warning is printed.

    When ``anchor_min_weight`` > 0 and the df has an ``is_anchor`` column,
    weight_cap_score is floored per anchor and non-anchor members are
    renormalised so the bucket still sums to 1.0.
    """
    df = df.copy()
    # Fill missing market cap with 0 for weight math; keep original NaN in
    # the output column via a saved copy.
    original_market_cap = df["market_cap"].copy()
    market_cap_num = pd.to_numeric(df["market_cap"], errors="coerce").fillna(0.0)
    score_num = pd.to_numeric(df["score"], errors="coerce").fillna(0.0)

    df["_cap_x_score"] = market_cap_num * score_num
    df["_score_num"] = score_num

    parts: list[pd.DataFrame] = []
    for bucket_id, g in df.groupby("bucket_id", sort=False):
        n = len(g)
        g = g.copy()
        # weight_equal
        g["weight_equal"] = 1.0 / n if n else 0.0

        # weight_score
        s_sum = g["_score_num"].sum()
        if s_sum > 0:
            g["weight_score"] = g["_score_num"] / s_sum
        else:
            g["weight_score"] = 1.0 / n if n else 0.0

        # weight_cap_score
        cs_sum = g["_cap_x_score"].sum()
        if cs_sum > 0:
            g["weight_cap_score"] = g["_cap_x_score"] / cs_sum
        else:
            print(
                f"WARNING: bucket '{bucket_id}' has no positive market_cap*score "
                "(all members missing market cap?); falling back to equal weights "
                "for weight_cap_score."
            )
            g["weight_cap_score"] = 1.0 / n if n else 0.0

        # Apply anchor-weight floor to weight_cap_score if requested.
        if anchor_min_weight > 0 and "is_anchor" in g.columns and g["is_anchor"].any():
            g["weight_cap_score"] = _apply_anchor_floor(
                g["weight_cap_score"], g["is_anchor"], anchor_min_weight
            )

        parts.append(g)

    out = pd.concat(parts, axis=0) if parts else df
    out = out.drop(columns=["_cap_x_score", "_score_num"])
    # Restore original market_cap (with NaNs) in the output.
    out["market_cap"] = original_market_cap.reindex(out.index)
    return out


def _filing_fy_histogram(conn: sqlite3.Connection, ciks: list[str]) -> dict[str, int]:
    if not ciks:
        return {}
    placeholders = ",".join("?" * len(ciks))
    rows = conn.execute(
        f"SELECT fy FROM filings WHERE cik IN ({placeholders}) AND fy IS NOT NULL",
        ciks,
    ).fetchall()
    counts: Counter = Counter()
    for r in rows:
        fy = r[0]
        if fy is None:
            continue
        counts[str(int(fy))] += 1
    return dict(sorted(counts.items()))


def _write_manifest(
    path: Path,
    *,
    asof: str,
    df: pd.DataFrame,
    settings: Any,
    filing_fy_histogram: dict[str, int],
) -> dict[str, Any]:
    run_date = datetime.now(timezone.utc).date().isoformat()
    manifest = {
        "run_date": run_date,
        "asof": asof,
        "n_companies_scored": int(df["cik"].nunique()) if not df.empty else 0,
        "n_pairs_scored": int(len(df)),
        "prompt_version": _modal_or_mixed(df["prompt_version"]) if not df.empty else "",
        "model_version": _modal_or_mixed(df["model_version"]) if not df.empty else "",
        "embedding_model": settings.embedding_model,
        "similarity_threshold": float(settings.similarity_threshold),
        "score_floor": float(settings.score_floor),
        "anchor_min_weight": float(getattr(settings, "anchor_min_weight", 0.0)),
        "definitions_sha256": settings.file_sha256(settings.definitions_path),
        "prompt_sha256": settings.file_sha256(settings.prompt_path),
        "filing_fy_histogram": filing_fy_histogram,
    }
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def build(asof: str, *, settings: Any = None) -> dict[str, Any]:
    """Build the frozen index composition for a given as-of date.

    Returns a summary dict for callers/tests. Writes CSV, parquet and
    manifest into outputs/<asof>/.
    """
    s = settings if settings is not None else config.get_settings()
    bucket_names = _load_bucket_names(s.definitions_path)
    anchor_pairs = _load_anchor_pairs(s.definitions_path)
    anchor_min_weight = float(getattr(s, "anchor_min_weight", 0.0))

    out_dir = s.outputs_dir / asof
    out_dir.mkdir(parents=True, exist_ok=True)

    conn = db.connect(s.db_path)
    try:
        df = _load_scored(conn, s.score_floor, anchor_pairs=anchor_pairs)
        if df.empty:
            print(
                f"build: no scores >= floor {s.score_floor}; writing empty outputs "
                f"to {out_dir}"
            )
            empty = pd.DataFrame({c: pd.Series(dtype="object") for c in _OUTPUT_COLUMNS})
            _write_outputs(empty, out_dir)
            manifest = _write_manifest(
                out_dir / "manifest.json",
                asof=asof,
                df=empty,
                settings=s,
                filing_fy_histogram={},
            )
            return {
                "asof": asof,
                "n_rows": 0,
                "n_buckets": 0,
                "manifest": manifest,
                "output_dir": out_dir,
            }

        df = _compute_weights(df, anchor_min_weight=anchor_min_weight)
        df["bucket_name"] = df["bucket_id"].map(bucket_names).fillna(df["bucket_id"])

        # Reorder to spec.
        df = df.reindex(columns=_OUTPUT_COLUMNS)
        # Deterministic ordering.
        df = df.sort_values(
            ["bucket_id", "weight_cap_score", "ticker"],
            ascending=[True, False, True],
            kind="mergesort",
        ).reset_index(drop=True)

        _write_outputs(df, out_dir)

        # Weight-sum sanity check.
        for wcol in ("weight_cap_score", "weight_equal", "weight_score"):
            sums = df.groupby("bucket_id")[wcol].sum()
            bad = sums[(sums - 1.0).abs() > 1e-6]
            if not bad.empty:
                raise RuntimeError(
                    f"{wcol} does not sum to 1.0 in buckets: {bad.to_dict()}"
                )

        # Bucket membership warning (all 22 buckets should have >= 2 members).
        counts_per_bucket = df.groupby("bucket_id").size()
        expected_buckets = set(bucket_names.keys())
        missing = expected_buckets - set(counts_per_bucket.index)
        for b in sorted(missing):
            print(f"WARNING: bucket '{b}' has 0 members at score floor {s.score_floor}.")
        for b, n in counts_per_bucket.items():
            if n < 2:
                print(
                    f"WARNING: bucket '{b}' has only {n} member(s) at score floor "
                    f"{s.score_floor}; expected >= 2."
                )

        histogram = _filing_fy_histogram(conn, df["cik"].dropna().unique().tolist())
        manifest = _write_manifest(
            out_dir / "manifest.json",
            asof=asof,
            df=df,
            settings=s,
            filing_fy_histogram=histogram,
        )

        print(
            f"build: wrote {len(df)} rows across {counts_per_bucket.size} buckets to "
            f"{out_dir}"
        )
        return {
            "asof": asof,
            "n_rows": int(len(df)),
            "n_buckets": int(counts_per_bucket.size),
            "manifest": manifest,
            "output_dir": out_dir,
        }
    finally:
        conn.close()


def _write_outputs(df: pd.DataFrame, out_dir: Path) -> None:
    """Write CSV + parquet with the exact column ordering preserved."""
    # Ensure column order (parquet preserves column order via pyarrow).
    df = df.reindex(columns=_OUTPUT_COLUMNS)
    df.to_csv(out_dir / "minidex_weights.csv", index=False)
    df.to_parquet(out_dir / "minidex_weights.parquet", index=False, engine="pyarrow")
