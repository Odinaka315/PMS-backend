import uuid
from datetime import date, timedelta

from sqlalchemy import and_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import staff_scope_clause
from app.core.roles import Role
from app.models import QueryEvent, Staff, StaffQuery, User

from .schemas import EventOut, QueryDetail, QueryOut

OPEN = ("Pending Reply", "Replied")
ADVERSE = ("Warning", "Reprimand", "Further Action")


def effective_outcome(q: StaffQuery) -> str | None:
    return "Cleared" if q.appeal_outcome == "Overturned" else q.outcome


def appeal_open_until(q: StaffQuery) -> date | None:
    if q.status != "Resolved" or q.appealed_at is not None or q.outcome not in ADVERSE or q.resolved_at is None:
        return None
    return q.resolved_at.date() + timedelta(days=settings.query_appeal_days)


def is_overdue(q: StaffQuery) -> bool:
    return q.status == "Pending Reply" and q.reply_due < date.today()


def is_subject(user: User, q: StaffQuery) -> bool:
    return user.staff_id is not None and user.staff_id == q.staff_id


def can_reply(user: User, q: StaffQuery) -> bool:
    return is_subject(user, q) and q.status == "Pending Reply"


def can_resolve(user: User, q: StaffQuery) -> bool:
    """HR or the director decides the outcome: never the person queried, and never the person who issued it."""
    return (user.role in (Role.HR, Role.DIRECTOR) and not is_subject(user, q) and user.id != q.issued_by
            and (q.status == "Replied" or is_overdue(q)))


def can_appeal(user: User, q: StaffQuery) -> bool:
    until = appeal_open_until(q)
    return is_subject(user, q) and until is not None and date.today() <= until


def can_decide_appeal(user: User, q: StaffQuery) -> bool:
    return (user.role in (Role.DIRECTOR, Role.SUPERADMIN) and q.status == "Appealed" and not is_subject(user, q)
            and user.id not in (q.issued_by, q.resolved_by))


def can_withdraw(user: User, q: StaffQuery) -> bool:
    return user.role in (Role.HR, Role.DIRECTOR) and q.status in OPEN and not is_subject(user, q)


def scope(user: User):
    return StaffQuery.staff_id.in_(select(Staff.id).where(staff_scope_clause(user)))


def awaiting_filter(user: User):
    """Queries where the caller is the one who has to act next."""
    today = date.today()
    not_subject = StaffQuery.staff_id != user.staff_id if user.staff_id else true()
    parts = []
    if user.staff_id:
        parts.append(and_(StaffQuery.staff_id == user.staff_id, StaffQuery.status == "Pending Reply"))
    if user.role in (Role.HR, Role.DIRECTOR):
        parts.append(and_(not_subject, StaffQuery.issued_by != user.id, or_(
            StaffQuery.status == "Replied", and_(StaffQuery.status == "Pending Reply", StaffQuery.reply_due < today))))
    if user.role in (Role.DIRECTOR, Role.SUPERADMIN):
        parts.append(and_(not_subject, StaffQuery.status == "Appealed", StaffQuery.issued_by != user.id,
                          StaffQuery.resolved_by != user.id))
    return or_(*parts) if parts else StaffQuery.id.is_(None)


async def emails(session: AsyncSession, ids) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    return {i: e for i, e in await session.execute(select(User.id, User.email).where(User.id.in_(wanted)))}


def to_out(user: User, q: StaffQuery, s: Staff, who: dict) -> QueryOut:
    return QueryOut(
        id=q.id, staff_id=s.id, file_number=s.file_number, staff_name=s.full_name, department=s.department.name,
        title=q.title, description=q.description, severity=q.severity, status=q.status, issued_by=who.get(q.issued_by),
        issued_at=q.created_at, reply_due=q.reply_due, overdue=is_overdue(q), replied_at=q.replied_at,
        outcome=effective_outcome(q), resolved_by=who.get(q.resolved_by), resolved_at=q.resolved_at,
        appealed_at=q.appealed_at, appeal_outcome=q.appeal_outcome, appeal_open_until=appeal_open_until(q),
        can_reply=can_reply(user, q), can_resolve=can_resolve(user, q), can_appeal=can_appeal(user, q),
        can_decide_appeal=can_decide_appeal(user, q), can_withdraw=can_withdraw(user, q))


async def many(session: AsyncSession, user: User, rows) -> list[QueryOut]:
    who = await emails(session, [x for q, _ in rows for x in (q.issued_by, q.resolved_by)])
    return [to_out(user, q, s, who) for q, s in rows]


async def detail(session: AsyncSession, user: User, q: StaffQuery, s: Staff) -> QueryDetail:
    who = await emails(session, [q.issued_by, q.resolved_by])
    events = (await session.scalars(select(QueryEvent).where(QueryEvent.query_id == q.id).order_by(QueryEvent.at))).all()
    return QueryDetail(**to_out(user, q, s, who).model_dump(),
                       events=[EventOut(kind=e.kind, by=e.by_email, role=e.by_role, text=e.text, detail=e.detail, at=e.at)
                               for e in events])


def add_event(session: AsyncSession, q: StaffQuery, user: User, kind: str, text: str, detail: str | None = None) -> None:
    session.add(QueryEvent(query_id=q.id, kind=kind, by_user=user.id, by_email=user.email, by_role=user.role,
                           text=text, detail=detail))


def default_due() -> date:
    return date.today() + timedelta(days=settings.query_reply_days)
