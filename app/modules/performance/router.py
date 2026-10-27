"""Performance appraisals and goals.

Flow (placeholder rules; confirm with the hospital):
  HR creates a cycle (criteria and rating bands default to a placeholder form) and opens it; that creates an appraisal for
  every Active / On Leave staff member and assigns an appraiser (their supervisor, else their department's HOD; an HOD's or
  HR officer's appraiser is the director).
  Not Started -> the person self-assesses -> Self-Assessed -> the appraiser scores -> Reviewed
  -> HR or the director finalises (never the appraiser or the person) -> Finalised -> the person acknowledges.
  HR/director can send a Reviewed appraisal back to the appraiser.
The person appraised sees only their own self-assessment until the appraisal is Finalised; scores, rating and the
appraiser's comments are hidden from them before that. Scores are whole numbers 0-100 per criterion; the final score is
the weighted average, rated against the cycle's bands. Criteria and bands are frozen when the cycle opens.
"""
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import can_access_staff, get_accessible_staff, get_current_user, require_roles, staff_scope_clause
from app.core.roles import Role
from app.core.security import utcnow
from app.models import Appraisal, AppraisalCycle, PerformanceGoal, Staff, User
from app.modules.notifications.service import notify, user_id_for_staff, users_with_role

from . import rules
from .schemas import (AcknowledgeIn, AppraisalOut, AppraisalPage, AppraiserIn, CriterionScore, CycleIn, CycleOut,
                      DepartmentAverage, FinaliseIn, GoalCreate, GoalOut, GoalUpdate, HistoryRow, PerformanceSummary,
                      ReturnIn, ScoresIn, StatusCount)

router = APIRouter(prefix="/performance", tags=["performance"])

MANAGE = Depends(require_roles(Role.HR, Role.DIRECTOR, Role.SUPERADMIN))
PAID = ("Active", "On Leave")
STATUSES = r"^(Not Started|Self-Assessed|Reviewed|Finalised)$"


# ------------------------------------------------------------------ cycles
async def _cycle_out(session: AsyncSession, c: AppraisalCycle) -> CycleOut:
    n = await session.scalar(select(func.count()).select_from(Appraisal).where(Appraisal.cycle_id == c.id)) or 0
    done = await session.scalar(select(func.count()).select_from(Appraisal).where(
        Appraisal.cycle_id == c.id, Appraisal.status == "Finalised")) or 0
    return CycleOut(id=c.id, name=c.name, year=c.year, status=c.status, start_date=c.start_date, end_date=c.end_date,
                    self_deadline=c.self_deadline, criteria=c.criteria, rating_bands=c.rating_bands, appraisals=n,
                    finalised=done, opened_at=c.opened_at, closed_at=c.closed_at)


async def _cycle(session: AsyncSession, cid: uuid.UUID) -> AppraisalCycle:
    c = await session.get(AppraisalCycle, cid)
    if c is None:
        raise HTTPException(404, "Appraisal cycle not found")
    return c


@router.post("/cycles", response_model=CycleOut, status_code=201)
async def create_cycle(body: CycleIn, request: Request, user: User = Depends(require_roles(Role.HR)),
                       session: AsyncSession = Depends(get_session)):
    if await session.scalar(select(AppraisalCycle.id).where(AppraisalCycle.year == body.year,
                                                            AppraisalCycle.name == body.name)):
        raise HTTPException(409, "A cycle with this name already exists for that year")
    c = AppraisalCycle(name=body.name, year=body.year, start_date=body.start_date, end_date=body.end_date,
                       self_deadline=body.self_deadline, criteria=body.criteria_json(), rating_bands=body.bands_json(),
                       created_by=user.id)
    session.add(c)
    await session.flush()
    audit.record(session, user=user, action="appraisal_cycle_created", entity="appraisal_cycle", entity_id=c.id,
                 after={"name": c.name, "year": c.year}, request=request)
    await session.commit()
    return await _cycle_out(session, c)


@router.put("/cycles/{cid}", response_model=CycleOut)
async def update_cycle(cid: uuid.UUID, body: CycleIn, request: Request, user: User = Depends(require_roles(Role.HR)),
                       session: AsyncSession = Depends(get_session)):
    c = await _cycle(session, cid)
    if c.status != "Draft":
        raise HTTPException(409, "Only a draft cycle can be edited; its criteria are frozen once it opens")
    clash = await session.scalar(select(AppraisalCycle.id).where(
        AppraisalCycle.year == body.year, AppraisalCycle.name == body.name, AppraisalCycle.id != c.id))
    if clash:
        raise HTTPException(409, "A cycle with this name already exists for that year")
    c.name, c.year, c.start_date, c.end_date, c.self_deadline = body.name, body.year, body.start_date, body.end_date, body.self_deadline
    c.criteria, c.rating_bands = body.criteria_json(), body.bands_json()
    audit.record(session, user=user, action="appraisal_cycle_updated", entity="appraisal_cycle", entity_id=c.id,
                 after={"name": c.name, "year": c.year}, request=request)
    await session.commit()
    return await _cycle_out(session, c)


@router.get("/cycles", response_model=list[CycleOut], dependencies=[MANAGE])
async def list_cycles(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(AppraisalCycle).order_by(AppraisalCycle.year.desc(),
                                                                 AppraisalCycle.created_at.desc()))).all()
    return [await _cycle_out(session, c) for c in rows]


@router.get("/cycles/{cid}", response_model=CycleOut, dependencies=[MANAGE])
async def get_cycle(cid: uuid.UUID, session: AsyncSession = Depends(get_session)):
    return await _cycle_out(session, await _cycle(session, cid))


async def _appraiser_for(session: AsyncSession, s: Staff, subject_uid: uuid.UUID | None) -> uuid.UUID | None:
    """Supervisor if they can appraise, else the department's HOD; HODs and HR officers go to the director; a director
    to the super admin. HR can change it afterwards."""
    candidates: list[uuid.UUID | None] = []
    subject = await session.scalar(select(User.role).where(User.id == subject_uid)) if subject_uid else "staff"
    if subject in ("hod", "hr"):
        candidates += await users_with_role(session, "director")
    elif subject == "director":
        candidates += await users_with_role(session, "superadmin")
    else:
        if s.supervisor_id:
            sup = await session.scalar(select(User).where(User.staff_id == s.supervisor_id, User.is_active.is_(True)))
            if sup and sup.role in rules.APPRAISER_ROLES:
                candidates.append(sup.id)
        candidates += await users_with_role(session, "hod", department_id=s.department_id)
    return next((c for c in candidates if c and c != subject_uid), None)


async def _add_missing(session: AsyncSession, c: AppraisalCycle) -> int:
    have = set((await session.scalars(select(Appraisal.staff_id).where(Appraisal.cycle_id == c.id))).all())
    staff = (await session.scalars(select(Staff).where(Staff.deleted_at.is_(None), Staff.status.in_(PAID))
                                   .order_by(Staff.file_number))).unique().all()
    added, to_tell = 0, []
    for s in staff:
        if s.id in have:
            continue
        uid = await user_id_for_staff(session, s.id)
        session.add(Appraisal(cycle_id=c.id, staff_id=s.id, department=s.department.name,
                              appraiser_user_id=await _appraiser_for(session, s, uid)))
        added += 1
        if uid:
            to_tell.append(uid)
    await notify(session, to_tell, type="performance", title="Appraisal open",
                 text=f"Please complete your self-assessment for {c.name} by {c.self_deadline:%d %b %Y}.",
                 link="/performance", entity_id=c.id, dedupe_key=f"appraisal-open:{c.id}")
    await session.flush()
    return added


@router.post("/cycles/{cid}/open", response_model=CycleOut)
async def open_cycle(cid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR)),
                     session: AsyncSession = Depends(get_session)):
    c = await _cycle(session, cid)
    if c.status != "Draft":
        raise HTTPException(409, f"This cycle is already {c.status.lower()}")
    c.status, c.opened_at = "Open", utcnow()
    added = await _add_missing(session, c)
    if added == 0:
        raise HTTPException(409, "There is no one to appraise")
    audit.record(session, user=user, action="appraisal_cycle_opened", entity="appraisal_cycle", entity_id=c.id,
                 after={"appraisals": added}, request=request)
    await session.commit()
    return await _cycle_out(session, c)


@router.post("/cycles/{cid}/sync", response_model=CycleOut)
async def sync_cycle(cid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR)),
                     session: AsyncSession = Depends(get_session)):
    """Adds appraisals for people who joined after the cycle opened."""
    c = await _cycle(session, cid)
    if c.status != "Open":
        raise HTTPException(409, "Only an open cycle can be synchronised")
    added = await _add_missing(session, c)
    audit.record(session, user=user, action="appraisal_cycle_synced", entity="appraisal_cycle", entity_id=c.id,
                 after={"added": added}, request=request)
    await session.commit()
    return await _cycle_out(session, c)


@router.post("/cycles/{cid}/close", response_model=CycleOut)
async def close_cycle(cid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR)),
                      session: AsyncSession = Depends(get_session)):
    c = await _cycle(session, cid)
    if c.status != "Open":
        raise HTTPException(409, "Only an open cycle can be closed")
    c.status, c.closed_at = "Closed", utcnow()
    out = await _cycle_out(session, c)
    audit.record(session, user=user, action="appraisal_cycle_closed", entity="appraisal_cycle", entity_id=c.id,
                 after={"finalised": out.finalised, "of": out.appraisals}, request=request)
    await session.commit()
    return out


# ------------------------------------------------------------------ appraisals
def _is_subject(user: User, a: Appraisal) -> bool:
    return user.staff_id is not None and user.staff_id == a.staff_id


def _perms(user: User, a: Appraisal, c: AppraisalCycle) -> dict[str, bool]:
    open_ = c.status == "Open"
    subject = _is_subject(user, a)
    senior = user.role in (Role.HR, Role.DIRECTOR)
    return {
        "can_self_assess": subject and open_ and a.status in ("Not Started", "Self-Assessed"),
        "can_review": (open_ and not subject and a.appraiser_user_id == user.id and (
            a.status in ("Self-Assessed", "Reviewed") or (a.status == "Not Started" and date.today() > c.self_deadline))),
        "can_finalise": open_ and senior and not subject and a.status == "Reviewed" and user.id != a.reviewed_by,
        "can_acknowledge": subject and a.status == "Finalised" and a.acknowledged_at is None,
    }


def _visible(user: User):
    return or_(Appraisal.staff_id.in_(select(Staff.id).where(staff_scope_clause(user))),
               Appraisal.appraiser_user_id == user.id)


def _can_view(user: User, a: Appraisal, s: Staff) -> bool:
    return can_access_staff(user, s) or a.appraiser_user_id == user.id


async def _out(session: AsyncSession, user: User, a: Appraisal, c: AppraisalCycle, s: Staff, who: dict | None = None):
    if who is None:
        who = {i: e for i, e in await session.execute(select(User.id, User.email).where(User.id == a.appraiser_user_id))}
    hide = _is_subject(user, a) and a.status != "Finalised"  # the person appraised sees scores only once finalised
    mine, theirs = a.self_scores or {}, (None if hide else a.appraiser_scores) or {}
    return AppraisalOut(
        id=a.id, cycle_id=c.id, cycle=c.name, year=c.year, cycle_status=c.status, self_deadline=c.self_deadline,
        staff_id=s.id, file_number=s.file_number, staff_name=s.full_name, department=a.department,
        appraiser=who.get(a.appraiser_user_id), status=a.status,
        criteria=[CriterionScore(key=k["key"], name=k["name"], weight=k["weight"], self_score=mine.get(k["key"]),
                                 appraiser_score=theirs.get(k["key"])) for k in c.criteria],
        self_comment=a.self_comment, self_submitted_at=a.self_submitted_at,
        appraiser_comment=None if hide else a.appraiser_comment,
        score=None if hide or a.score_x100 is None else a.score_x100 / 100, rating=None if hide else a.rating,
        reviewed_at=None if hide else a.reviewed_at, finalised_at=a.finalised_at, finalise_comment=a.finalise_comment,
        return_reason=None if _is_subject(user, a) else a.return_reason, acknowledged_at=a.acknowledged_at,
        staff_comment=a.staff_comment, **_perms(user, a, c))


async def _load(session: AsyncSession, user: User, aid: uuid.UUID):
    a = await session.scalar(select(Appraisal).where(Appraisal.id == aid).execution_options(populate_existing=True))
    s = await session.get(Staff, a.staff_id) if a else None
    if a is None or not _can_view(user, a, s):
        raise HTTPException(404, "Appraisal not found")
    return a, await session.get(AppraisalCycle, a.cycle_id), s


@router.get("/appraisals", response_model=AppraisalPage)
async def list_appraisals(cycle: uuid.UUID | None = None, status: str | None = Query(None, pattern=STATUSES),
                          staff: str | None = None, mine: bool = False,
                          awaiting_me: bool = Query(False, description="Appraisals where the caller has to act next"),
                          page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
                          user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    stmt = (select(Appraisal, AppraisalCycle, Staff).join(AppraisalCycle, AppraisalCycle.id == Appraisal.cycle_id)
            .join(Staff, Staff.id == Appraisal.staff_id).where(_visible(user), AppraisalCycle.status != "Draft"))
    if cycle:
        stmt = stmt.where(Appraisal.cycle_id == cycle)
    if status:
        stmt = stmt.where(Appraisal.status == status)
    if staff:
        stmt = stmt.where(Appraisal.staff_id == (await get_accessible_staff(session, user, staff)).id)
    if mine:
        stmt = stmt.where(Appraisal.staff_id == user.staff_id)
    if awaiting_me:
        parts = [(Appraisal.appraiser_user_id == user.id) & (Appraisal.staff_id != user.staff_id)
                 & ((Appraisal.status == "Self-Assessed")
                    | ((Appraisal.status == "Not Started") & (AppraisalCycle.self_deadline < date.today())))]
        if user.staff_id:
            parts.append((Appraisal.staff_id == user.staff_id) & (Appraisal.status == "Not Started"))
            parts.append((Appraisal.staff_id == user.staff_id) & (Appraisal.status == "Finalised")
                         & Appraisal.acknowledged_at.is_(None))
        if user.role in (Role.HR, Role.DIRECTOR):
            parts.append((Appraisal.status == "Reviewed") & (Appraisal.reviewed_by != user.id)
                         & (Appraisal.staff_id != (user.staff_id or uuid.UUID(int=0))))
        stmt = stmt.where(or_(*parts), AppraisalCycle.status == "Open")
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.execute(stmt.order_by(AppraisalCycle.year.desc(), Staff.file_number)
                                  .offset((page - 1) * size).limit(size))).all()
    appraisers = {a.appraiser_user_id for a, _, _ in rows if a.appraiser_user_id}
    who = {i: e for i, e in await session.execute(select(User.id, User.email).where(User.id.in_(appraisers)))} \
        if appraisers else {}
    return AppraisalPage(items=[await _out(session, user, a, c, s, who) for a, c, s in rows], total=total or 0,
                         page=page, size=size)


@router.get("/appraisals/{aid}", response_model=AppraisalOut)
async def get_appraisal(aid: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    a, c, s = await _load(session, user, aid)
    if c.status == "Draft":
        raise HTTPException(404, "Appraisal not found")
    if not _is_subject(user, a):
        audit.record(session, user=user, action="appraisal_viewed", entity="appraisal", entity_id=a.id,
                     subject_staff_id=a.staff_id, request=request)
        await session.commit()
    return await _out(session, user, a, c, s)


@router.post("/appraisals/{aid}/self-assessment", response_model=AppraisalOut)
async def self_assess(aid: uuid.UUID, body: ScoresIn, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    a, c, s = await _load(session, user, aid)
    if not _is_subject(user, a):
        raise HTTPException(403, "Only the person being appraised can submit their self-assessment")
    if c.status != "Open":
        raise HTTPException(409, "This appraisal cycle is not open")
    if a.status not in ("Not Started", "Self-Assessed"):
        raise HTTPException(409, "The appraiser has already started, so the self-assessment can no longer change")
    a.self_scores, a.self_comment = rules.validate_scores(body.scores, c.criteria), body.comment
    a.self_submitted_at, a.status = utcnow(), "Self-Assessed"
    audit.record(session, user=user, action="appraisal_self_assessed", entity="appraisal", entity_id=a.id,
                 subject_staff_id=a.staff_id, request=request)
    if a.appraiser_user_id:
        await notify(session, [a.appraiser_user_id], type="performance", title="Appraisal ready for your review",
                     text=f"{s.full_name} submitted a self-assessment for {c.name}.", link="/performance", entity_id=a.id)
    await session.commit()
    return await _out(session, user, a, c, s)


@router.post("/appraisals/{aid}/review", response_model=AppraisalOut)
async def review(aid: uuid.UUID, body: ScoresIn, request: Request, user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    a, c, s = await _load(session, user, aid)
    if _is_subject(user, a):
        raise HTTPException(403, "You cannot appraise yourself")
    if a.appraiser_user_id != user.id:
        raise HTTPException(403, "You are not the appraiser for this appraisal")
    if c.status != "Open":
        raise HTTPException(409, "This appraisal cycle is not open")
    if not _perms(user, a, c)["can_review"]:
        raise HTTPException(409, "It is not ready for review: the self-assessment is missing and its deadline has not passed"
                            if a.status == "Not Started" else f"This appraisal is already {a.status.lower()}")
    a.appraiser_scores = rules.validate_scores(body.scores, c.criteria)
    a.appraiser_comment, a.reviewed_by, a.reviewed_at, a.status = body.comment, user.id, utcnow(), "Reviewed"
    a.score_x100 = rules.weighted_x100(a.appraiser_scores, c.criteria)
    a.rating, a.return_reason = rules.rating_for(a.score_x100, c.rating_bands), None
    audit.record(session, user=user, action="appraisal_reviewed", entity="appraisal", entity_id=a.id,
                 subject_staff_id=a.staff_id, request=request)
    await notify(session, [*await users_with_role(session, "hr"), *await users_with_role(session, "director")],
                 type="performance", title="Appraisal awaiting finalisation", exclude=user.id,
                 text=f"The appraisal of {s.full_name} for {c.name} has been scored.", link="/performance", entity_id=a.id)
    await session.commit()
    return await _out(session, user, a, c, s)


@router.post("/appraisals/{aid}/finalise", response_model=AppraisalOut)
async def finalise(aid: uuid.UUID, body: FinaliseIn, request: Request,
                   user: User = Depends(require_roles(Role.HR, Role.DIRECTOR)),
                   session: AsyncSession = Depends(get_session)):
    a, c, s = await _load(session, user, aid)
    if _is_subject(user, a):
        raise HTTPException(403, "You cannot finalise your own appraisal")
    if a.reviewed_by == user.id:
        raise HTTPException(403, "You scored this appraisal, so someone else must finalise it")
    if c.status != "Open":
        raise HTTPException(409, "This appraisal cycle is not open")
    if a.status != "Reviewed":
        raise HTTPException(409, "Only an appraisal that has been scored can be finalised")
    a.status, a.finalised_by, a.finalised_at, a.finalise_comment = "Finalised", user.id, utcnow(), body.comment
    audit.record(session, user=user, action="appraisal_finalised", entity="appraisal", entity_id=a.id,
                 subject_staff_id=a.staff_id, after={"rating": a.rating, "score": a.score_x100 / 100}, request=request)
    uid = await user_id_for_staff(session, a.staff_id)
    if uid:
        await notify(session, [uid], type="performance", title="Your appraisal is final",
                     text=f"Your {c.name} appraisal is ready. Please read and acknowledge it.", link="/performance",
                     entity_id=a.id)
    await session.commit()
    return await _out(session, user, a, c, s)


@router.post("/appraisals/{aid}/return", response_model=AppraisalOut)
async def send_back(aid: uuid.UUID, body: ReturnIn, request: Request,
                    user: User = Depends(require_roles(Role.HR, Role.DIRECTOR)),
                    session: AsyncSession = Depends(get_session)):
    a, c, s = await _load(session, user, aid)
    if _is_subject(user, a) or a.reviewed_by == user.id:
        raise HTTPException(403, "You cannot send back your own appraisal or one you scored")
    if c.status != "Open" or a.status != "Reviewed":
        raise HTTPException(409, "Only a scored appraisal in an open cycle can be sent back")
    a.status, a.return_reason = "Self-Assessed", body.reason
    a.score_x100 = a.rating = None  # the score is not final, so it is not kept
    audit.record(session, user=user, action="appraisal_returned", entity="appraisal", entity_id=a.id,
                 subject_staff_id=a.staff_id, request=request)
    if a.appraiser_user_id:
        await notify(session, [a.appraiser_user_id], type="performance", title="Appraisal sent back to you",
                     text="Please review the reason and score it again.", link="/performance", entity_id=a.id)
    await session.commit()
    return await _out(session, user, a, c, s)


@router.post("/appraisals/{aid}/acknowledge", response_model=AppraisalOut)
async def acknowledge(aid: uuid.UUID, body: AcknowledgeIn, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    a, c, s = await _load(session, user, aid)
    if not _is_subject(user, a):
        raise HTTPException(403, "Only the person appraised can acknowledge it")
    if a.status != "Finalised":
        raise HTTPException(409, "The appraisal is not final yet")
    if a.acknowledged_at is not None:
        raise HTTPException(409, "You have already acknowledged this appraisal")
    a.acknowledged_at, a.staff_comment = utcnow(), (body.comment or "").strip() or None
    audit.record(session, user=user, action="appraisal_acknowledged", entity="appraisal", entity_id=a.id,
                 subject_staff_id=a.staff_id, request=request)
    await session.commit()
    return await _out(session, user, a, c, s)


@router.put("/appraisals/{aid}/appraiser", response_model=AppraisalOut)
async def set_appraiser(aid: uuid.UUID, body: AppraiserIn, request: Request, user: User = Depends(require_roles(Role.HR)),
                        session: AsyncSession = Depends(get_session)):
    a, c, s = await _load(session, user, aid)
    if c.status != "Open" or a.status in ("Reviewed", "Finalised"):
        raise HTTPException(409, "The appraiser can only be changed before the appraisal is scored")
    new = await session.get(User, body.user_id)
    if new is None or not new.is_active or new.role not in rules.APPRAISER_ROLES:
        raise HTTPException(422, "The appraiser must be an active HOD, HR, director or super admin account")
    if new.staff_id == a.staff_id:
        raise HTTPException(422, "Nobody can appraise themselves")
    a.appraiser_user_id = new.id
    audit.record(session, user=user, action="appraiser_assigned", entity="appraisal", entity_id=a.id,
                 subject_staff_id=a.staff_id, after={"appraiser": new.email}, request=request)
    await notify(session, [new.id], type="performance", title="You have been made an appraiser",
                 text=f"You will appraise {s.full_name} for {c.name}.", link="/performance", entity_id=a.id, exclude=user.id)
    await session.commit()
    return await _out(session, user, a, c, s)


# ------------------------------------------------------------------ history and summary
@router.get("/history", response_model=list[HistoryRow])
async def history(staff: str | None = Query(None, description="UUID or file number; defaults to you"),
                  user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Finalised results over the years (for the performance chart and, later, promotion eligibility)."""
    if staff is None:
        if user.staff_id is None:
            raise HTTPException(404, "This account has no staff record")
        sid = user.staff_id
    else:
        sid = (await get_accessible_staff(session, user, staff)).id
    rows = (await session.execute(
        select(Appraisal, AppraisalCycle).join(AppraisalCycle, AppraisalCycle.id == Appraisal.cycle_id)
        .where(Appraisal.staff_id == sid, Appraisal.status == "Finalised").order_by(AppraisalCycle.year, AppraisalCycle.name))).all()
    return [HistoryRow(cycle_id=c.id, cycle=c.name, year=c.year, score=a.score_x100 / 100, rating=a.rating,
                       finalised_at=a.finalised_at) for a, c in rows]


@router.get("/summary", response_model=PerformanceSummary)
async def summary(cycle: uuid.UUID | None = None,
                  user: User = Depends(require_roles(Role.HOD, Role.HR, Role.DIRECTOR, Role.SUPERADMIN)),
                  session: AsyncSession = Depends(get_session)):
    """Progress and results for one cycle (the latest opened one by default), within the caller's data scope."""
    if cycle:
        c = await _cycle(session, cycle)
    else:
        c = await session.scalar(select(AppraisalCycle).where(AppraisalCycle.status != "Draft")
                                 .order_by(AppraisalCycle.year.desc(), AppraisalCycle.created_at.desc()).limit(1))
    if c is None or c.status == "Draft":
        return PerformanceSummary(cycle=None, by_status=[], ratings=[], average_score=None, by_department=[])
    base = (select(Appraisal).where(Appraisal.cycle_id == c.id,
                                    Appraisal.staff_id.in_(select(Staff.id).where(staff_scope_clause(user)))).subquery())
    by_status = (await session.execute(select(base.c.status, func.count()).group_by(base.c.status))).all()
    order = ["Not Started", "Self-Assessed", "Reviewed", "Finalised"]
    ratings = (await session.execute(select(base.c.rating, func.count()).where(base.c.status == "Finalised")
                                     .group_by(base.c.rating))).all()
    avg = await session.scalar(select(func.avg(base.c.score_x100)).where(base.c.status == "Finalised"))
    done = case((base.c.status == "Finalised", 1), else_=0)
    depts = (await session.execute(
        select(base.c.department, func.count(), func.sum(done),
               func.avg(case((base.c.status == "Finalised", base.c.score_x100)))).group_by(base.c.department)
        .order_by(base.c.department))).all()
    labels = [b["label"] for b in sorted(c.rating_bands, key=lambda b: -b["min"])]
    got = dict(ratings)
    return PerformanceSummary(
        cycle={"id": str(c.id), "name": c.name, "year": c.year, "status": c.status},
        by_status=[StatusCount(status=s, count=dict(by_status).get(s, 0)) for s in order],
        ratings=[StatusCount(status=r, count=got.get(r, 0)) for r in labels],
        average_score=None if avg is None else round(float(avg) / 100, 2),
        by_department=[DepartmentAverage(department=d, appraisals=n, finalised=int(f or 0),
                                         average=None if av is None else round(float(av) / 100, 2))
                       for d, n, f, av in depts])


# ------------------------------------------------------------------ goals
def _goal_perms(user: User, g: PerformanceGoal, s: Staff) -> tuple[bool, bool]:
    owner = user.staff_id == g.staff_id
    creator = g.created_by == user.id
    return owner or creator or user.role == Role.HR, creator or user.role == Role.HR


async def _goal_out(session: AsyncSession, user: User, g: PerformanceGoal, s: Staff, who: dict | None = None) -> GoalOut:
    if who is None:
        who = {i: e for i, e in await session.execute(select(User.id, User.email).where(User.id == g.created_by))}
    can_edit, can_delete = _goal_perms(user, g, s)
    return GoalOut(id=g.id, staff_id=g.staff_id, file_number=s.file_number, title=g.title, target=g.target,
                   progress=g.progress, status=g.status, due_date=g.due_date, cycle_id=g.cycle_id,
                   created_by=who.get(g.created_by), can_edit=can_edit, can_delete=can_delete)


async def _goal(session: AsyncSession, user: User, gid: uuid.UUID):
    g = await session.scalar(select(PerformanceGoal).where(PerformanceGoal.id == gid)
                             .execution_options(populate_existing=True))
    s = await session.get(Staff, g.staff_id) if g else None
    if g is None or not can_access_staff(user, s):
        raise HTTPException(404, "Goal not found")
    return g, s


@router.post("/goals", response_model=GoalOut, status_code=201)
async def create_goal(body: GoalCreate, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    if body.staff is None:
        if user.staff_id is None:
            raise HTTPException(400, "This account has no staff record")
        s = await session.get(Staff, user.staff_id)
    else:
        s = await get_accessible_staff(session, user, body.staff)
    if s.id != user.staff_id and user.role not in (Role.HOD, Role.HR):
        raise HTTPException(403, "You can only set goals for yourself")  # HODs are limited to their department by scope
    if body.cycle_id and await session.get(AppraisalCycle, body.cycle_id) is None:
        raise HTTPException(422, "Unknown appraisal cycle")
    g = PerformanceGoal(staff_id=s.id, cycle_id=body.cycle_id, title=body.title, target=body.target,
                        due_date=body.due_date, created_by=user.id)
    session.add(g)
    await session.flush()
    audit.record(session, user=user, action="goal_created", entity="goal", entity_id=g.id, subject_staff_id=s.id,
                 after={"title": g.title}, request=request)
    if s.id != user.staff_id:
        uid = await user_id_for_staff(session, s.id)
        if uid:
            await notify(session, [uid], type="performance", title="A goal was set for you", text=g.title[:100],
                         link="/performance", entity_id=g.id)
    await session.commit()
    return await _goal_out(session, user, g, s)


@router.get("/goals", response_model=list[GoalOut])
async def list_goals(staff: str | None = Query(None, description="UUID or file number; defaults to you"),
                     status: str | None = Query(None, pattern=r"^(Active|Achieved|Dropped)$"),
                     user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if staff is None:
        if user.staff_id is None:
            return []
        s = await session.get(Staff, user.staff_id)
    else:
        s = await get_accessible_staff(session, user, staff)
    stmt = select(PerformanceGoal).where(PerformanceGoal.staff_id == s.id)
    if status:
        stmt = stmt.where(PerformanceGoal.status == status)
    rows = (await session.scalars(stmt.order_by(PerformanceGoal.created_at.desc()).limit(200))).all()
    return [await _goal_out(session, user, g, s) for g in rows]


@router.patch("/goals/{gid}", response_model=GoalOut)
async def update_goal(gid: uuid.UUID, body: GoalUpdate, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    g, s = await _goal(session, user, gid)
    if not _goal_perms(user, g, s)[0]:
        raise HTTPException(403, "You cannot change this goal")
    before = {"progress": g.progress, "status": g.status}
    for field in ("title", "target", "due_date"):
        if field in body.model_fields_set:
            setattr(g, field, getattr(body, field))
    if body.progress is not None:
        g.progress = body.progress
    if body.status is not None:
        g.status = body.status
    if g.status == "Achieved":
        g.progress = 100
    elif body.progress == 100 and body.status is None and g.status == "Active":
        g.status = "Achieved"
    audit.record(session, user=user, action="goal_updated", entity="goal", entity_id=g.id, subject_staff_id=g.staff_id,
                 before=before, after={"progress": g.progress, "status": g.status}, request=request)
    await session.commit()
    return await _goal_out(session, user, g, s)


@router.delete("/goals/{gid}", status_code=204)
async def delete_goal(gid: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    g, s = await _goal(session, user, gid)
    if not _goal_perms(user, g, s)[1]:
        raise HTTPException(403, "You cannot delete this goal")
    audit.record(session, user=user, action="goal_deleted", entity="goal", entity_id=g.id, subject_staff_id=g.staff_id,
                 before={"title": g.title}, request=request)
    await session.delete(g)
    await session.commit()
