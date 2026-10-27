import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from .rules import DEFAULT_BANDS, DEFAULT_CRITERIA


def _clean(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("This cannot be empty")
    return v


class CriterionIn(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,30}$")
    name: str = Field(min_length=1, max_length=120)
    weight: int = Field(ge=1, le=100)


class BandIn(BaseModel):
    min: int = Field(ge=0, le=100)
    label: str = Field(min_length=1, max_length=40)


class CycleIn(BaseModel):
    name: str = Field(min_length=3, max_length=120)
    year: int = Field(ge=2000, le=2100)
    start_date: date
    end_date: date
    self_deadline: date
    criteria: list[CriterionIn] | None = Field(default=None, max_length=12, description="Defaults to the placeholder form")
    rating_bands: list[BandIn] | None = Field(default=None, max_length=8)

    @model_validator(mode="after")
    def _check(self):
        if not (self.start_date <= self.self_deadline <= self.end_date):
            raise ValueError("Dates must satisfy start <= self-assessment deadline <= end")
        if self.criteria is not None:
            if not self.criteria:
                raise ValueError("A cycle needs at least one criterion")
            if len({c.key for c in self.criteria}) != len(self.criteria):
                raise ValueError("Criterion keys must be unique")
            if sum(c.weight for c in self.criteria) != 100:
                raise ValueError("Criterion weights must add up to 100")
        if self.rating_bands is not None:
            mins = [b.min for b in self.rating_bands]
            if not mins or mins != sorted(set(mins), reverse=True) or mins[-1] != 0:
                raise ValueError("Rating bands must go from highest to lowest minimum, without repeats, ending at 0")
        return self

    def criteria_json(self) -> list[dict]:
        return [c.model_dump() for c in self.criteria] if self.criteria else [dict(c) for c in DEFAULT_CRITERIA]

    def bands_json(self) -> list[dict]:
        return [b.model_dump() for b in self.rating_bands] if self.rating_bands else [dict(b) for b in DEFAULT_BANDS]


class CycleOut(BaseModel):
    id: uuid.UUID
    name: str
    year: int
    status: str
    start_date: date
    end_date: date
    self_deadline: date
    criteria: list[dict]
    rating_bands: list[dict]
    appraisals: int
    finalised: int
    opened_at: datetime | None
    closed_at: datetime | None


class ScoresIn(BaseModel):
    scores: dict[str, int]
    comment: str = Field(min_length=1, max_length=3000)
    _c = field_validator("comment")(_clean)


class FinaliseIn(BaseModel):
    comment: str | None = Field(default=None, max_length=3000)


class ReturnIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    _c = field_validator("reason")(_clean)


class AcknowledgeIn(BaseModel):
    comment: str | None = Field(default=None, max_length=2000)


class AppraiserIn(BaseModel):
    user_id: uuid.UUID


class CriterionScore(BaseModel):
    key: str
    name: str
    weight: int
    self_score: int | None
    appraiser_score: int | None


class AppraisalOut(BaseModel):
    id: uuid.UUID
    cycle_id: uuid.UUID
    cycle: str
    year: int
    cycle_status: str
    self_deadline: date
    staff_id: uuid.UUID
    file_number: str
    staff_name: str
    department: str
    appraiser: str | None
    status: str
    criteria: list[CriterionScore]
    self_comment: str | None
    self_submitted_at: datetime | None
    appraiser_comment: str | None   # hidden from the person appraised until finalised
    score: float | None             # hidden from the person appraised until finalised
    rating: str | None              # hidden from the person appraised until finalised
    reviewed_at: datetime | None
    finalised_at: datetime | None
    finalise_comment: str | None
    return_reason: str | None       # never shown to the person appraised
    acknowledged_at: datetime | None
    staff_comment: str | None
    can_self_assess: bool
    can_review: bool
    can_finalise: bool
    can_acknowledge: bool


class AppraisalPage(BaseModel):
    items: list[AppraisalOut]
    total: int
    page: int
    size: int


class HistoryRow(BaseModel):
    cycle_id: uuid.UUID
    cycle: str
    year: int
    score: float
    rating: str
    finalised_at: datetime


class StatusCount(BaseModel):
    status: str
    count: int


class DepartmentAverage(BaseModel):
    department: str
    appraisals: int
    finalised: int
    average: float | None


class PerformanceSummary(BaseModel):
    cycle: dict | None
    by_status: list[StatusCount]
    ratings: list[StatusCount]
    average_score: float | None
    by_department: list[DepartmentAverage]


GoalStatus = Literal["Active", "Achieved", "Dropped"]


class GoalCreate(BaseModel):
    staff: str | None = Field(default=None, description="UUID or file number; defaults to you")
    title: str = Field(min_length=3, max_length=150)
    target: str | None = Field(default=None, max_length=2000)
    due_date: date | None = None
    cycle_id: uuid.UUID | None = None
    _t = field_validator("title")(_clean)


class GoalUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=3, max_length=150)
    target: str | None = Field(default=None, max_length=2000)
    progress: int | None = Field(default=None, ge=0, le=100)
    status: GoalStatus | None = None
    due_date: date | None = None


class GoalOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    file_number: str
    title: str
    target: str | None
    progress: int
    status: str
    due_date: date | None
    cycle_id: uuid.UUID | None
    created_by: str | None
    can_edit: bool
    can_delete: bool
