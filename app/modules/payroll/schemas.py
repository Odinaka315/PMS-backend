import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

PERIOD = r"^\d{4}-(0[1-9]|1[0-2])$"
Kind = Literal["earning", "deduction"]
Method = Literal["fixed", "percent_of_basic", "percent_of_gross"]
MAX_NAIRA = 1_000_000_000


def naira(kobo: int | None) -> float | None:
    return None if kobo is None else round(kobo / 100, 2)


def kobo(naira_amount: float) -> int:
    return int(round(naira_amount * 100))


# ------------------------------------------------------------------ pay structure
class ScaleEntryIn(BaseModel):
    level: int = Field(ge=1, le=20)
    step: int = Field(ge=1, le=20)
    basic: float = Field(gt=0, le=MAX_NAIRA, description="Monthly basic pay in naira")


class ScaleUpdate(BaseModel):
    entries: list[ScaleEntryIn] = Field(min_length=1, max_length=500)


class ScaleEntryOut(BaseModel):
    grade_level_id: uuid.UUID
    level: int
    code: str
    step: int
    basic: float


class ComponentIn(BaseModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,29}$")
    name: str = Field(min_length=1, max_length=80)
    kind: Kind
    method: Method
    amount: float | None = Field(default=None, ge=0, le=MAX_NAIRA, description="Naira, for method=fixed")
    percent: float | None = Field(default=None, ge=0, le=100, description="For the percent_* methods")
    min_level: int | None = Field(default=None, ge=1, le=20)
    max_level: int | None = Field(default=None, ge=1, le=20)
    sort_order: int = Field(default=100, ge=0, le=1000)
    is_active: bool = True

    @model_validator(mode="after")
    def _consistent(self):
        if self.method == "fixed":
            if self.amount is None or self.percent is not None:
                raise ValueError("A fixed component needs an amount (and no percent)")
        else:
            if self.percent is None or self.amount is not None:
                raise ValueError("A percentage component needs a percent (and no amount)")
        if self.method == "percent_of_gross" and self.kind != "deduction":
            raise ValueError("percent_of_gross can only be used for deductions")
        if self.min_level and self.max_level and self.min_level > self.max_level:
            raise ValueError("min_level cannot be above max_level")
        return self


class ComponentOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    kind: str
    method: str
    amount: float | None
    percent: float | None
    min_level: int | None
    max_level: int | None
    sort_order: int
    is_active: bool


class AdjustmentIn(BaseModel):
    staff: str = Field(description="UUID or file number")
    kind: Kind
    name: str = Field(min_length=1, max_length=80)
    amount: float = Field(gt=0, le=MAX_NAIRA)
    start_period: str = Field(pattern=PERIOD)
    end_period: str | None = Field(default=None, pattern=PERIOD)
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _order(self):
        if self.end_period and self.end_period < self.start_period:
            raise ValueError("end_period cannot be before start_period")
        return self


class AdjustmentOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    file_number: str
    staff_name: str
    kind: str
    name: str
    amount: float
    start_period: str
    end_period: str | None
    reason: str | None
    active: bool


# ------------------------------------------------------------------ runs and payslips
class RunCreate(BaseModel):
    year: int = Field(ge=2000, le=2100)
    month: int = Field(ge=1, le=12)


class ApproveIn(BaseModel):
    acknowledge_exceptions: bool = False


class RunOut(BaseModel):
    id: uuid.UUID
    year: int
    month: int
    label: str
    status: str
    staff_count: int
    exception_count: int
    total_gross: float
    total_deductions: float
    total_net: float
    created_by: str | None
    created_at: datetime
    approved_by: str | None
    approved_at: datetime | None
    exceptions_acknowledged: bool
    paid_by: str | None
    paid_at: datetime | None


class LineOut(BaseModel):
    code: str
    name: str
    kind: str
    amount: float


class PayslipSummary(BaseModel):
    id: uuid.UUID
    run_id: uuid.UUID
    period: str
    label: str
    staff_id: uuid.UUID
    file_number: str
    staff_name: str
    department: str
    grade: str | None
    step: int | None
    gross: float
    deductions: float
    net: float
    exception: str | None


class PayslipOut(PayslipSummary):
    basic: float
    earnings: list[LineOut]
    deduction_lines: list[LineOut]
    bank_name: str | None
    account_last4: str | None
    run_status: str
    paid_at: datetime | None


class PayslipPage(BaseModel):
    items: list[PayslipSummary]
    total: int
    page: int
    size: int


class RunDetail(RunOut):
    exceptions: list[PayslipSummary]


class DepartmentTotal(BaseModel):
    department: str
    staff: int
    gross: float
    net: float


class PayrollSummary(BaseModel):
    latest_run: RunOut | None
    by_department: list[DepartmentTotal]


class MyPayslipPage(BaseModel):
    items: list[PayslipSummary]
