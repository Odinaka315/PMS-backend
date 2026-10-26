import calendar
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import decrypt_field, utcnow
from app.models import (GradeLevel, PayAdjustment, PayComponent, PayrollRun, Payslip, SalaryScale, Staff, User)

from . import calc
from .schemas import LineOut, PayslipOut, PayslipSummary, RunOut, naira

# Who is paid. PLACEHOLDER policy: suspended, retired and exited staff are left out of the run.
PAID_STATUSES = ("Active", "On Leave")


def period_int(year: int, month: int) -> int:
    return year * 100 + month


def period_str(p: int) -> str:
    return f"{p // 100:04d}-{p % 100:02d}"


def label(year: int, month: int) -> str:
    return f"{calendar.month_name[month]} {year}"


async def _emails(session: AsyncSession, ids: list[uuid.UUID | None]) -> dict[uuid.UUID, str]:
    wanted = {i for i in ids if i}
    if not wanted:
        return {}
    rows = await session.execute(select(User.id, User.email).where(User.id.in_(wanted)))
    return {i: e for i, e in rows}


async def run_out(session: AsyncSession, run: PayrollRun) -> RunOut:
    who = await _emails(session, [run.created_by, run.approved_by, run.paid_by])
    return RunOut(
        id=run.id, year=run.year, month=run.month, label=label(run.year, run.month), status=run.status,
        staff_count=run.staff_count, exception_count=run.exception_count, total_gross=naira(run.total_gross_kobo),
        total_deductions=naira(run.total_deductions_kobo), total_net=naira(run.total_net_kobo),
        created_by=who.get(run.created_by), created_at=run.created_at, approved_by=who.get(run.approved_by),
        approved_at=run.approved_at, exceptions_acknowledged=run.exceptions_acknowledged,
        paid_by=who.get(run.paid_by), paid_at=run.paid_at)


def summary(p: Payslip, run: PayrollRun) -> PayslipSummary:
    return PayslipSummary(
        id=p.id, run_id=p.run_id, period=period_str(run.period), label=label(run.year, run.month), staff_id=p.staff_id,
        file_number=p.file_number, staff_name=p.staff_name, department=p.department, grade=p.grade_code, step=p.step,
        gross=naira(p.gross_kobo), deductions=naira(p.deductions_kobo), net=naira(p.net_kobo), exception=p.exception)


def detail(p: Payslip, run: PayrollRun) -> PayslipOut:
    def lines(kind):
        return [LineOut(code=x["code"], name=x["name"], kind=x["kind"], amount=naira(x["amount_kobo"]))
                for x in p.lines if x["kind"] == kind]
    return PayslipOut(**summary(p, run).model_dump(), basic=naira(p.basic_kobo), earnings=lines("earning"),
                      deduction_lines=lines("deduction"), bank_name=p.bank_name, account_last4=p.account_last4,
                      run_status=run.status, paid_at=run.paid_at)


async def calculate(session: AsyncSession, run: PayrollRun) -> None:
    """(Re)builds every payslip of a Draft run from the current records. Nothing is paid by this."""
    scales = {(s.grade_level_id, s.step): s.basic_kobo for s in (await session.scalars(select(SalaryScale))).all()}
    levels = {g.id: g for g in (await session.scalars(select(GradeLevel))).all()}
    components = [calc.Component(c.code, c.name, c.kind, c.method, c.amount_kobo, c.rate_bps, c.min_level, c.max_level,
                                 c.sort_order)
                  for c in (await session.scalars(select(PayComponent).where(PayComponent.is_active.is_(True)))).all()]
    adjustments: dict[uuid.UUID, list[calc.Extra]] = {}
    for a in (await session.scalars(select(PayAdjustment).where(
            PayAdjustment.cancelled_at.is_(None), PayAdjustment.start_period <= run.period))).all():
        if a.end_period is None or a.end_period >= run.period:
            adjustments.setdefault(a.staff_id, []).append(calc.Extra(str(a.id), a.kind, a.name, a.amount_kobo))

    staff = (await session.scalars(select(Staff).where(
        Staff.deleted_at.is_(None), Staff.status.in_(PAID_STATUSES)).order_by(Staff.file_number))).unique().all()

    await session.execute(delete(Payslip).where(Payslip.run_id == run.id))
    totals = [0, 0, 0]
    exceptions = 0
    for s in staff:
        grade = levels.get(s.grade_level_id)
        problem = None
        basic = 0
        if grade is None or s.step is None:
            problem = "No grade level or step on record"
        else:
            basic = scales.get((grade.id, s.step))
            if basic is None:
                basic, problem = 0, f"No salary scale for {grade.code} step {s.step}"
        if problem:
            lines, gross, ded, net = [], 0, 0, 0
        else:
            lines, gross, ded, net = calc.compute(basic, grade.level, components, adjustments.get(s.id, []))
            if net < 0:
                problem = "Deductions exceed gross pay"
        bank = s.bank
        last4 = None
        if bank is None:
            problem = problem or "No bank account on record"
        else:
            number = decrypt_field(bank.account_number_enc) or ""
            last4 = number[-4:] or None
        exceptions += 1 if problem else 0
        totals[0] += gross
        totals[1] += ded
        totals[2] += net
        session.add(Payslip(
            run_id=run.id, staff_id=s.id, file_number=s.file_number, staff_name=s.full_name,
            department=s.department.name, grade_code=grade.code if grade else None, step=s.step, basic_kobo=basic,
            gross_kobo=gross, deductions_kobo=ded, net_kobo=net, lines=lines,
            bank_name=bank.bank_name if bank else None, account_last4=last4, exception=problem))
    run.staff_count, run.exception_count = len(staff), exceptions
    run.total_gross_kobo, run.total_deductions_kobo, run.total_net_kobo = totals
    run.calculated_at = utcnow()
    await session.flush()
