from datetime import timedelta

from fastapi import HTTPException, Request, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import settings
from app.core.security import (
    as_utc, create_access_token, hash_password, hash_token, new_refresh_token, utcnow, verify_password,
)
from app.models import RefreshToken, User

from .schemas import TokenOut, UserOut

INVALID = HTTPException(status.HTTP_401_UNAUTHORIZED, "Incorrect email or password")


def user_out(user: User) -> UserOut:
    s = user.staff
    return UserOut(
        id=user.id, email=user.email, role=user.role, is_active=user.is_active,
        must_change_password=user.must_change_password, password_expires_at=user.password_expires_at,
        staff_id=user.staff_id,
        name=s.full_name if s else "System Administrator",
        title=(s.designation if s else "Super Admin"),
        department=(s.department.name if s else "IT"),
        file_number=(s.file_number if s else None),
        avatar=(s.avatar_url if s else None),
        grade=(s.grade_level.code if s and s.grade_level else None),
        step=(f"Step {s.step}" if s and s.step else None),
    )


async def _issue_tokens(session: AsyncSession, user: User, request: Request) -> TokenOut:
    raw, hashed = new_refresh_token()
    now = utcnow()
    session.add(RefreshToken(
        user_id=user.id, token_hash=hashed, created_at=now,
        expires_at=now + timedelta(days=settings.refresh_token_days),
        user_agent=(request.headers.get("user-agent") or "")[:255],
    ))
    return TokenOut(
        access_token=create_access_token(user.id, user.role), refresh_token=raw,
        expires_in=settings.access_token_minutes * 60, user=user_out(user),
    )


async def login(session: AsyncSession, request: Request, email: str, password: str) -> TokenOut:
    email = email.strip().lower()
    user = await session.scalar(select(User).where(User.email == email))
    now = utcnow()

    if user and user.locked_until and as_utc(user.locked_until) > now:
        audit.record(session, user=None, user_email=email, action="login_blocked_locked", entity="user",
                     entity_id=user.id, request=request)
        await session.commit()
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Account temporarily locked. Try again later.")

    ok = verify_password(user.password_hash if user else None, password)
    if not ok or user is None or not user.is_active:
        if user:
            user.failed_attempts += 1
            if user.failed_attempts >= settings.max_failed_logins:
                user.locked_until = now + timedelta(minutes=settings.lockout_minutes)
                user.failed_attempts = 0
        audit.record(session, user=None, user_email=email, action="login_failed", entity="user",
                     entity_id=user.id if user else None, request=request)
        await session.commit()
        raise INVALID

    if user.must_change_password and user.password_expires_at and as_utc(user.password_expires_at) < now:
        # The password was right, so it is safe to say why it no longer works.
        audit.record(session, user=None, user_email=email, action="login_blocked_password_expired", entity="user",
                     entity_id=user.id, subject_staff_id=user.staff_id, request=request)
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            "Your initial password has expired. Please ask HR to reset it for you.")

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now
    tokens = await _issue_tokens(session, user, request)
    audit.record(session, user=user, action="login", entity="user", entity_id=user.id,
                 subject_staff_id=user.staff_id, request=request)
    await session.commit()
    return tokens


async def refresh(session: AsyncSession, request: Request, raw_token: str) -> TokenOut:
    """Rotating refresh tokens: each is single-use. Re-presenting a used one revokes the user's sessions."""
    row = await session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_token(raw_token)))
    if row is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid refresh token")
    now = utcnow()
    if row.revoked_at is not None:
        await session.execute(update(RefreshToken).where(RefreshToken.user_id == row.user_id, RefreshToken.revoked_at.is_(None))
                              .values(revoked_at=now))
        await session.commit()
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token already used; please sign in again")
    if as_utc(row.expires_at) < now:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Refresh token expired")
    user = await session.scalar(select(User).where(User.id == row.user_id))
    if user is None or not user.is_active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Account disabled")
    row.revoked_at = now
    tokens = await _issue_tokens(session, user, request)
    await session.commit()
    return tokens


async def revoke_all(session: AsyncSession, user_id) -> None:
    await session.execute(update(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked_at.is_(None))
                          .values(revoked_at=utcnow()))


def validate_new_password(password: str, email: str, current: str | None = None) -> None:
    if len(password) < settings.min_password_length:
        raise HTTPException(422, f"Password must be at least {settings.min_password_length} characters")
    if current is not None and password == current:
        raise HTTPException(422, "Your new password must be different from your current one")
    weak = {"password", "hospital123", "12345678", settings.initial_password.lower()} - {""}
    if password.lower() == email.lower() or password.lower() in weak:
        raise HTTPException(422, "Password is too easy to guess")


async def change_password(session: AsyncSession, request: Request, user: User, current: str, new: str) -> TokenOut:
    """Sets the user's own password, signs out every other device, and returns fresh tokens for this one so the
    first-login flow (sign in -> choose a password) carries straight on without a second sign-in."""
    if not verify_password(user.password_hash, current):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is incorrect")
    validate_new_password(new, user.email, current=current)
    user.password_hash = hash_password(new)
    user.must_change_password = False
    user.password_expires_at = None
    await revoke_all(session, user.id)  # sign out other devices
    tokens = await _issue_tokens(session, user, request)
    audit.record(session, user=user, action="password_changed", entity="user", entity_id=user.id,
                 subject_staff_id=user.staff_id, request=request)
    await session.commit()
    return tokens
