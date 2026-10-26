import uuid
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import get_current_user, require_roles
from app.core.roles import Role
from app.core.security import utcnow
from app.models import Document, MedicalLicense, Notification, Staff, User

from .service import notify, user_id_for_staff

router = APIRouter(prefix="/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    id: uuid.UUID
    type: str
    title: str
    text: str
    link: str | None
    read: bool
    created_at: datetime


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    total: int
    unread: int
    page: int
    size: int


def _out(n: Notification) -> NotificationOut:
    return NotificationOut(id=n.id, type=n.type, title=n.title, text=n.text, link=n.link, read=n.read_at is not None,
                           created_at=n.created_at)


def _mine(user: User):
    return Notification.user_id == user.id


@router.get("", response_model=NotificationPage)
async def list_notifications(unread_only: bool = False, type: str | None = Query(None, max_length=20),
                             page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100),
                             user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    stmt = select(Notification).where(_mine(user))
    if unread_only:
        stmt = stmt.where(Notification.read_at.is_(None))
    if type:
        stmt = stmt.where(Notification.type == type)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(Notification.created_at.desc(), Notification.id)
                                  .offset((page - 1) * size).limit(size))).all()
    unread = await session.scalar(select(func.count()).where(_mine(user), Notification.read_at.is_(None)))
    return NotificationPage(items=[_out(n) for n in rows], total=total or 0, unread=unread or 0, page=page, size=size)


@router.get("/unread-count")
async def unread_count(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Cheap enough for the bell icon to poll."""
    n = await session.scalar(select(func.count()).where(_mine(user), Notification.read_at.is_(None)))
    return {"unread": n or 0}


@router.post("/read-all")
async def mark_all_read(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    res = await session.execute(update(Notification).where(_mine(user), Notification.read_at.is_(None))
                                .values(read_at=utcnow()))
    await session.commit()
    return {"marked": res.rowcount or 0}


async def _own(session: AsyncSession, user: User, nid: uuid.UUID) -> Notification:
    n = await session.scalar(select(Notification).where(Notification.id == nid, _mine(user)))
    if n is None:  # someone else's notification looks exactly like a missing one
        raise HTTPException(404, "Notification not found")
    return n


@router.post("/{nid}/read", response_model=NotificationOut)
async def mark_read(nid: uuid.UUID, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    n = await _own(session, user, nid)
    if n.read_at is None:
        n.read_at = utcnow()
        await session.commit()
    return _out(n)


@router.delete("/{nid}", status_code=204)
async def dismiss(nid: uuid.UUID, user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    n = await _own(session, user, nid)
    await session.execute(delete(Notification).where(Notification.id == n.id))
    await session.commit()


@router.post("/sweep", dependencies=[Depends(require_roles(Role.HR, Role.SUPERADMIN))])
async def expiry_sweep(request: Request, days: int = Query(30, ge=1, le=365),
                       user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Warns people whose documents or professional licence expire within `days`. Safe to run as often as you like
    (each warning is sent once per item), so it can be called by hand now and by a scheduler later."""
    horizon = date.today() + timedelta(days=days)
    sent = 0
    docs = (await session.execute(
        select(Document, Staff).join(Staff, Staff.id == Document.staff_id)
        .where(Document.deleted_at.is_(None), Document.status == "Verified", Document.expiry_date.is_not(None),
               Document.expiry_date <= horizon, Staff.deleted_at.is_(None)))).all()
    for d, s in docs:
        uid = await user_id_for_staff(session, s.id)
        if uid:
            left = (d.expiry_date - date.today()).days
            when = "has expired" if left < 0 else f"expires in {left} day(s)"
            sent += await notify(session, [uid], type="document", title="Document expiry",
                                 text=f"Your {d.name} {when}. Please upload a renewed copy.", link="/documents",
                                 entity_id=d.id, dedupe_key=f"doc-expiry:{d.id}:{d.expiry_date}")
    lics = (await session.execute(
        select(MedicalLicense, Staff).join(Staff, Staff.id == MedicalLicense.staff_id)
        .where(MedicalLicense.expires_on.is_not(None), MedicalLicense.expires_on <= horizon,
               Staff.deleted_at.is_(None)))).all()
    for lic, s in lics:
        uid = await user_id_for_staff(session, s.id)
        if uid:
            left = (lic.expires_on - date.today()).days
            when = "has expired" if left < 0 else f"expires in {left} day(s)"
            sent += await notify(session, [uid], type="license", title="Licence expiry",
                                 text=f"Your professional licence {when}. Please renew it and tell HR.", link="/profile",
                                 entity_id=lic.id, dedupe_key=f"license-expiry:{lic.id}:{lic.expires_on}")
    audit.record(session, user=user, action="expiry_sweep", entity="notification", after={"sent": sent, "days": days},
                 request=request)
    await session.commit()
    return {"sent": sent}
