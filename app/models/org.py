from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, IdMixin


class Department(IdMixin, Base):
    __tablename__ = "departments"

    code: Mapped[str] = mapped_column(String(10), unique=True)  # e.g. MED, SUR; used in file numbers
    name: Mapped[str] = mapped_column(String(120), unique=True)


class GradeLevel(IdMixin, Base):
    __tablename__ = "grade_levels"

    scale: Mapped[str] = mapped_column(String(20), default="CONHESS")
    level: Mapped[int] = mapped_column(Integer)
    code: Mapped[str] = mapped_column(String(40), unique=True)  # "CONHESS 7"
