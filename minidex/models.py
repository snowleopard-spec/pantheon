from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Confidence = Literal["high", "medium", "low"]
EvidenceType = Literal["segment_data", "description_only"]


class BucketScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_id: str
    score: float = Field(ge=0.0, le=1.0)
    confidence: Confidence
    rationale: str
    evidence_type: EvidenceType


class ScoringResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticker: str
    fy: int
    pre_revenue: bool = False
    scores: list[BucketScore]

    @field_validator("scores")
    @classmethod
    def non_empty(cls, v: list[BucketScore]) -> list[BucketScore]:
        if not v:
            raise ValueError("scores must be non-empty")
        return v
