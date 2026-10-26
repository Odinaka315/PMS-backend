import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field


class DocumentInit(BaseModel):
    doc_type: Literal["License", "NYSC", "Promotion", "Appointment", "Certificate", "Passport", "NIN", "CV", "Other"]
    name: str | None = Field(default=None, max_length=200, description="Display name; defaults to the filename")
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(max_length=100)
    size: int = Field(description="File size in bytes")
    expiry_date: date | None = None


class DocumentOut(BaseModel):
    """Matches the frontend's document rows (name, type, status, expiry) plus what the viewer may do."""
    id: uuid.UUID
    staff_id: uuid.UUID
    name: str
    type: str
    status: str  # Pending | Verified | Rejected
    expiry: date | None
    original_filename: str
    content_type: str
    size_bytes: int | None
    uploaded_at: datetime
    verified_by: str | None
    verified_at: datetime | None
    rejection_reason: str | None
    can_download: bool
    can_verify: bool
    can_delete: bool


class UploadTarget(BaseModel):
    url: str
    method: str
    headers: dict[str, str]  # the browser must send exactly these headers
    expires_in: int


class UploadInitOut(BaseModel):
    document: DocumentOut
    upload: UploadTarget


class DownloadOut(BaseModel):
    url: str
    expires_in: int
    filename: str


class VerifyIn(BaseModel):
    decision: Literal["verify", "reject"]
    reason: str | None = Field(default=None, max_length=1000)


class ExpiringOut(BaseModel):
    id: uuid.UUID
    staff_id: uuid.UUID
    file_number: str
    staff_name: str
    name: str
    type: str
    status: str
    expiry: date
    days_left: int
