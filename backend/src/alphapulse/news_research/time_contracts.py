"""Original occurrence, plan and reporting time contracts."""
from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field, model_validator


class PackageTimePoint(BaseModel):
    occurred_at: datetime | None = Field(description="发生时间；未知必须为null且time_precision=unknown，已知必须带时区，不用报道时间代替")
    time_precision: Literal['minute', 'day', 'month', 'unknown'] = Field(description='occurred_at为null时必须unknown；非null时按原文精度选择minute/day/month')
    planned_at: datetime | None = Field(default=None, description='计划发生时间，不证明已发生')
    planned_precision: Literal['minute', 'day', 'month', 'unknown'] = 'unknown'
    period_text: str = Field(default='', description='数据统计期间原文')
    time_text: str
    reported_at: datetime | None
    description: str = Field(min_length=1)
    verification: Literal['supported', 'partial', 'conflicting', 'insufficient', 'refuted']
    evidence_refs: list[dict] = Field(min_length=1)

    @model_validator(mode='after')
    def time_consistency(self):
        if (self.planned_at is None) != (self.planned_precision == 'unknown'):
            raise ValueError('计划时间与精度不一致')
        if self.planned_at and self.planned_at.tzinfo is None:
            raise ValueError('计划时间必须有时区')
        if (self.occurred_at is None) != (self.time_precision == 'unknown'):
            raise ValueError('发生时间与精度不一致')
        if self.occurred_at and self.occurred_at.tzinfo is None:
            raise ValueError('发生时间必须有时区')
        if self.reported_at and self.reported_at.tzinfo is None:
            raise ValueError('报道时间必须有时区')
        return self
