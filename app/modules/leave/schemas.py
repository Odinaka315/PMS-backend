import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field


class LeaveTypeOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    annual_entitlement_days: int
    allow_backdate: bool


class BalanceRow(BaseModel):
    leave_type_id: uuid.UUID
    code: str
    name: str
    entitlement: int
    used: int       # approved days this year
    pending: int    # awaiting approval
    remaining: int  # entitlement - used - pending


class BalanceOut(BaseModel):
    year: int
    items: list[BalanceRow]
    total_remaining: int


class LeaveCreate(BaseModel):
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    reason: str | None = Field(default=None, max_length=2000)
    replacement_staff_id: uuid.UUID | None = None


class DecisionIn(BaseModel):
    decision: str = Field(pattern=r"^(approve|decline)$")
    comment: str | None = Field(default=None, max_length=2000)


class StepOut(BaseModel):
    """One node of the stepper: Submitted -> HOD Approved -> HR Approved -> DA Approved -> Completed."""
    label: str
    role: str | None
    state: str  # done | current | upcoming | declined
    decided_by: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class LeaveOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    staff_name: str
    file_number: str
    department: str
    type: str
    leave_type_id: uuid.UUID
    start_date: date
    end_date: date
    days: int
    reason: str | None
    replacement: str | None
    status: str
    current_step_role: str | None
    created_at: datetime
    steps: list[StepOut]


class LeavePage(BaseModel):
    items: list[LeaveOut]
    total: int
    page: int
    size: int
