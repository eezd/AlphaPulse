"""Structured correction and time contracts shared by news stages."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, model_validator

class PackageInput(BaseModel):
    id: int = Field(gt=0)
    revision: int = Field(ge=0)


class Assignment(BaseModel):
    analysis_id: int = Field(gt=0)
    evidence: str = Field(min_length=1, max_length=500)


class Replacement(BaseModel):
    keep_id: int | None
    title: str = Field(min_length=1, max_length=300)
    fact: str = Field(min_length=1)
    category: str = Field(min_length=1, max_length=64)
    tags: list[str] = Field(min_length=1, max_length=20)
    content_kind: Literal['report', 'statement', 'data', 'opinion', 'explanation', 'other']
    assignments: list[Assignment] = Field(min_length=1)


class Correction(BaseModel):
    packages: list[PackageInput] = Field(min_length=1)
    replacements: list[Replacement] = Field(min_length=1)
    reason: str = Field(min_length=1)


class PackageTimePoint(BaseModel):
    occurred_at: datetime | None
    time_precision: Literal['minute', 'day', 'month', 'unknown']
    time_text: str
    reported_at: datetime | None
    description: str = Field(min_length=1)
    verification: Literal['supported', 'partial', 'conflicting', 'insufficient', 'refuted']
    evidence_refs: list[dict] = Field(min_length=1)

    @model_validator(mode='after')
    def time_consistency(self):
        if (self.occurred_at is None) != (self.time_precision == 'unknown'):
            raise ValueError('发生时间与精度不一致')
        if self.occurred_at and self.occurred_at.tzinfo is None:
            raise ValueError('发生时间必须有时区')
        if self.reported_at and self.reported_at.tzinfo is None:
            raise ValueError('报道时间必须有时区')
        return self


