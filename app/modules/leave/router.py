import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import can_access_staff, get_current_user, get_staff_or_404
from app.core.security import utcnow  # noqa: F401
from app.models import LeaveRequest, LeaveType, User

from . import service
from .schemas import BalanceOut, DecisionIn, LeaveCreate, LeaveOut, LeavePage, LeaveTypeOut

router = APIRouter(prefix="/leave", tags=["leave"])


@router.get("/types", response_model=list[LeaveTypeOut])
async def leave_types(_: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name))).all()
    return [LeaveTypeOut(id=t.id, code=t.code, name=t.name, annual_entitlement_days=t.annual_entitlement_days,
                         allow_backdate=t.allow_backdate) for t in rows]


@router.get("/balance", response_model=BalanceOut)
async def leave_balance(staff: str | None = Query(None, description="UUID or file number; defaults to you"),
                        year: int | None = None, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    if staff is None:
        if user.staff_id is None:
            raise HTTPException(404, "This account has no staff record")
        target_id = user.staff_id
    else:
        s = await get_staff_or_404(session, staff)
        if not can_access_staff(user, s):
            raise HTTPException(404, "Staff member not found")
        target_id = s.id
    return await service.balances(session, target_id, year or date.today().year)


@router.post("/requests", response_model=LeaveOut, status_code=201)
async def create_request(body: LeaveCreate, request: Request, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    req = await service.create_request(session, user, body, request)
    await session.commit()
    req = await _load(session, req.id)
    return service.to_out(req, await service.workflow_for(session, req.leave_type_id))


@router.get("/requests", response_model=LeavePage)
async def list_requests(
    status: str | None = Query(None, pattern=r"^(Pending|Approved|Declined|Cancelled)$"),
    mine: bool = False, awaiting_me: bool = Query(False, description="Only requests waiting on the caller's approval"),
    page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
    user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session),
):
    cond = service.visible_filter(user)
    stmt = select(LeaveRequest).where(cond)
    if mine:
        stmt = stmt.where(LeaveRequest.staff_id == user.staff_id)
    if awaiting_me:
        stmt = stmt.where(service.awaiting_filter(user))
    if status:
        stmt = stmt.where(LeaveRequest.status == status)
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.scalars(stmt.order_by(LeaveRequest.created_at.desc())
                                  .offset((page - 1) * size).limit(size))).unique().all()
    cache: dict = {}
    items = []
    for r in rows:
        if r.leave_type_id not in cache:
            cache[r.leave_type_id] = await service.workflow_for(session, r.leave_type_id)
        items.append(service.to_out(r, cache[r.leave_type_id]))
    return LeavePage(items=items, total=total or 0, page=page, size=size)


async def _load(session: AsyncSession, request_id: uuid.UUID) -> LeaveRequest:
    req = await session.scalar(select(LeaveRequest).where(LeaveRequest.id == request_id)
                               .execution_options(populate_existing=True))
    if req is None:
        raise HTTPException(404, "Leave request not found")
    return req


async def _load_visible(session: AsyncSession, user: User, request_id: uuid.UUID) -> LeaveRequest:
    req = await _load(session, request_id)
    if not can_access_staff(user, req.staff):
        raise HTTPException(404, "Leave request not found")
    return req


@router.get("/requests/{request_id}", response_model=LeaveOut)
async def get_request(request_id: uuid.UUID, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    req = await _load_visible(session, user, request_id)
    return service.to_out(req, await service.workflow_for(session, req.leave_type_id))


@router.post("/requests/{request_id}/decision", response_model=LeaveOut)
async def decide(request_id: uuid.UUID, body: DecisionIn, request: Request, user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    req = await _load_visible(session, user, request_id)
    await service.decide(session, user, req, body.decision, body.comment, request)
    await session.commit()
    req = await _load(session, request_id)
    return service.to_out(req, await service.workflow_for(session, req.leave_type_id))


@router.post("/requests/{request_id}/cancel", response_model=LeaveOut)
async def cancel(request_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                 session: AsyncSession = Depends(get_session)):
    req = await _load(session, request_id)
    if req.staff_id != user.staff_id:
        raise HTTPException(404, "Leave request not found")
    if req.status != "Pending":
        raise HTTPException(409, "Only a pending request can be cancelled")
    req.status, req.current_step_order, req.current_step_role = "Cancelled", None, None
    audit.record(session, user=user, action="leave_cancelled", entity="leave_request", entity_id=req.id,
                 subject_staff_id=req.staff_id, request=request)
    await session.commit()
    req = await _load(session, request_id)
    return service.to_out(req, await service.workflow_for(session, req.leave_type_id))
