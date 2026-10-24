import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, IdMixin, TimestampMixin


class LeaveType(IdMixin, Base):
    """Configurable by HR. Entitlements here are PLACEHOLDERS until the hospital confirms its policy."""

    __tablename__ = "leave_types"

    code: Mapped[str] = mapped_column(String(30), unique=True)  # annual, sick, compassionate
    name: Mapped[str] = mapped_column(String(80), unique=True)
    annual_entitlement_days: Mapped[int] = mapped_column(Integer)
    allow_backdate: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class LeaveWorkflowStep(IdMixin, Base):
    """Approval chain. leave_type_id NULL = default chain used when a type has no chain of its own."""

    __tablename__ = "leave_workflow_steps"
    __table_args__ = (UniqueConstraint("leave_type_id", "step_order"),)

    leave_type_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("leave_types.id"), nullable=True)
    step_order: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))  # hod | hr | director
    label: Mapped[str] = mapped_column(String(60))


class LeaveRequest(IdMixin, TimestampMixin, Base):
    __tablename__ = "leave_requests"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"), index=True)
    leave_type_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leave_types.id"))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    replacement_staff_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Pending", index=True)  # Pending|Approved|Declined|Cancelled
    current_step_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    current_step_role: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)

    staff = relationship("Staff", foreign_keys=[staff_id], lazy="joined")
    leave_type = relationship("LeaveType", lazy="joined")
    replacement = relationship("Staff", foreign_keys=[replacement_staff_id], lazy="joined")
    decisions = relationship("LeaveDecision", order_by="LeaveDecision.decided_at", lazy="selectin", cascade="all, delete-orphan")


class LeaveDecision(IdMixin, Base):
    __tablename__ = "leave_decisions"

    request_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("leave_requests.id", ondelete="CASCADE"), index=True)
    step_order: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(20))
    decided_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(10))  # approve | decline
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    decider = relationship("User", lazy="joined")
