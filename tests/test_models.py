from __future__ import annotations

import pytest
from pydantic import ValidationError

from minidex.models import ScoringResponse


VALID = {
    "ticker": "NVDA",
    "fy": 2024,
    "pre_revenue": False,
    "scores": [
        {
            "bucket_id": "fabless_chip_design",
            "score": 0.9,
            "confidence": "high",
            "rationale": "GPU sales dominate revenue.",
            "evidence_type": "segment_data",
        }
    ],
}


def test_valid_response_parses():
    r = ScoringResponse.model_validate(VALID)
    assert r.ticker == "NVDA"
    assert r.scores[0].score == 0.9


def test_score_out_of_range_rejected():
    bad = {**VALID, "scores": [{**VALID["scores"][0], "score": 1.5}]}
    with pytest.raises(ValidationError):
        ScoringResponse.model_validate(bad)


def test_extra_fields_rejected():
    bad = {**VALID, "extra_field": "nope"}
    with pytest.raises(ValidationError):
        ScoringResponse.model_validate(bad)


def test_bad_confidence_rejected():
    bad = {**VALID, "scores": [{**VALID["scores"][0], "confidence": "medium-ish"}]}
    with pytest.raises(ValidationError):
        ScoringResponse.model_validate(bad)


def test_empty_scores_rejected():
    bad = {**VALID, "scores": []}
    with pytest.raises(ValidationError):
        ScoringResponse.model_validate(bad)
