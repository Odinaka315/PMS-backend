import uuid

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Staff, User

from .db import get_session
from .roles import GLOBAL_ROLES, Role
from .security import decode_access_token

bearer = HTTPBearer(auto_error=False)

API_PREFIX = "/api/v1"
# The only endpoints usable while a password change is pending: change it, see who you are, sign out.
ALLOWED_WHILE_PASSWORD_CHANGE_PENDING = {"/auth/change-password", "/auth/me", "/auth/logout", "/auth/logout-all"}


async def get_current_user(
    request: Request,
    creds: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_session),
) -> User:
    unauthorized = HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated", headers={"WWW-Authenticate": "Bearer"})
    if creds is None:
        raise unauthorized
    payload = decode_access_token(creds.credentials)
    if payload is None:
        raise unauthorized
    user = await session.scalar(select(User).where(User.id == uuid.UUID(payload["sub"])))
    if user is None or not user.is_active:
        raise unauthorized
    if user.must_change_password and request.url.path.removeprefix(API_PREFIX) not in ALLOWED_WHILE_PASSWORD_CHANGE_PENDING:
        # Enforced here, on the server: the frontend cannot be relied on to send the user to the change-password screen.
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail={
            "code": "password_change_required", "message": "You must choose a new password before continuing"})
    return user


def require_roles(*roles: Role):
    allowed = {r.value for r in roles}

    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in allowed:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "You do not have permission to do this")
        return user

    return checker


def can_access_staff(user: User, staff: Staff) -> bool:
    """Data scope: staff see themselves, an HOD sees their department, HR/director/superadmin see everyone."""
    if user.role in GLOBAL_ROLES:
        return True
    if user.staff_id == staff.id:
        return True
    return user.role == Role.HOD and user.department_id is not None and user.department_id == staff.department_id


def staff_scope_clause(user: User):
    """SQLAlchemy condition equivalent of can_access_staff, for list queries."""
    if user.role in GLOBAL_ROLES:
        return Staff.deleted_at.is_(None)
    if user.role == Role.HOD and user.department_id is not None:
        return (Staff.deleted_at.is_(None)) & (Staff.department_id == user.department_id)
    return (Staff.deleted_at.is_(None)) & (Staff.id == user.staff_id)


async def get_staff_or_404(session: AsyncSession, ident: str) -> Staff:
    """Accepts a UUID or a file number such as YAB-MED-042 (what the frontend uses in its URLs)."""
    staff = await session.scalar(select(Staff).where(Staff.file_number == ident, Staff.deleted_at.is_(None)))
    if staff is None:
        try:
            staff = await session.scalar(select(Staff).where(Staff.id == uuid.UUID(ident), Staff.deleted_at.is_(None)))
        except ValueError:
            staff = None
    if staff is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Staff member not found")
    return staff


async def get_accessible_staff(session: AsyncSession, user: User, ident: str) -> Staff:
    staff = await get_staff_or_404(session, ident)
    if not can_access_staff(user, staff):
        # 404 rather than 403 so out-of-scope records are not confirmed to exist.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Staff member not found")
    return staff


async def reload_staff(session: AsyncSession, staff_id: uuid.UUID) -> Staff:
    """Re-read a staff row from the database, refreshing relationships that were loaded before a change."""
    return await session.scalar(
        select(Staff).where(Staff.id == staff_id).execution_options(populate_existing=True)
    )
