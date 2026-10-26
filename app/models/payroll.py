"""Payroll. Money is stored as whole kobo (integers) so there is never any floating-point drift.
Every figure here is a PLACEHOLDER until the hospital confirms its real pay structure; HR can change all of it."""
import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, IdMixin, TimestampMixin


class SalaryScale(IdMixin, Base):
    """Monthly basic pay for a grade level and step."""

    __tablename__ = "salary_scales"
    __table_args__ = (UniqueConstraint("grade_level_id", "step"),)

    grade_level_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("grade_levels.id"), index=True)
    step: Mapped[int] = mapped_column(Integer)
    basic_kobo: Mapped[int] = mapped_column(BigInteger)


class PayComponent(IdMixin, TimestampMixin, Base):
    """An allowance or a deduction applied to everyone it covers (optionally limited to a range of grade levels)."""

    __tablename__ = "pay_components"

    code: Mapped[str] = mapped_column(String(30), unique=True)
    name: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(10))  # earning | deduction
    method: Mapped[str] = mapped_column(String(20))  # fixed | percent_of_basic | percent_of_gross (deductions only)
    amount_kobo: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # for fixed
    rate_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)  # for percent_*, in basis points (2500 = 25%)
    min_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=100)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PayAdjustment(IdMixin, TimestampMixin, Base):
    """A person-specific extra: call duty, a loan repayment, a one-off arrears payment. Applies for a range of months."""

    __tablename__ = "pay_adjustments"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"), index=True)
    kind: Mapped[str] = mapped_column(String(10))  # earning | deduction
    name: Mapped[str] = mapped_column(String(80))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    start_period: Mapped[int] = mapped_column(Integer)  # YYYYMM
    end_period: Mapped[int | None] = mapped_column(Integer, nullable=True)  # YYYYMM, inclusive; null = until cancelled
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PayrollRun(IdMixin, TimestampMixin, Base):
    """One month's payroll. Draft (recalculable) -> Approved (director/super admin, never the creator) -> Paid."""

    __tablename__ = "payroll_runs"

    year: Mapped[int] = mapped_column(Integer)
    month: Mapped[int] = mapped_column(Integer)
    period: Mapped[int] = mapped_column(Integer, unique=True)  # YYYYMM: one run per month
    status: Mapped[str] = mapped_column(String(10), default="Draft", index=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    exceptions_acknowledged: Mapped[bool] = mapped_column(Boolean, default=False)
    paid_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    staff_count: Mapped[int] = mapped_column(Integer, default=0)
    exception_count: Mapped[int] = mapped_column(Integer, default=0)
    total_gross_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    total_deductions_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    total_net_kobo: Mapped[int] = mapped_column(BigInteger, default=0)


class Payslip(IdMixin, Base):
    """A snapshot: later changes to scales, components or a person's record never alter a payslip already produced."""

    __tablename__ = "payslips"
    __table_args__ = (UniqueConstraint("run_id", "staff_id"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payroll_runs.id", ondelete="CASCADE"), index=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"), index=True)
    file_number: Mapped[str] = mapped_column(String(30))
    staff_name: Mapped[str] = mapped_column(String(200))
    department: Mapped[str] = mapped_column(String(120))
    grade_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    basic_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    gross_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    deductions_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    net_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    lines: Mapped[list] = mapped_column(JSON, default=list)  # [{code, name, kind, amount_kobo}]
    bank_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    account_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)  # never the full number
    exception: Mapped[str | None] = mapped_column(String(200), nullable=True)  # why this person needs attention
