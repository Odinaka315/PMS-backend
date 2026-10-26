import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.modules.auth.schemas import EMAIL_PATTERN


class StaffSummary(BaseModel):
    """One row of the directory; matches the frontend's staffList shape (plus ids)."""
    id: uuid.UUID
    file_number: str
    name: str
    dept: str
    department_id: uuid.UUID
    grade: str | None
    step: int | None
    status: str
    avatar: str | None
    designation: str | None


class StaffPage(BaseModel):
    items: list[StaffSummary]
    total: int
    page: int
    size: int


class StaffLookup(BaseModel):
    id: uuid.UUID
    file_number: str
    name: str
    dept: str


class StaffCreate(BaseModel):
    file_number: str | None = Field(default=None, max_length=30)  # generated from the department code if omitted
    title: str | None = Field(default=None, max_length=20)
    first_name: str = Field(min_length=1, max_length=80)
    last_name: str = Field(min_length=1, max_length=80)
    other_names: str | None = None
    email: str | None = Field(default=None, pattern=EMAIL_PATTERN, max_length=255)  # also the login name
    phone: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    marital_status: str | None = None
    nationality: str | None = "Nigerian"
    state_of_origin: str | None = None
    address: str | None = None
    avatar_url: str | None = None
    department_id: uuid.UUID
    designation: str | None = None
    grade_level_id: uuid.UUID | None = None
    step: int | None = Field(default=None, ge=1, le=15)
    employment_date: date | None = None
    confirmation_date: date | None = None
    supervisor_id: uuid.UUID | None = None
    # Create the staff member's login automatically (needs an email address). Untick for people who won't use the portal.
    create_account: bool = True


class StaffUpdate(BaseModel):
    """HR edit. Every field optional; file_number is immutable."""
    title: str | None = None
    first_name: str | None = Field(default=None, min_length=1, max_length=80)
    last_name: str | None = Field(default=None, min_length=1, max_length=80)
    other_names: str | None = None
    email: str | None = Field(default=None, pattern=EMAIL_PATTERN, max_length=255)
    phone: str | None = None
    date_of_birth: date | None = None
    gender: str | None = None
    marital_status: str | None = None
    nationality: str | None = None
    state_of_origin: str | None = None
    address: str | None = None
    avatar_url: str | None = None
    department_id: uuid.UUID | None = None
    designation: str | None = None
    grade_level_id: uuid.UUID | None = None
    step: int | None = Field(default=None, ge=1, le=15)
    employment_date: date | None = None
    confirmation_date: date | None = None
    supervisor_id: uuid.UUID | None = None
    status: str | None = Field(default=None, pattern=r"^(Active|On Leave|Suspended|Retired|Exited)$")


class StaffSelfUpdate(BaseModel):
    phone: str | None = None
    address: str | None = None
    avatar_url: str | None = None
    marital_status: str | None = None


class EducationIn(BaseModel):
    institution: str = Field(min_length=2, max_length=200)
    qualification: str = Field(min_length=2, max_length=200)
    year: int | None = Field(default=None, ge=1940, le=2100)


class EducationOut(EducationIn):
    id: uuid.UUID


class DependantIn(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    relationship: str = Field(min_length=2, max_length=40)
    date_of_birth: date | None = None


class DependantOut(DependantIn):
    id: uuid.UUID


class EmergencyContactIn(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    relationship: str = Field(min_length=2, max_length=40)
    phone: str = Field(min_length=5, max_length=30)
    email: str | None = None


class LicenseIn(BaseModel):
    license_number: str = Field(min_length=2, max_length=60)
    issued_on: date | None = None
    expires_on: date | None = None
    body: str | None = None


class LicenseOut(LicenseIn):
    status: str  # Active | Expiring Soon | Expired


class BankIn(BaseModel):
    bank_name: str = Field(min_length=2, max_length=100)
    account_name: str = Field(min_length=2, max_length=150)
    account_number: str = Field(pattern=r"^\d{10}$")  # NUBAN
    bvn: str | None = Field(default=None, pattern=r"^\d{11}$")


class BankOut(BaseModel):
    bank_name: str
    account_name: str
    account_number: str  # masked unless the viewer is the owner or HR
    bvn: str | None  # always masked


class Computed(BaseModel):
    years_of_service: float | None
    service_label: str | None  # "5y 4m"
    retirement_date: date | None
    time_to_retirement_label: str | None


class AccountInfo(BaseModel):
    """The login created for a staff member."""
    user_id: uuid.UUID
    email: str  # the login name
    must_change_password: bool
    initial_password_expires_at: datetime | None
    # Only in INITIAL_PASSWORD_MODE=random: the one-time password for HR to hand over. Never shown again.
    temporary_password: str | None = None


class StaffProfile(BaseModel):
    """Aggregate for the Profile pages' tabs (Personal, Employment, Education, License, Emergency, Dependants, Bank)."""
    id: uuid.UUID
    file_number: str
    name: str
    avatar: str | None
    status: str
    personal: dict
    employment: dict
    education: list[EducationOut]
    license: LicenseOut | None
    emergency_contact: dict | None
    dependants: list[DependantOut]
    bank: BankOut | None
    computed: Computed


class StaffCreated(StaffProfile):
    account: AccountInfo | None = None
    account_note: str | None = None  # why no account was created, when none was
