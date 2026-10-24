import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, IdMixin, TimestampMixin


class Staff(IdMixin, TimestampMixin, Base):
    __tablename__ = "staff"

    file_number: Mapped[str] = mapped_column(String(30), unique=True, index=True)  # e.g. YAB-MED-042
    first_name: Mapped[str] = mapped_column(String(80), index=True)
    last_name: Mapped[str] = mapped_column(String(80), index=True)
    other_names: Mapped[str | None] = mapped_column(String(120), nullable=True)
    title: Mapped[str | None] = mapped_column(String(20), nullable=True)  # Dr., Mrs., ...
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(30), nullable=True)
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    gender: Mapped[str | None] = mapped_column(String(20), nullable=True)
    marital_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    nationality: Mapped[str | None] = mapped_column(String(60), nullable=True)
    state_of_origin: Mapped[str | None] = mapped_column(String(60), nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    department_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("departments.id"), index=True)
    designation: Mapped[str | None] = mapped_column(String(120), nullable=True)
    grade_level_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("grade_levels.id"), nullable=True)
    step: Mapped[int | None] = mapped_column(Integer, nullable=True)
    employment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    confirmation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="Active", index=True)  # Active | On Leave | Suspended | Retired | Exited
    supervisor_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("staff.id"), nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    department = relationship("Department", lazy="joined")
    grade_level = relationship("GradeLevel", lazy="joined")
    supervisor = relationship("Staff", remote_side="Staff.id", lazy="joined", join_depth=1)

    education = relationship("Education", cascade="all, delete-orphan", lazy="selectin")
    dependants = relationship("Dependant", cascade="all, delete-orphan", lazy="selectin")
    license = relationship("MedicalLicense", uselist=False, cascade="all, delete-orphan", lazy="selectin")
    emergency_contact = relationship("EmergencyContact", uselist=False, cascade="all, delete-orphan", lazy="selectin")
    bank = relationship("BankAccount", uselist=False, cascade="all, delete-orphan", lazy="selectin")

    @property
    def full_name(self) -> str:
        parts = [self.title, self.first_name, self.last_name]
        return " ".join(p for p in parts if p)


class Education(IdMixin, Base):
    __tablename__ = "staff_education"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), index=True)
    institution: Mapped[str] = mapped_column(String(200))
    qualification: Mapped[str] = mapped_column(String(200))
    year: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Dependant(IdMixin, Base):
    __tablename__ = "staff_dependants"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(150))
    relationship_: Mapped[str] = mapped_column("relationship", String(40))
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)


class MedicalLicense(IdMixin, Base):
    __tablename__ = "staff_licenses"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), unique=True)
    license_number: Mapped[str] = mapped_column(String(60))
    issued_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    expires_on: Mapped[date | None] = mapped_column(Date, nullable=True)
    body: Mapped[str | None] = mapped_column(String(60), nullable=True)  # e.g. MDCN, NMCN


class EmergencyContact(IdMixin, Base):
    __tablename__ = "staff_emergency_contacts"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), unique=True)
    name: Mapped[str] = mapped_column(String(150))
    relationship_: Mapped[str] = mapped_column("relationship", String(40))
    phone: Mapped[str] = mapped_column(String(30))
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)


class BankAccount(IdMixin, Base):
    __tablename__ = "staff_bank_accounts"

    staff_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("staff.id", ondelete="CASCADE"), unique=True)
    bank_name: Mapped[str] = mapped_column(String(100))
    account_name: Mapped[str] = mapped_column(String(150))
    account_number_enc: Mapped[str] = mapped_column(String(500))  # Fernet-encrypted
    bvn_enc: Mapped[str | None] = mapped_column(String(500), nullable=True)  # Fernet-encrypted
