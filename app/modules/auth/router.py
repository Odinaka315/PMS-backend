from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import get_current_user
from app.core.security import hash_token
from app.models import RefreshToken, User

from . import service
from .schemas import ChangePasswordIn, LoginIn, RefreshIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request, session: AsyncSession = Depends(get_session)):
    return await service.login(session, request, body.email, body.password)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshIn, request: Request, session: AsyncSession = Depends(get_session)):
    return await service.refresh(session, request, body.refresh_token)


@router.post("/logout", status_code=204)
async def logout(body: RefreshIn, request: Request, user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    row = await session.scalar(select(RefreshToken).where(
        RefreshToken.token_hash == hash_token(body.refresh_token), RefreshToken.user_id == user.id))
    if row and row.revoked_at is None:
        from app.core.security import utcnow
        row.revoked_at = utcnow()
    audit.record(session, user=user, action="logout", entity="user", entity_id=user.id,
                 subject_staff_id=user.staff_id, request=request)
    await session.commit()
    return Response(status_code=204)


@router.post("/logout-all", status_code=204)
async def logout_all(request: Request, user: User = Depends(get_current_user),
                     session: AsyncSession = Depends(get_session)):
    """The Settings page's 'Sign out of all devices'."""
    await service.revoke_all(session, user.id)
    audit.record(session, user=user, action="logout_all", entity="user", entity_id=user.id,
                 subject_staff_id=user.staff_id, request=request)
    await session.commit()
    return Response(status_code=204)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)):
    return service.user_out(user)


@router.post("/change-password", status_code=204)
async def change_password(body: ChangePasswordIn, request: Request, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    await service.change_password(session, request, user, body.current_password, body.new_password)
    return Response(status_code=204)
