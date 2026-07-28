from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import yaml

from minidex import config, db


def load_bucket_defs(definitions_path: Path) -> list[dict[str, Any]]:
    """Read the mini-dex bucket definitions YAML."""
    data = yaml.safe_load(definitions_path.read_text(encoding="utf-8"))
    return list(data["buckets"])


def bucket_embed_text(bucket: dict[str, Any]) -> str:
    """definition + ' ' + joined includes list."""
    definition = (bucket.get("definition") or "").strip()
    includes = bucket.get("includes") or []
    if isinstance(includes, list):
        includes_text = ", ".join(str(x) for x in includes)
    else:
        includes_text = str(includes)
    return f"{definition} {includes_text}".strip()


def load_candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return candidate companies that have at least one filing row with an item1_path.

    We take the most recent filing per cik (max filed_date, then max accession).
    """
    rows = conn.execute(
        """
        SELECT c.cik, c.ticker, f.accession, f.item1_path
        FROM companies c
        JOIN filings f ON f.cik = c.cik
        WHERE c.is_candidate = 1
          AND f.item1_path IS NOT NULL
          AND f.item1_path != ''
        """
    ).fetchall()

    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        cik = r["cik"]
        row = {
            "cik": cik,
            "ticker": r["ticker"],
            "accession": r["accession"],
            "item1_path": r["item1_path"],
        }
        existing = latest.get(cik)
        if existing is None or row["accession"] > existing["accession"]:
            latest[cik] = row
    return list(latest.values())


def _read_item1(path: str | Path) -> str:
    p = Path(path)
    if not p.is_absolute():
        # try relative to repo root
        candidate = config.get_settings().repo_root / p
        if candidate.exists():
            p = candidate
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return ""


def _truncate_by_tokens(model: Any, text: str) -> str:
    """Truncate text to the model's tokenizer max_seq_length worth of tokens."""
    tokenizer = getattr(model, "tokenizer", None)
    max_len = getattr(model, "max_seq_length", None)
    if tokenizer is None or not max_len:
        return text
    try:
        ids = tokenizer.encode(text, add_special_tokens=False)
    except Exception:
        return text
    if len(ids) <= max_len:
        return text
    truncated_ids = ids[:max_len]
    try:
        return tokenizer.decode(truncated_ids, skip_special_tokens=True)
    except Exception:
        return text


def _load_model(name: str) -> Any:
    st = config.require_scoring("sentence_transformers")  # local import for testability

    return st.SentenceTransformer(name)


def compute_pairs(
    similarity: np.ndarray,
    candidate_ciks: list[str],
    bucket_ids: list[str],
    threshold: float,
) -> list[tuple[str, str, float]]:
    """Return list of (cik, bucket_id, similarity) where similarity > threshold."""
    pairs: list[tuple[str, str, float]] = []
    if similarity.size == 0:
        return pairs
    above = np.argwhere(similarity > threshold)
    for i, j in above:
        pairs.append((candidate_ciks[int(i)], bucket_ids[int(j)], float(similarity[i, j])))
    return pairs


def _anchor_map(buckets: Iterable[dict[str, Any]]) -> dict[str, list[str]]:
    """ticker (upper) -> list of bucket_ids where it is an anchor."""
    m: dict[str, list[str]] = {}
    for b in buckets:
        bid = b["id"]
        for t in (b.get("anchors") or []):
            m.setdefault(str(t).upper(), []).append(bid)
    return m


def run(
    *,
    model_factory=_load_model,
    settings=None,
) -> dict[str, Any]:
    """Stage 4 — build the shortlist table.

    Returns a summary dict (useful for tests / callers).
    """
    s = settings if settings is not None else config.get_settings()

    buckets = load_bucket_defs(s.definitions_path)
    bucket_ids = [b["id"] for b in buckets]
    bucket_texts = [bucket_embed_text(b) for b in buckets]
    anchors_by_ticker = _anchor_map(buckets)

    conn = db.connect(s.db_path)
    try:
        candidates = load_candidates(conn)
        if not candidates:
            print("shortlist: no candidates with filings found; nothing to do.")
            return {
                "n_candidates": 0,
                "n_buckets": len(buckets),
                "n_pairs": 0,
                "anchor_min_similarity": None,
            }

        model = model_factory(s.embedding_model)

        item1_texts: list[str] = []
        for cand in candidates:
            raw = _read_item1(cand["item1_path"])
            item1_texts.append(_truncate_by_tokens(model, raw))

        candidate_vecs = np.asarray(
            model.encode(item1_texts, normalize_embeddings=True)
        )
        bucket_vecs = np.asarray(
            model.encode(bucket_texts, normalize_embeddings=True)
        )

        # cosine sim (vectors are unit-normalised): dot product
        sim = candidate_vecs @ bucket_vecs.T  # (n_candidates, n_buckets)

        candidate_ciks = [c["cik"] for c in candidates]
        embed_pairs = compute_pairs(sim, candidate_ciks, bucket_ids, s.similarity_threshold)

        # write embed pairs first
        pair_sim: dict[tuple[str, str], float] = {}
        with db.transaction(conn):
            for cik, bucket_id, score in embed_pairs:
                db.upsert_shortlist(
                    conn, cik=cik, bucket_id=bucket_id, similarity=score, source="embed"
                )
                pair_sim[(cik, bucket_id)] = score

        # Force anchor pairs (source='anchor'). Anchor source always wins;
        # similarity comes from the computed sim matrix if available.
        cik_by_ticker = {c["ticker"].upper(): c["cik"] for c in candidates}
        idx_by_cik = {c: i for i, c in enumerate(candidate_ciks)}
        bucket_idx = {bid: i for i, bid in enumerate(bucket_ids)}

        anchor_sims: list[float] = []
        anchor_pairs_written = 0
        with db.transaction(conn):
            for ticker, buckets_for_ticker in anchors_by_ticker.items():
                cik = cik_by_ticker.get(ticker)
                if cik is None:
                    # ticker isn't a candidate w/ a filing — can't force it here
                    continue
                i = idx_by_cik[cik]
                for bid in buckets_for_ticker:
                    j = bucket_idx.get(bid)
                    if j is None:
                        continue
                    computed = float(sim[i, j])
                    db.upsert_shortlist(
                        conn,
                        cik=cik,
                        bucket_id=bid,
                        similarity=computed,
                        source="anchor",
                    )
                    pair_sim[(cik, bid)] = computed
                    anchor_sims.append(computed)
                    anchor_pairs_written += 1

        total_pairs = conn.execute("SELECT COUNT(*) FROM shortlist").fetchone()[0]

        # Median shortlisted buckets per candidate that has any pair.
        per_company_counts = [
            r[0]
            for r in conn.execute(
                "SELECT COUNT(*) FROM shortlist GROUP BY cik"
            ).fetchall()
        ]
        median_per_company = (
            float(np.median(per_company_counts)) if per_company_counts else 0.0
        )

        anchor_min = min(anchor_sims) if anchor_sims else None

        print(
            f"shortlist: candidates={len(candidates)} buckets={len(buckets)} "
            f"embed_pairs={len(embed_pairs)} anchor_pairs={anchor_pairs_written} "
            f"total_pairs={total_pairs} median_per_company={median_per_company:.1f} "
            f"threshold={s.similarity_threshold:.3f}"
        )
        if anchor_min is not None:
            print(f"shortlist: min anchor similarity = {anchor_min:.4f}")
            if anchor_min < s.similarity_threshold:
                suggested = max(0.0, anchor_min - 0.02)
                print(
                    "WARNING: at least one anchor pair is below the similarity "
                    f"threshold ({anchor_min:.4f} < {s.similarity_threshold:.3f}). "
                    f"Consider lowering MINIDEX_SIM_THRESHOLD to ~{suggested:.2f}."
                )

        return {
            "n_candidates": len(candidates),
            "n_buckets": len(buckets),
            "n_pairs": total_pairs,
            "n_embed_pairs": len(embed_pairs),
            "n_anchor_pairs": anchor_pairs_written,
            "anchor_min_similarity": anchor_min,
            "median_per_company": median_per_company,
        }
    finally:
        conn.close()
