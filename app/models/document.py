import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, IdMixin, TimestampMixin


class Document(IdMixin, TimestampMixin, Base):
    """Metadata only. The file itself lives in object storage under object_key (never exposed to clients)."""

    __tablename__ = "documents"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id"), index=True)
    doc_type: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    original_filename: Mapped[str] = mapped_column(String(255))
    content_type: Mapped[str] = mapped_column(String(100))
    declared_size: Mapped[int] = mapped_column(Integer)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)  # actual size, set once validated
    object_key: Mapped[str] = mapped_column(String(300), unique=True)
    # Awaiting Upload -> Pending -> Verified | Rejected ;  Failed = uploaded file did not pass validation
    status: Mapped[str] = mapped_column(String(20), default="Awaiting Upload", index=True)
    expiry_date: Mapped[date | None] = mapped_column(Date, nullable=True, index=True)
    uploaded_by: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"))
    verified_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_by_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
