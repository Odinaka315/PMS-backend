"""Queries (formal requests for a staff member to explain something).

Who can do what (placeholder rules; confirm with the hospital):
  * Issue:    an HOD to staff in their own department, HR or the director to anyone, never to yourself
  * See:      the person queried, their HOD, HR, director and super admin (same data scope as the staff record)
  * Reply:    only the person queried, while the query is waiting for a reply
  * Resolve:  HR or director, never the person queried and never whoever issued it; needs a reply, or the deadline passed
  * Appeal:   only the person queried, once, within QUERY_APPEAL_DAYS of an adverse outcome
  * Decide the appeal: director or super admin, never the person queried, the issuer or the resolver
  * Withdraw: HR or director, while still open, with a reason
Queries are never deleted. Every step is on the query's own timeline and in the audit log.
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import can_access_staff, get_accessible_staff, get_current_user, get_staff_or_404, require_roles
from app.core.roles import Role
from app.core.security import utcnow
from app.models import Staff, StaffQuery, User
from app.modules.notifications.service import notify, user_id_for_staff, users_with_role

from . import service as svc
from .schemas import (AppealDecisionIn, AppealIn, QueryCreate, QueryDetail, QueryPage, QuerySummary, ReplyIn, ResolveIn,
                      WithdrawIn)

router = APIRouter(tags=["queries"])

STATUSES = r"^(Pending Reply|Replied|Resolved|Appealed|Withdrawn)$"


async def _load(session: AsyncSession, user: User, qid: uuid.UUID) -> tuple[StaffQuery, Staff]:
    q = await session.scalar(select(StaffQuery).where(StaffQuery.id == qid).execution_options(populate_existing=True))
    s = await session.get(Staff, q.staff_id) if q else None
    if q is None or not can_access_staff(user, s):
        raise HTTPException(404, "Query not found")  # out of scope looks the same as missing
    return q, s


async def _tell_subject(session, q, **kw):
    uid = await user_id_for_staff(session, q.staff_id)
    if uid:
        await notify(session, [uid], type="query", link="/queries", entity_id=q.id, **kw)


@router.post("/queries", response_model=QueryDetail, status_code=201)
async def issue_query(body: QueryCreate, request: Request,
                      user: User = Depends(require_roles(Role.HOD, Role.HR, Role.DIRECTOR)),
                      session: AsyncSession = Depends(get_session)):
    target = await get_staff_or_404(session, body.staff)
    if not can_access_staff(user, target):
        raise HTTPException(404, "Staff member not found")  # an HOD cannot even confirm other departments exist
    if user.staff_id == target.id:
        raise HTTPException(422, "You cannot issue a query to yourself")
    due = body.reply_due or svc.default_due()
    if not (date.today() <= due <= date.today() + timedelta(days=30)):
        raise HTTPException(422, "The reply deadline must be within the next 30 days")
    q = StaffQuery(staff_id=target.id, title=body.title, description=body.description, severity=body.severity,
                   issued_by=user.id, reply_due=due)
    session.add(q)
    await session.flush()
    svc.add_event(session, q, user, "issued", body.description)
    audit.record(session, user=user, action="query_issued", entity="query", entity_id=q.id, subject_staff_id=target.id,
                 after={"title": body.title, "severity": body.severity, "reply_due": due}, request=request)
    await _tell_subject(session, q, title="A query has been issued to you",
                        text=f"Please reply by {due:%d %b %Y}.")
    await session.commit()
    return await svc.detail(session, user, q, target)


@router.get("/queries", response_model=QueryPage)
async def list_queries(status: str | None = Query(None, pattern=STATUSES), staff: str | None = None,
                       mine: bool = False, awaiting_me: bool = False, overdue: bool = False,
                       page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
                       user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    stmt = select(StaffQuery, Staff).join(Staff, Staff.id == StaffQuery.staff_id).where(svc.scope(user))
    if staff:
        stmt = stmt.where(StaffQuery.staff_id == (await get_accessible_staff(session, user, staff)).id)
    if mine:
        stmt = stmt.where(StaffQuery.staff_id == user.staff_id)
    if status:
        stmt = stmt.where(StaffQuery.status == status)
    if awaiting_me:
        stmt = stmt.where(svc.awaiting_filter(user))
    if overdue:
        stmt = stmt.where(StaffQuery.status == "Pending Reply", StaffQuery.reply_due < date.today())
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.execute(stmt.order_by(StaffQuery.created_at.desc(), StaffQuery.id)
                                  .offset((page - 1) * size).limit(size))).all()
    return QueryPage(items=await svc.many(session, user, rows), total=total or 0, page=page, size=size)


@router.get("/queries/summary", response_model=QuerySummary)
async def summary(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    base = select(func.count()).select_from(StaffQuery).where(svc.scope(user))

    async def count(*cond):
        return await session.scalar(base.where(*cond)) or 0

    return QuerySummary(
        pending_reply=await count(StaffQuery.status == "Pending Reply"),
        overdue=await count(StaffQuery.status == "Pending Reply", StaffQuery.reply_due < date.today()),
        replied=await count(StaffQuery.status == "Replied"), appealed=await count(StaffQuery.status == "Appealed"),
        resolved=await count(StaffQuery.status == "Resolved"), awaiting_me=await count(svc.awaiting_filter(user)))


@router.get("/staff/{ident}/queries", response_model=QueryPage)
async def staff_queries(ident: str, page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
                        user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The 'Queries' tab of a staff profile."""
    s = await get_accessible_staff(session, user, ident)
    stmt = select(StaffQuery, Staff).join(Staff, Staff.id == StaffQuery.staff_id).where(StaffQuery.staff_id == s.id)
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.execute(stmt.order_by(StaffQuery.created_at.desc()).offset((page - 1) * size).limit(size))).all()
    return QueryPage(items=await svc.many(session, user, rows), total=total or 0, page=page, size=size)


@router.get("/queries/{qid}", response_model=QueryDetail)
async def get_query(qid: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                    session: AsyncSession = Depends(get_session)):
    q, s = await _load(session, user, qid)
    if not svc.is_subject(user, q):  # reading someone else's disciplinary record is itself audited
        audit.record(session, user=user, action="query_viewed", entity="query", entity_id=q.id,
                     subject_staff_id=q.staff_id, request=request)
        await session.commit()
    return await svc.detail(session, user, q, s)


@router.post("/queries/{qid}/reply", response_model=QueryDetail)
async def reply(qid: uuid.UUID, body: ReplyIn, request: Request, user: User = Depends(get_current_user),
                session: AsyncSession = Depends(get_session)):
    q, s = await _load(session, user, qid)
    if not svc.is_subject(user, q):
        raise HTTPException(403, "Only the person the query was issued to can reply")
    if q.status != "Pending Reply":
        raise HTTPException(409, f"This query is {q.status.lower()}, so it cannot be replied to")
    q.status, q.replied_at = "Replied", utcnow()
    svc.add_event(session, q, user, "reply", body.text)
    audit.record(session, user=user, action="query_replied", entity="query", entity_id=q.id, subject_staff_id=q.staff_id,
                 after={"late": q.reply_due < date.today()}, request=request)
    ids = [q.issued_by, *await users_with_role(session, "hr")]
    await notify(session, ids, type="query", title="A query has been replied to", exclude=user.id,
                 text=f"{s.full_name} replied to a query and it is waiting for a decision.", link="/queries", entity_id=q.id)
    await session.commit()
    return await svc.detail(session, user, q, s)


@router.post("/queries/{qid}/resolve", response_model=QueryDetail)
async def resolve(qid: uuid.UUID, body: ResolveIn, request: Request,
                  user: User = Depends(require_roles(Role.HR, Role.DIRECTOR)),
                  session: AsyncSession = Depends(get_session)):
    q, s = await _load(session, user, qid)
    if svc.is_subject(user, q):
        raise HTTPException(403, "You cannot decide a query issued to you")
    if user.id == q.issued_by:
        raise HTTPException(403, "You cannot resolve a query you issued; another HR officer or the director must")
    if q.status not in svc.OPEN:
        raise HTTPException(409, f"This query is already {q.status.lower()}")
    if not svc.can_resolve(user, q):
        raise HTTPException(409, "The person has not replied yet and the reply deadline has not passed")
    q.status, q.outcome, q.resolved_by, q.resolved_at = "Resolved", body.outcome, user.id, utcnow()
    svc.add_event(session, q, user, "resolved", body.note, body.outcome)
    audit.record(session, user=user, action="query_resolved", entity="query", entity_id=q.id, subject_staff_id=q.staff_id,
                 after={"outcome": body.outcome}, request=request)
    extra = f" You may appeal within {svc.settings.query_appeal_days} days." if body.outcome in svc.ADVERSE else ""
    await _tell_subject(session, q, title="A query has been resolved", text="The outcome is now on your record." + extra)
    await notify(session, [q.issued_by], type="query", title="A query you issued has been resolved", exclude=user.id,
                 text="You can see the outcome on the query.", link="/queries", entity_id=q.id)
    await session.commit()
    return await svc.detail(session, user, q, s)


@router.post("/queries/{qid}/appeal", response_model=QueryDetail)
async def appeal(qid: uuid.UUID, body: AppealIn, request: Request, user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    q, s = await _load(session, user, qid)
    if not svc.is_subject(user, q):
        raise HTTPException(403, "Only the person the query was issued to can appeal")
    if q.appealed_at is not None:
        raise HTTPException(409, "This query has already been appealed")
    if q.status != "Resolved" or q.outcome not in svc.ADVERSE:
        raise HTTPException(409, "Only a resolved query with an adverse outcome can be appealed")
    if not svc.can_appeal(user, q):
        raise HTTPException(409, "The appeal period has ended")
    q.status, q.appealed_at = "Appealed", utcnow()
    svc.add_event(session, q, user, "appeal", body.grounds)
    audit.record(session, user=user, action="query_appealed", entity="query", entity_id=q.id, subject_staff_id=q.staff_id,
                 request=request)
    await notify(session, await users_with_role(session, "director"), type="query", title="A query decision was appealed",
                 text=f"{s.full_name} appealed the outcome of a query.", link="/queries", entity_id=q.id)
    await session.commit()
    return await svc.detail(session, user, q, s)


@router.post("/queries/{qid}/appeal-decision", response_model=QueryDetail)
async def decide_appeal(qid: uuid.UUID, body: AppealDecisionIn, request: Request,
                        user: User = Depends(require_roles(Role.DIRECTOR, Role.SUPERADMIN)),
                        session: AsyncSession = Depends(get_session)):
    q, s = await _load(session, user, qid)
    if q.status != "Appealed":
        raise HTTPException(409, "This query is not under appeal")
    if svc.is_subject(user, q) or user.id in (q.issued_by, q.resolved_by):
        raise HTTPException(403, "You cannot decide an appeal on a query you issued, resolved or received")
    q.status, q.appeal_outcome, q.appeal_decided_by = "Resolved", body.decision, user.id
    svc.add_event(session, q, user, "appeal_decision", body.note, body.decision)
    audit.record(session, user=user, action="query_appeal_decided", entity="query", entity_id=q.id,
                 subject_staff_id=q.staff_id, after={"decision": body.decision}, request=request)
    await _tell_subject(session, q, title="Your appeal has been decided", text=f"The appeal was {body.decision.lower()}.")
    await notify(session, [*await users_with_role(session, "hr"), q.issued_by], type="query", exclude=user.id,
                 title="An appeal has been decided", text=f"The appeal was {body.decision.lower()}.", link="/queries",
                 entity_id=q.id)
    await session.commit()
    return await svc.detail(session, user, q, s)


@router.post("/queries/{qid}/withdraw", response_model=QueryDetail)
async def withdraw(qid: uuid.UUID, body: WithdrawIn, request: Request,
                   user: User = Depends(require_roles(Role.HR, Role.DIRECTOR)),
                   session: AsyncSession = Depends(get_session)):
    q, s = await _load(session, user, qid)
    if svc.is_subject(user, q):
        raise HTTPException(403, "You cannot withdraw a query issued to you")
    if q.status not in svc.OPEN:
        raise HTTPException(409, f"This query is already {q.status.lower()}")
    q.status = "Withdrawn"
    svc.add_event(session, q, user, "withdrawn", body.reason)
    audit.record(session, user=user, action="query_withdrawn", entity="query", entity_id=q.id,
                 subject_staff_id=q.staff_id, request=request)
    await _tell_subject(session, q, title="A query has been withdrawn", text="You no longer need to reply to it.")
    await session.commit()
    return await svc.detail(session, user, q, s)
