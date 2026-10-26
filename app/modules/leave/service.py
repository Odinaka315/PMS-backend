import uuid
from datetime import date

from fastapi import HTTPException
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.roles import Role
from app.core.security import utcnow
from app.models import LeaveDecision, LeaveRequest, LeaveType, LeaveWorkflowStep, Staff, User

from app.modules.notifications.service import notify, user_id_for_staff, users_with_role

from .schemas import BalanceOut, BalanceRow, LeaveOut, StepOut


async def workflow_for(session: AsyncSession, leave_type_id: uuid.UUID) -> list[LeaveWorkflowStep]:
    """The type's own approval chain, or the default chain (leave_type_id IS NULL)."""
    for key in (leave_type_id, None):
        cond = LeaveWorkflowStep.leave_type_id == key if key else LeaveWorkflowStep.leave_type_id.is_(None)
        steps = (await session.scalars(select(LeaveWorkflowStep).where(cond).order_by(LeaveWorkflowStep.step_order))).all()
        if steps:
            return list(steps)
    return []


def count_days(start: date, end: date) -> int:
    # ASSUMPTION: calendar days, inclusive. Switch to working days once the hospital confirms its policy.
    return (end - start).days + 1


async def balances(session: AsyncSession, staff_id: uuid.UUID, year: int) -> BalanceOut:
    types = (await session.scalars(select(LeaveType).where(LeaveType.is_active.is_(True)).order_by(LeaveType.name))).all()
    rows = (await session.execute(
        select(LeaveRequest.leave_type_id, LeaveRequest.status, func.coalesce(func.sum(LeaveRequest.days), 0))
        .where(LeaveRequest.staff_id == staff_id, LeaveRequest.status.in_(("Approved", "Pending")),
               LeaveRequest.start_date >= date(year, 1, 1), LeaveRequest.start_date <= date(year, 12, 31))
        .group_by(LeaveRequest.leave_type_id, LeaveRequest.status))).all()
    used: dict = {}
    pending: dict = {}
    for type_id, status, total in rows:
        (used if status == "Approved" else pending)[type_id] = int(total)
    items = [BalanceRow(leave_type_id=t.id, code=t.code, name=t.name, entitlement=t.annual_entitlement_days,
                        used=used.get(t.id, 0), pending=pending.get(t.id, 0),
                        remaining=t.annual_entitlement_days - used.get(t.id, 0) - pending.get(t.id, 0)) for t in types]
    return BalanceOut(year=year, items=items, total_remaining=sum(i.remaining for i in items))


def to_out(req: LeaveRequest, workflow: list[LeaveWorkflowStep]) -> LeaveOut:
    decisions = {d.step_order: d for d in req.decisions}
    steps = [StepOut(label="Submitted", role=None, state="done", decided_at=req.created_at)]
    for w in workflow:
        d = decisions.get(w.step_order)
        if d and d.decision == "approve":
            state = "done"
        elif d and d.decision == "decline":
            state = "declined"
        elif req.status == "Pending" and req.current_step_order == w.step_order:
            state = "current"
        else:
            state = "upcoming"
        steps.append(StepOut(label=w.label, role=w.role, state=state, comment=d.comment if d else None,
                             decided_by=(d.decider.email if d else None), decided_at=(d.decided_at if d else None)))
    steps.append(StepOut(label="Completed", role=None, state="done" if req.status == "Approved" else "upcoming"))
    s = req.staff
    return LeaveOut(
        id=req.id, staff_id=s.id, staff_name=s.full_name, file_number=s.file_number, department=s.department.name,
        type=req.leave_type.name, leave_type_id=req.leave_type_id, start_date=req.start_date, end_date=req.end_date,
        days=req.days, reason=req.reason, replacement=req.replacement.full_name if req.replacement else None,
        status=req.status, current_step_role=req.current_step_role, created_at=req.created_at, steps=steps,
    )


async def create_request(session: AsyncSession, user: User, data, request) -> LeaveRequest:
    if user.staff_id is None:
        raise HTTPException(400, "This account has no staff record, so it cannot request leave")
    ltype = await session.get(LeaveType, data.leave_type_id)
    if ltype is None or not ltype.is_active:
        raise HTTPException(422, "Unknown leave type")
    if data.end_date < data.start_date:
        raise HTTPException(422, "End date cannot be before start date")
    if data.start_date < date.today() and not ltype.allow_backdate:
        raise HTTPException(422, f"{ltype.name} cannot start in the past")
    days = count_days(data.start_date, data.end_date)

    overlap = await session.scalar(select(LeaveRequest.id).where(
        LeaveRequest.staff_id == user.staff_id, LeaveRequest.status.in_(("Pending", "Approved")),
        LeaveRequest.start_date <= data.end_date, LeaveRequest.end_date >= data.start_date))
    if overlap:
        raise HTTPException(409, "These dates overlap another leave request")

    bal = await balances(session, user.staff_id, data.start_date.year)
    row = next(b for b in bal.items if b.leave_type_id == ltype.id)
    if days > row.remaining:
        raise HTTPException(422, f"Not enough {ltype.name.lower()} balance: {row.remaining} day(s) left, {days} requested")

    if data.replacement_staff_id:
        rep = await session.get(Staff, data.replacement_staff_id)
        if rep is None or rep.deleted_at or rep.id == user.staff_id or rep.department_id != user.department_id:
            raise HTTPException(422, "Replacement officer must be another staff member in your department")

    workflow = await workflow_for(session, ltype.id)
    # Approvers don't approve their own level: an HOD's request starts at HR, HR's at the director.
    remaining = [w for w in workflow if w.role != user.role]
    first = remaining[0] if remaining else None

    req = LeaveRequest(
        staff_id=user.staff_id, leave_type_id=ltype.id, start_date=data.start_date, end_date=data.end_date, days=days,
        reason=data.reason, replacement_staff_id=data.replacement_staff_id,
        status="Pending" if first else "Approved",
        current_step_order=first.step_order if first else None, current_step_role=first.role if first else None,
    )
    session.add(req)
    await session.flush()
    audit.record(session, user=user, action="leave_requested", entity="leave_request", entity_id=req.id,
                 subject_staff_id=user.staff_id,
                 after={"type": ltype.name, "start": data.start_date, "end": data.end_date, "days": days}, request=request)
    if first:
        await notify_approvers(session, first.role, req, ltype.name, user.staff, exclude=user.id)
    return req


async def decide(session: AsyncSession, user: User, req: LeaveRequest, decision: str, comment: str | None, request):
    if req.status != "Pending":
        raise HTTPException(409, f"This request is already {req.status.lower()}")
    if user.role != req.current_step_role:
        raise HTTPException(403, "This request is not waiting on your approval")
    if user.staff_id == req.staff_id:
        raise HTTPException(403, "You cannot approve or decline your own request")
    if user.role == Role.HOD and user.department_id != req.staff.department_id:
        raise HTTPException(403, "This request belongs to another department")

    workflow = await workflow_for(session, req.leave_type_id)
    skip_role = None
    req_user = await session.scalar(select(User).where(User.staff_id == req.staff_id))
    if req_user:
        skip_role = req_user.role
    session.add(LeaveDecision(request_id=req.id, step_order=req.current_step_order, role=user.role,
                              decided_by=user.id, decision=decision, comment=comment, decided_at=utcnow()))

    if decision == "decline":
        req.status, req.current_step_order, req.current_step_role = "Declined", None, None
    else:
        nxt = next((w for w in workflow if w.step_order > req.current_step_order and w.role != skip_role), None)
        if nxt is None:
            req.status, req.current_step_order, req.current_step_role = "Approved", None, None
        else:
            req.current_step_order, req.current_step_role = nxt.step_order, nxt.role
    audit.record(session, user=user, action=f"leave_{decision}d", entity="leave_request", entity_id=req.id,
                 subject_staff_id=req.staff_id, after={"status": req.status, "comment": comment}, request=request)

    # Tell the requester when it is settled, and the next approver when it is their turn.
    if req_user and req.status in ("Approved", "Declined"):
        verb = "approved" if req.status == "Approved" else "declined"
        await notify(session, [req_user.id], type="leave", title=f"Leave request {verb}",
                     text=f"Your {req.leave_type.name.lower()} request ({req.start_date:%d %b} to {req.end_date:%d %b %Y}) was {verb}.",
                     link="/leave", entity_id=req.id)
    elif req.status == "Pending":
        await notify_approvers(session, req.current_step_role, req, req.leave_type.name, req.staff, exclude=user.id)


async def notify_approvers(session: AsyncSession, role: str, req: LeaveRequest, type_name: str, staff: Staff,
                           exclude=None) -> None:
    ids = await users_with_role(session, role, department_id=staff.department_id)
    own = await user_id_for_staff(session, req.staff_id)  # nobody is asked to approve their own request
    await notify(session, [i for i in ids if i != own], type="leave", title="Leave awaiting your approval",
                 text=f"{staff.full_name} requested {req.days} day(s) of {type_name.lower()}.",
                 link="/leave", entity_id=req.id, exclude=exclude)


def visible_filter(user: User):
    """Which leave requests a user may see: own, department (HOD), or all (HR/director/superadmin)."""
    if user.role in (Role.HR, Role.DIRECTOR, Role.SUPERADMIN):
        return True
    if user.role == Role.HOD and user.department_id is not None:
        return LeaveRequest.staff_id.in_(select(Staff.id).where(Staff.department_id == user.department_id))
    return LeaveRequest.staff_id == user.staff_id


def awaiting_filter(user: User):
    cond = and_(LeaveRequest.status == "Pending", LeaveRequest.current_step_role == user.role,
                LeaveRequest.staff_id != user.staff_id)
    if user.role == Role.HOD and user.department_id is not None:
        cond = and_(cond, LeaveRequest.staff_id.in_(select(Staff.id).where(Staff.department_id == user.department_id)))
    return cond
