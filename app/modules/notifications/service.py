"""Creating notifications. Everything here only adds rows to the caller's transaction, so a notification exists
if and only if the change that caused it was saved."""
import uuid
from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import utcnow
from app.models import Notification, Staff, User


async def notify(session: AsyncSession, user_ids: Iterable[uuid.UUID], *, type: str, title: str, text: str,
                 link: str | None = None, entity_id: uuid.UUID | None = None, dedupe_key: str | None = None,
                 exclude: uuid.UUID | None = None) -> int:
    """Tell each user (once each). With a dedupe_key, anyone already told this exact thing is skipped.
    `exclude` is the person who caused the event: they do not need to be told about their own action."""
    ids = {u for u in user_ids if u != exclude}
    if not ids:
        return 0
    if dedupe_key:
        seen = await session.scalars(select(Notification.user_id).where(
            Notification.dedupe_key == dedupe_key, Notification.user_id.in_(ids)))
        ids -= set(seen)
    now = utcnow()
    session.add_all([
        Notification(user_id=u, type=type, title=title[:150], text=text, link=link, entity_id=entity_id,
                     dedupe_key=dedupe_key, created_at=now)
        for u in ids])
    return len(ids)


async def users_with_role(session: AsyncSession, role: str, *, department_id: uuid.UUID | None = None) -> list[uuid.UUID]:
    """Active accounts holding a role; for an HOD, only the ones heading that department."""
    stmt = select(User.id).where(User.role == role, User.is_active.is_(True))
    if role == "hod":
        if department_id is None:
            return []
        stmt = stmt.join(Staff, Staff.id == User.staff_id).where(Staff.department_id == department_id)
    return list((await session.scalars(stmt)).all())


async def user_id_for_staff(session: AsyncSession, staff_id: uuid.UUID) -> uuid.UUID | None:
    return await session.scalar(select(User.id).where(User.staff_id == staff_id, User.is_active.is_(True)))
