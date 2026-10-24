import uuid

from pydantic import BaseModel, Field

EMAIL_PATTERN = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"  # permissive on purpose: demo accounts use .test


class LoginIn(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=255)
    password: str = Field(min_length=1, max_length=200)


class RefreshIn(BaseModel):
    refresh_token: str


class ChangePasswordIn(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8, max_length=200)


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    role: str
    is_active: bool
    must_change_password: bool
    staff_id: uuid.UUID | None
    # Mirrors the frontend's `User` type so useAuth can consume it directly.
    name: str | None = None
    title: str | None = None
    department: str | None = None
    file_number: str | None = None
    avatar: str | None = None
    grade: str | None = None
    step: str | None = None


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserOut
