import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Severity = Literal["Low", "Medium", "High"]
Outcome = Literal["Cleared", "Warning", "Reprimand", "Further Action"]


def _text(v: str) -> str:
    v = v.strip()
    if not v:
        raise ValueError("This cannot be empty")
    return v


class QueryCreate(BaseModel):
    staff: str = Field(description="UUID or file number of the person being queried")
    title: str = Field(min_length=3, max_length=150)
    description: str = Field(min_length=3, max_length=5000)
    severity: Severity = "Medium"
    reply_due: date | None = Field(default=None, description="Defaults to QUERY_REPLY_DAYS from today")

    _t = field_validator("title", "description")(_text)


class ReplyIn(BaseModel):
    text: str = Field(min_length=1, max_length=5000)
    _t = field_validator("text")(_text)


class ResolveIn(BaseModel):
    outcome: Outcome
    note: str = Field(min_length=1, max_length=2000)
    _t = field_validator("note")(_text)


class AppealIn(BaseModel):
    grounds: str = Field(min_length=1, max_length=5000)
    _t = field_validator("grounds")(_text)


class AppealDecisionIn(BaseModel):
    decision: Literal["Upheld", "Overturned"]
    note: str = Field(min_length=1, max_length=2000)
    _t = field_validator("note")(_text)


class WithdrawIn(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    _t = field_validator("reason")(_text)


class EventOut(BaseModel):
    kind: str
    by: str
    role: str
    text: str
    detail: str | None
    at: datetime


class QueryOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    file_number: str
    staff_name: str
    department: str
    title: str
    description: str
    severity: str
    status: str
    issued_by: str | None
    issued_at: datetime
    reply_due: date
    overdue: bool
    replied_at: datetime | None
    outcome: str | None            # the outcome that stands (Cleared if an appeal overturned it)
    resolved_by: str | None
    resolved_at: datetime | None
    appealed_at: datetime | None
    appeal_outcome: str | None
    appeal_open_until: date | None
    can_reply: bool
    can_resolve: bool
    can_appeal: bool
    can_decide_appeal: bool
    can_withdraw: bool


class QueryDetail(QueryOut):
    events: list[EventOut]


class QueryPage(BaseModel):
    items: list[QueryOut]
    total: int
    page: int
    size: int


class QuerySummary(BaseModel):
    pending_reply: int
    overdue: int
    replied: int
    appealed: int
    resolved: int
    awaiting_me: int
