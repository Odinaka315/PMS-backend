"""Creating login accounts for staff. Used by HR's 'create staff' flow and by POST /staff/{id}/account."""
from datetime import timedelta

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import settings
from app.core.roles import Role
from app.core.security import generate_temp_password, hash_password, utcnow
from app.models import Staff, User


def initial_password() -> tuple[str, bool]:
    """The password a new account starts with, and whether it is the shared one (True) or a one-off (False)."""
    if settings.initial_password_mode == "random":
        return generate_temp_password(), False
    return settings.initial_password, True


def initial_expiry():
    return utcnow() + timedelta(days=settings.initial_password_days)


async def provision_account(session: AsyncSession, *, staff: Staff, actor: User, request: Request | None,
                            automatic: bool = False) -> tuple[User, str | None]:
    """Create the login for a staff member (role 'staff'). Does not commit; the caller's transaction decides.

    Returns the user and, only in 'random' mode, the one-time password for HR to hand over. In 'shared' mode the
    password is the configured INITIAL_PASSWORD, so it is not echoed back.
    The login name is the staff member's email address."""
    email = (staff.email or "").strip().lower()
    if not email:
        raise HTTPException(422, "The staff member has no email address; it is their login name")
    if await session.scalar(select(User.id).where(User.staff_id == staff.id)):
        raise HTTPException(409, "This staff member already has an account")
    if await session.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, f"The email {email} is already used by another account")

    password, shared = initial_password()
    user = User(email=email, password_hash=hash_password(password), role=Role.STAFF.value, staff_id=staff.id,
                must_change_password=True, password_expires_at=initial_expiry())
    session.add(user)
    await session.flush()
    audit.record(session, user=actor, action="user_created", entity="user", entity_id=user.id,
                 subject_staff_id=staff.id,
                 after={"email": email, "role": Role.STAFF.value, "automatic": automatic,
                        "password_mode": settings.initial_password_mode},
                 request=request)
    return user, (None if shared else password)
