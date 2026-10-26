import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.core.security import utcnow

from .base import Base, IdMixin


class Notification(IdMixin, Base):
    """An in-app notification for one user. Only its recipient can ever read, change or delete it.
    Text must not carry sensitive figures (no salaries, no personal numbers): it is a pointer, not a record."""

    __tablename__ = "notifications"
    __table_args__ = (UniqueConstraint("user_id", "dedupe_key"),)

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    type: Mapped[str] = mapped_column(String(20), index=True)  # leave | document | payroll | license | system
    title: Mapped[str] = mapped_column(String(150))
    text: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(200), nullable=True)  # frontend path, e.g. /leave
    entity_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    # Lets a recurring job (e.g. the expiry sweep) avoid telling the same person the same thing twice.
    dedupe_key: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
