import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import utcnow

from .base import Base, IdMixin, TimestampMixin


class StaffQuery(IdMixin, TimestampMixin, Base):
    """A formal query: management asks a staff member to explain something, they reply, and it is resolved.
    Never deleted; a mistaken one is withdrawn, so the record of what happened stays complete."""

    __tablename__ = "staff_queries"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"), index=True)
    title: Mapped[str] = mapped_column(String(150))
    description: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(10))  # Low | Medium | High
    # Pending Reply -> Replied -> Resolved -> (Appealed -> Resolved) ;  Withdrawn from either open state
    status: Mapped[str] = mapped_column(String(20), default="Pending Reply", index=True)
    issued_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    reply_due: Mapped[date] = mapped_column(Date)
    replied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Cleared | Warning | Reprimand | Further Action
    appealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    appeal_decided_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    appeal_outcome: Mapped[str | None] = mapped_column(String(12), nullable=True)  # Upheld | Overturned


class QueryEvent(IdMixin, Base):
    """The timeline of a query: what was said, by whom, when."""

    __tablename__ = "query_events"

    query_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff_queries.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))  # issued | reply | resolved | appeal | appeal_decision | withdrawn
    by_user: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    by_email: Mapped[str] = mapped_column(String(255))
    by_role: Mapped[str] = mapped_column(String(20))
    text: Mapped[str] = mapped_column(Text)
    detail: Mapped[str | None] = mapped_column(String(40), nullable=True)  # e.g. the outcome chosen
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
