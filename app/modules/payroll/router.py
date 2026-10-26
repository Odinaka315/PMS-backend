"""Payroll.

Who can do what (defaults; confirm with the hospital):
  * Pay structure (scales, allowances/deductions, individual adjustments): HR edits; HR, director, super admin read
  * A payroll run:  HR creates, recalculates (while Draft), deletes (while Draft) and marks Paid;
                    director or super admin approves, and never the person who created the run
  * Payslips:       each person sees only their own, and only once the run is Paid.
                    HR, director and super admin can see everyone's, at any stage. HODs see only their own.
                    Viewing someone else's payslip is written to the audit trail.
Every figure here is a placeholder until the hospital confirms its pay structure.
"""
import csv
import io
import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import get_current_user, get_staff_or_404, require_roles
from app.core.roles import Role
from app.core.security import utcnow
from app.models import GradeLevel, PayAdjustment, PayComponent, PayrollRun, Payslip, SalaryScale, Staff, User
from app.modules.notifications.service import notify, users_with_role

from . import service
from .schemas import (AdjustmentIn, AdjustmentOut, ApproveIn, ComponentIn, ComponentOut, DepartmentTotal, MyPayslipPage,
                      PayrollSummary, PayslipOut, PayslipPage, RunCreate, RunDetail, RunOut, ScaleEntryOut, ScaleUpdate,
                      kobo, naira)

router = APIRouter(prefix="/payroll", tags=["payroll"])

VIEW = Depends(require_roles(Role.HR, Role.DIRECTOR, Role.SUPERADMIN))
HR_ONLY = Depends(require_roles(Role.HR))


# ------------------------------------------------------------------ pay structure
@router.get("/scales", response_model=list[ScaleEntryOut], dependencies=[VIEW])
async def list_scales(session: AsyncSession = Depends(get_session)):
    rows = (await session.execute(
        select(SalaryScale, GradeLevel).join(GradeLevel, GradeLevel.id == SalaryScale.grade_level_id)
        .order_by(GradeLevel.level, SalaryScale.step))).all()
    return [ScaleEntryOut(grade_level_id=g.id, level=g.level, code=g.code, step=s.step, basic=naira(s.basic_kobo))
            for s, g in rows]


@router.put("/scales", response_model=list[ScaleEntryOut])
async def update_scales(body: ScaleUpdate, request: Request, user: User = Depends(require_roles(Role.HR)),
                        session: AsyncSession = Depends(get_session)):
    """Adds or changes entries. Existing payslips are snapshots, so this only affects runs calculated afterwards."""
    grades = {g.level: g for g in (await session.scalars(select(GradeLevel).where(GradeLevel.scale == "CONHESS"))).all()}
    seen = set()
    for e in body.entries:
        if e.level not in grades:
            raise HTTPException(422, f"Unknown grade level {e.level}")
        if (e.level, e.step) in seen:
            raise HTTPException(422, f"Level {e.level} step {e.step} appears twice")
        seen.add((e.level, e.step))
    existing = {(s.grade_level_id, s.step): s for s in (await session.scalars(select(SalaryScale))).all()}
    changed = []
    for e in body.entries:
        g = grades[e.level]
        row = existing.get((g.id, e.step))
        if row is None:
            session.add(SalaryScale(grade_level_id=g.id, step=e.step, basic_kobo=kobo(e.basic)))
            changed.append({"level": e.level, "step": e.step, "from": None, "to": e.basic})
        elif row.basic_kobo != kobo(e.basic):
            changed.append({"level": e.level, "step": e.step, "from": naira(row.basic_kobo), "to": e.basic})
            row.basic_kobo = kobo(e.basic)
    audit.record(session, user=user, action="salary_scale_updated", entity="salary_scale",
                 after={"changes": changed[:100], "count": len(changed)}, request=request)
    await session.commit()
    return await list_scales(session)


def _component_out(c: PayComponent) -> ComponentOut:
    return ComponentOut(id=c.id, code=c.code, name=c.name, kind=c.kind, method=c.method, amount=naira(c.amount_kobo),
                        percent=None if c.rate_bps is None else c.rate_bps / 100, min_level=c.min_level,
                        max_level=c.max_level, sort_order=c.sort_order, is_active=c.is_active)


def _apply(c: PayComponent, b: ComponentIn) -> None:
    c.code, c.name, c.kind, c.method = b.code, b.name, b.kind, b.method
    c.amount_kobo = kobo(b.amount) if b.amount is not None else None
    c.rate_bps = int(round(b.percent * 100)) if b.percent is not None else None
    c.min_level, c.max_level, c.sort_order, c.is_active = b.min_level, b.max_level, b.sort_order, b.is_active


def _component_snapshot(c: PayComponent) -> dict:
    return _component_out(c).model_dump(exclude={"id"})


@router.get("/components", response_model=list[ComponentOut], dependencies=[VIEW])
async def list_components(session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(PayComponent).order_by(PayComponent.kind.desc(), PayComponent.sort_order,
                                                                PayComponent.code))).all()
    return [_component_out(c) for c in rows]


@router.post("/components", response_model=ComponentOut, status_code=201)
async def create_component(body: ComponentIn, request: Request, user: User = Depends(require_roles(Role.HR)),
                           session: AsyncSession = Depends(get_session)):
    if await session.scalar(select(PayComponent.id).where(PayComponent.code == body.code)):
        raise HTTPException(409, "A component with this code already exists")
    c = PayComponent()
    _apply(c, body)
    session.add(c)
    await session.flush()
    audit.record(session, user=user, action="pay_component_created", entity="pay_component", entity_id=c.id,
                 after=_component_snapshot(c), request=request)
    await session.commit()
    return _component_out(c)


@router.put("/components/{cid}", response_model=ComponentOut)
async def update_component(cid: uuid.UUID, body: ComponentIn, request: Request,
                           user: User = Depends(require_roles(Role.HR)), session: AsyncSession = Depends(get_session)):
    c = await session.get(PayComponent, cid)
    if c is None:
        raise HTTPException(404, "Component not found")
    if body.code != c.code and await session.scalar(select(PayComponent.id).where(PayComponent.code == body.code)):
        raise HTTPException(409, "A component with this code already exists")
    before = _component_snapshot(c)
    _apply(c, body)
    audit.record(session, user=user, action="pay_component_updated", entity="pay_component", entity_id=c.id,
                 before=before, after=_component_snapshot(c), request=request)
    await session.commit()
    return _component_out(c)


# ------------------------------------------------------------------ individual adjustments
def _adj_out(a: PayAdjustment, s: Staff) -> AdjustmentOut:
    return AdjustmentOut(id=a.id, staff_id=a.staff_id, file_number=s.file_number, staff_name=s.full_name, kind=a.kind,
                         name=a.name, amount=naira(a.amount_kobo), start_period=service.period_str(a.start_period),
                         end_period=service.period_str(a.end_period) if a.end_period else None, reason=a.reason,
                         active=a.cancelled_at is None)


@router.post("/adjustments", response_model=AdjustmentOut, status_code=201)
async def create_adjustment(body: AdjustmentIn, request: Request, user: User = Depends(require_roles(Role.HR)),
                            session: AsyncSession = Depends(get_session)):
    s = await get_staff_or_404(session, body.staff)
    a = PayAdjustment(staff_id=s.id, kind=body.kind, name=body.name, amount_kobo=kobo(body.amount),
                      start_period=int(body.start_period.replace("-", "")),
                      end_period=int(body.end_period.replace("-", "")) if body.end_period else None,
                      reason=body.reason, created_by=user.id)
    session.add(a)
    await session.flush()
    audit.record(session, user=user, action="pay_adjustment_created", entity="pay_adjustment", entity_id=a.id,
                 subject_staff_id=s.id, after={"kind": a.kind, "name": a.name, "amount": body.amount,
                                               "from": body.start_period, "to": body.end_period}, request=request)
    await session.commit()
    return _adj_out(a, s)


@router.get("/adjustments", response_model=list[AdjustmentOut], dependencies=[HR_ONLY])
async def list_adjustments(staff: str | None = None, include_cancelled: bool = False,
                           session: AsyncSession = Depends(get_session)):
    stmt = select(PayAdjustment, Staff).join(Staff, Staff.id == PayAdjustment.staff_id)
    if staff:
        stmt = stmt.where(PayAdjustment.staff_id == (await get_staff_or_404(session, staff)).id)
    if not include_cancelled:
        stmt = stmt.where(PayAdjustment.cancelled_at.is_(None))
    rows = (await session.execute(stmt.order_by(PayAdjustment.created_at.desc()).limit(500))).all()
    return [_adj_out(a, s) for a, s in rows]


@router.delete("/adjustments/{aid}", status_code=204)
async def cancel_adjustment(aid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR)),
                            session: AsyncSession = Depends(get_session)):
    a = await session.get(PayAdjustment, aid)
    if a is None or a.cancelled_at is not None:
        raise HTTPException(404, "Adjustment not found")
    a.cancelled_at = utcnow()  # kept on record; payslips already produced are unaffected
    audit.record(session, user=user, action="pay_adjustment_cancelled", entity="pay_adjustment", entity_id=a.id,
                 subject_staff_id=a.staff_id, request=request)
    await session.commit()


# ------------------------------------------------------------------ runs
async def _run(session: AsyncSession, rid: uuid.UUID) -> PayrollRun:
    run = await session.get(PayrollRun, rid)
    if run is None:
        raise HTTPException(404, "Payroll run not found")
    return run


@router.post("/runs", response_model=RunOut, status_code=201)
async def create_run(body: RunCreate, request: Request, user: User = Depends(require_roles(Role.HR)),
                     session: AsyncSession = Depends(get_session)):
    today = date.today()
    if (body.year, body.month) > (today.year, today.month):
        raise HTTPException(422, "A payroll run cannot be created for a future month")
    period = service.period_int(body.year, body.month)
    if await session.scalar(select(PayrollRun.id).where(PayrollRun.period == period)):
        raise HTTPException(409, f"A payroll run for {service.label(body.year, body.month)} already exists")
    if not (await session.scalar(select(func.count()).select_from(SalaryScale))):
        raise HTTPException(409, "No salary scale has been set up yet")
    run = PayrollRun(year=body.year, month=body.month, period=period, created_by=user.id)
    session.add(run)
    await session.flush()
    await service.calculate(session, run)
    audit.record(session, user=user, action="payroll_run_created", entity="payroll_run", entity_id=run.id,
                 after={"period": service.period_str(period), "staff": run.staff_count,
                        "exceptions": run.exception_count}, request=request)
    await notify(session, await users_with_role(session, "director"), type="payroll", title="Payroll awaiting approval",
                 text=f"The {service.label(body.year, body.month)} payroll run is ready for your approval.",
                 link="/salary", entity_id=run.id)
    await session.commit()
    return await service.run_out(session, run)


@router.get("/runs", response_model=list[RunOut], dependencies=[VIEW])
async def list_runs(session: AsyncSession = Depends(get_session)):
    runs = (await session.scalars(select(PayrollRun).order_by(PayrollRun.period.desc()).limit(120))).all()
    return [await service.run_out(session, r) for r in runs]


@router.get("/runs/{rid}", response_model=RunDetail, dependencies=[VIEW])
async def get_run(rid: uuid.UUID, session: AsyncSession = Depends(get_session)):
    run = await _run(session, rid)
    bad = (await session.scalars(select(Payslip).where(Payslip.run_id == rid, Payslip.exception.is_not(None))
                                 .order_by(Payslip.file_number).limit(200))).all()
    return RunDetail(**(await service.run_out(session, run)).model_dump(), exceptions=[service.summary(p, run) for p in bad])


@router.post("/runs/{rid}/recalculate", response_model=RunOut)
async def recalculate(rid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR)),
                      session: AsyncSession = Depends(get_session)):
    run = await _run(session, rid)
    if run.status != "Draft":
        raise HTTPException(409, "Only a draft run can be recalculated")
    await service.calculate(session, run)
    run.exceptions_acknowledged = False
    audit.record(session, user=user, action="payroll_run_recalculated", entity="payroll_run", entity_id=run.id,
                 after={"staff": run.staff_count, "exceptions": run.exception_count}, request=request)
    await session.commit()
    return await service.run_out(session, run)


@router.delete("/runs/{rid}", status_code=204)
async def delete_run(rid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR)),
                     session: AsyncSession = Depends(get_session)):
    run = await _run(session, rid)
    if run.status != "Draft":
        raise HTTPException(409, "Only a draft run can be deleted")
    audit.record(session, user=user, action="payroll_run_deleted", entity="payroll_run", entity_id=run.id,
                 after={"period": service.period_str(run.period)}, request=request)
    await session.execute(delete(Payslip).where(Payslip.run_id == run.id))  # not left to the database's cascade
    await session.delete(run)
    await session.commit()


@router.post("/runs/{rid}/approve", response_model=RunOut)
async def approve(rid: uuid.UUID, body: ApproveIn, request: Request,
                  user: User = Depends(require_roles(Role.DIRECTOR, Role.SUPERADMIN)),
                  session: AsyncSession = Depends(get_session)):
    run = await _run(session, rid)
    if run.status != "Draft":
        raise HTTPException(409, f"This run is already {run.status.lower()}")
    if run.created_by == user.id:
        raise HTTPException(403, "You cannot approve a payroll run you created")
    if run.staff_count == 0:
        raise HTTPException(409, "This run has no staff in it")
    if run.exception_count and not body.acknowledge_exceptions:
        raise HTTPException(409, f"{run.exception_count} payslip(s) have exceptions. Review them, or approve with "
                                 "acknowledge_exceptions=true to accept that those staff will not be paid correctly.")
    run.status, run.approved_by, run.approved_at = "Approved", user.id, utcnow()
    run.exceptions_acknowledged = bool(run.exception_count)
    audit.record(session, user=user, action="payroll_run_approved", entity="payroll_run", entity_id=run.id,
                 after={"period": service.period_str(run.period), "exceptions": run.exception_count,
                        "acknowledged": run.exceptions_acknowledged, "total_net": naira(run.total_net_kobo)},
                 request=request)
    await notify(session, await users_with_role(session, "hr"), type="payroll", title="Payroll approved",
                 text=f"The {service.label(run.year, run.month)} payroll run was approved and can now be marked as paid.",
                 link="/salary", entity_id=run.id)
    await session.commit()
    return await service.run_out(session, run)


@router.post("/runs/{rid}/pay", response_model=RunOut)
async def mark_paid(rid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR)),
                    session: AsyncSession = Depends(get_session)):
    """Records that the payment has been made (this system does not move money). Publishes payslips to staff."""
    run = await _run(session, rid)
    if run.status != "Approved":
        raise HTTPException(409, "Only an approved run can be marked as paid")
    run.status, run.paid_by, run.paid_at = "Paid", user.id, utcnow()
    audit.record(session, user=user, action="payroll_run_paid", entity="payroll_run", entity_id=run.id,
                 after={"period": service.period_str(run.period)}, request=request)
    rows = await session.execute(select(User.id).join(Payslip, Payslip.staff_id == User.staff_id)
                                 .where(Payslip.run_id == run.id, Payslip.net_kobo > 0, User.is_active.is_(True)))
    await notify(session, [r[0] for r in rows], type="payroll", title="Payslip available",
                 text=f"Your {service.label(run.year, run.month)} payslip is ready.", link="/salary", entity_id=run.id,
                 dedupe_key=f"payslip:{run.id}")
    await session.commit()
    return await service.run_out(session, run)


@router.get("/runs/{rid}/payslips", response_model=PayslipPage, dependencies=[VIEW])
async def run_payslips(rid: uuid.UUID, q: str | None = Query(None, max_length=100), exceptions_only: bool = False,
                       page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
                       session: AsyncSession = Depends(get_session)):
    run = await _run(session, rid)
    stmt = select(Payslip).where(Payslip.run_id == rid)
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Payslip.staff_name).like(like), func.lower(Payslip.file_number).like(like)))
    if exceptions_only:
        stmt = stmt.where(Payslip.exception.is_not(None))
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(Payslip.file_number).offset((page - 1) * size).limit(size))).all()
    return PayslipPage(items=[service.summary(p, run) for p in rows], total=total or 0, page=page, size=size)


def _safe(cell) -> str:
    """Stops a name or note that starts with = + - @ being run as a formula when the CSV is opened in Excel."""
    s = "" if cell is None else str(cell)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


@router.get("/runs/{rid}/export")
async def export_run(rid: uuid.UUID, request: Request, user: User = Depends(require_roles(Role.HR, Role.DIRECTOR)),
                     session: AsyncSession = Depends(get_session)):
    """Payroll schedule as CSV. Holds no full account numbers (only the last four digits)."""
    run = await _run(session, rid)
    rows = (await session.scalars(select(Payslip).where(Payslip.run_id == rid).order_by(Payslip.file_number))).all()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["File number", "Name", "Department", "Grade", "Step", "Basic", "Gross", "Deductions", "Net", "Bank",
                "Account (last 4)", "Exception"])
    for p in rows:
        w.writerow([_safe(x) for x in (p.file_number, p.staff_name, p.department, p.grade_code, p.step,
                                       naira(p.basic_kobo), naira(p.gross_kobo), naira(p.deductions_kobo),
                                       naira(p.net_kobo), p.bank_name, p.account_last4, p.exception)])
    audit.record(session, user=user, action="payroll_exported", entity="payroll_run", entity_id=run.id,
                 after={"period": service.period_str(run.period), "rows": len(rows)}, request=request)
    await session.commit()
    return Response(out.getvalue(), media_type="text/csv; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="payroll-{service.period_str(run.period)}.csv"',
        "Cache-Control": "private, no-store"})


@router.get("/summary", response_model=PayrollSummary, dependencies=[VIEW])
async def payroll_summary(session: AsyncSession = Depends(get_session)):
    run = await session.scalar(select(PayrollRun).order_by(PayrollRun.period.desc()).limit(1))
    if run is None:
        return PayrollSummary(latest_run=None, by_department=[])
    rows = (await session.execute(
        select(Payslip.department, func.count(), func.sum(Payslip.gross_kobo), func.sum(Payslip.net_kobo))
        .where(Payslip.run_id == run.id).group_by(Payslip.department).order_by(Payslip.department))).all()
    return PayrollSummary(latest_run=await service.run_out(session, run),
                          by_department=[DepartmentTotal(department=d, staff=n, gross=naira(int(g or 0)),
                                                         net=naira(int(nt or 0))) for d, n, g, nt in rows])


# ------------------------------------------------------------------ payslips
@router.get("/payslips/me", response_model=MyPayslipPage)
async def my_payslips(year: int | None = Query(None, ge=2000, le=2100), user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    """Your own payslips from runs that have been paid, newest first."""
    if user.staff_id is None:
        return MyPayslipPage(items=[])
    stmt = (select(Payslip, PayrollRun).join(PayrollRun, PayrollRun.id == Payslip.run_id)
            .where(Payslip.staff_id == user.staff_id, PayrollRun.status == "Paid"))
    if year:
        stmt = stmt.where(PayrollRun.year == year)
    rows = (await session.execute(stmt.order_by(PayrollRun.period.desc()).limit(120))).all()
    return MyPayslipPage(items=[service.summary(p, r) for p, r in rows])


@router.get("/payslips/{pid}", response_model=PayslipOut)
async def get_payslip(pid: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    row = (await session.execute(select(Payslip, PayrollRun).join(PayrollRun, PayrollRun.id == Payslip.run_id)
                                 .where(Payslip.id == pid))).first()
    mine = row is not None and user.staff_id is not None and row[0].staff_id == user.staff_id
    privileged = user.role in (Role.HR, Role.DIRECTOR, Role.SUPERADMIN)
    if row is None or not ((mine and row[1].status == "Paid") or privileged):
        raise HTTPException(404, "Payslip not found")  # same answer whether it is missing or not yours
    p, run = row
    if not mine:
        audit.record(session, user=user, action="payslip_viewed", entity="payslip", entity_id=p.id,
                     subject_staff_id=p.staff_id, after={"period": service.period_str(run.period)}, request=request)
        await session.commit()
    return service.detail(p, run)
