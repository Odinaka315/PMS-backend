import uuid
from datetime import date, datetime

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, IdMixin, TimestampMixin


class AppraisalCycle(IdMixin, TimestampMixin, Base):
    """One appraisal round (for example 'Annual Appraisal 2026'). The criteria and rating bands are copied in when the
    cycle is created and frozen once it opens, so results never shift under people's feet."""

    __tablename__ = "appraisal_cycles"
    __table_args__ = (UniqueConstraint("year", "name"),)

    name: Mapped[str] = mapped_column(String(120))
    year: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String(10), default="Draft", index=True)  # Draft | Open | Closed
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    self_deadline: Mapped[date] = mapped_column(Date)  # after this the appraiser may score without a self-assessment
    criteria: Mapped[list] = mapped_column(JSON)  # [{key, name, weight}]  weights add up to 100
    rating_bands: Mapped[list] = mapped_column(JSON)  # [{min, label}]  highest first, last one has min 0
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Appraisal(IdMixin, TimestampMixin, Base):
    """One person's appraisal in one cycle.
    Not Started -> Self-Assessed -> Reviewed (appraiser scored) -> Finalised (HR/director) -> acknowledged by the person."""

    __tablename__ = "appraisals"
    __table_args__ = (UniqueConstraint("cycle_id", "staff_id"),)

    cycle_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("appraisal_cycles.id"), index=True)
    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"), index=True)
    department: Mapped[str] = mapped_column(String(120))  # as it was when the cycle opened
    appraiser_user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(15), default="Not Started", index=True)
    self_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    self_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    self_submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    appraiser_scores: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    appraiser_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    score_x100: Mapped[int | None] = mapped_column(Integer, nullable=True)  # weighted score in hundredths, e.g. 9150 = 91.50
    rating: Mapped[str | None] = mapped_column(String(40), nullable=True)
    finalised_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    finalised_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalise_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    return_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    staff_comment: Mapped[str | None] = mapped_column(Text, nullable=True)


class PerformanceGoal(IdMixin, TimestampMixin, Base):
    __tablename__ = "performance_goals"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"), index=True)
    cycle_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("appraisal_cycles.id"), nullable=True)
    title: Mapped[str] = mapped_column(String(150))
    target: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)  # 0-100
    status: Mapped[str] = mapped_column(String(10), default="Active")  # Active | Achieved | Dropped
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
