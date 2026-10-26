import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import require_roles
from app.core.roles import Role
from app.core.security import generate_temp_password, hash_password
from app.models import Staff, User
from app.modules.auth.schemas import EMAIL_PATTERN, UserOut
from app.modules.auth.service import revoke_all, user_out
from app.modules.users.service import initial_expiry

router = APIRouter(prefix="/users", tags=["users"])

# Account creation is limited to HR and superadmin (per the hospital's requirement).
# ASSUMPTION: HR can manage ordinary accounts only; privileged roles are superadmin-only.
HR_MANAGEABLE = {Role.STAFF.value, Role.HOD.value}


class UserCreate(BaseModel):
    email: str = Field(pattern=EMAIL_PATTERN, max_length=255)
    role: Role
    staff_id: uuid.UUID | None = None
    # No password field: the system generates a one-time password and returns it once, so nobody chooses (or knows)
    # a password except the account's owner, after their first sign-in.


class UserCreated(UserOut):
    temporary_password: str  # shown once; hand it to the account's owner


class UserPatch(BaseModel):
    is_active: bool | None = None
    role: Role | None = None


class UserPage(BaseModel):
    items: list[UserOut]
    total: int
    page: int
    size: int


def _check_manageable(actor: User, role: str) -> None:
    if actor.role == Role.HR and role not in HR_MANAGEABLE:
        raise HTTPException(403, "Only a super admin can manage this kind of account")


@router.post("", response_model=UserCreated, status_code=201)
async def create_user(body: UserCreate, request: Request, actor: User = Depends(require_roles(Role.HR, Role.SUPERADMIN)),
                      session: AsyncSession = Depends(get_session)):
    _check_manageable(actor, body.role.value)
    email = body.email.strip().lower()
    if await session.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "An account with this email already exists")

    if body.role != Role.SUPERADMIN:
        if body.staff_id is None:
            raise HTTPException(422, "staff_id is required: every account except super admin belongs to a staff record")
        staff = await session.scalar(select(Staff).where(Staff.id == body.staff_id, Staff.deleted_at.is_(None)))
        if staff is None:
            raise HTTPException(404, "Staff record not found")
        if await session.scalar(select(User.id).where(User.staff_id == staff.id)):
            raise HTTPException(409, "This staff member already has an account")

    temp = generate_temp_password()
    user = User(email=email, password_hash=hash_password(temp), role=body.role.value,
                staff_id=body.staff_id, must_change_password=True, password_expires_at=initial_expiry())
    session.add(user)
    await session.flush()
    audit.record(session, user=actor, action="user_created", entity="user", entity_id=user.id,
                 subject_staff_id=user.staff_id, after={"email": email, "role": body.role.value}, request=request)
    await session.commit()
    await session.refresh(user)
    return UserCreated(**user_out(user).model_dump(), temporary_password=temp)


@router.get("", response_model=UserPage)
async def list_users(q: str | None = None, role: Role | None = None,
                     awaiting_first_login: bool = Query(False, description="Only accounts still on their one-time password"),
                     page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
                     _: User = Depends(require_roles(Role.HR, Role.SUPERADMIN)),
                     session: AsyncSession = Depends(get_session)):
    stmt = select(User).outerjoin(Staff, User.staff_id == Staff.id)
    if q:
        like = f"%{q.lower()}%"
        stmt = stmt.where(or_(func.lower(User.email).like(like), func.lower(Staff.first_name).like(like),
                              func.lower(Staff.last_name).like(like)))
    if role:
        stmt = stmt.where(User.role == role.value)
    if awaiting_first_login:
        # Lets HR chase people who have not signed in yet; password_expires_at shows who is about to be locked out.
        stmt = stmt.where(User.must_change_password.is_(True))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(User.email).offset((page - 1) * size).limit(size))).unique().all()
    return UserPage(items=[user_out(u) for u in rows], total=total or 0, page=page, size=size)


@router.get("/summary")
async def users_summary(_: User = Depends(require_roles(Role.HR, Role.DIRECTOR, Role.SUPERADMIN)),
                        session: AsyncSession = Depends(get_session)):
    """Feeds the director's 'system users' card and the super admin's 'Total Users' KPI."""
    rows = (await session.execute(select(User.role, func.count()).where(User.is_active.is_(True)).group_by(User.role))).all()
    by_role = {r: c for r, c in rows}
    return {"total": sum(by_role.values()), "by_role": {r.value: by_role.get(r.value, 0) for r in Role}}


async def _get_user(session: AsyncSession, user_id: uuid.UUID) -> User:
    user = await session.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise HTTPException(404, "User not found")
    return user


@router.patch("/{user_id}", response_model=UserOut)
async def patch_user(user_id: uuid.UUID, body: UserPatch, request: Request,
                     actor: User = Depends(require_roles(Role.HR, Role.SUPERADMIN)),
                     session: AsyncSession = Depends(get_session)):
    user = await _get_user(session, user_id)
    _check_manageable(actor, user.role)
    if user.id == actor.id and (body.is_active is False or (body.role and body.role.value != user.role)):
        raise HTTPException(400, "You cannot deactivate or re-role your own account")
    before, after = {}, {}
    if body.is_active is not None and body.is_active != user.is_active:
        before["is_active"], after["is_active"] = user.is_active, body.is_active
        user.is_active = body.is_active
        if not body.is_active:
            await revoke_all(session, user.id)
    if body.role is not None and body.role.value != user.role:
        _check_manageable(actor, body.role.value)
        if body.role != Role.SUPERADMIN and user.staff_id is None:
            raise HTTPException(422, "This account has no staff record, so it cannot take that role")
        before["role"], after["role"] = user.role, body.role.value
        user.role = body.role.value
    if after:
        audit.record(session, user=actor, action="user_updated", entity="user", entity_id=user.id,
                     subject_staff_id=user.staff_id, before=before, after=after, request=request)
    await session.commit()
    return user_out(user)


@router.post("/{user_id}/reset-password")
async def reset_password(user_id: uuid.UUID, request: Request,
                         actor: User = Depends(require_roles(Role.HR, Role.SUPERADMIN)),
                         session: AsyncSession = Depends(get_session)):
    user = await _get_user(session, user_id)
    _check_manageable(actor, user.role)
    temp = generate_temp_password()
    user.password_hash = hash_password(temp)
    user.must_change_password = True
    user.password_expires_at = initial_expiry()
    user.failed_attempts, user.locked_until = 0, None
    await revoke_all(session, user.id)
    audit.record(session, user=actor, action="password_reset", entity="user", entity_id=user.id,
                 subject_staff_id=user.staff_id, request=request)
    await session.commit()
    return {"temporary_password": temp}
