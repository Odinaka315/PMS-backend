from datetime import date

from fastapi import APIRouter, Depends
from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_session
from app.core.deps import get_current_user, staff_scope_clause
from app.core.roles import Role
from app.models import Department, GradeLevel, LeaveRequest, Staff, User
from app.modules.leave import service as leave_service

router = APIRouter(prefix="/reports", tags=["reports"])

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


@router.get("/dashboard")
async def dashboard(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """Role-aware KPIs and chart data, computed from live rows within the caller's data scope.

    Payroll, promotion, performance, training and query KPIs from the frontend dashboards are added by their modules."""
    year = date.today().year
    scope = staff_scope_clause(user)
    active = scope & (Staff.status != "Exited") & (Staff.status != "Retired")

    total_staff = await session.scalar(select(func.count()).select_from(Staff).where(active)) or 0
    on_leave = await session.scalar(select(func.count()).select_from(Staff).where(scope, Staff.status == "On Leave")) or 0

    out: dict = {"role": user.role, "kpis": {"total_staff": total_staff, "on_leave": on_leave}}

    if user.role != Role.STAFF:
        dist = (await session.execute(
            select(Department.name, func.count(Staff.id)).join(Staff, Staff.department_id == Department.id)
            .where(active).group_by(Department.name).order_by(func.count(Staff.id).desc()))).all()
        out["department_distribution"] = [{"name": n, "value": c} for n, c in dist]
        out["kpis"]["departments"] = len(dist)

        gender = (await session.execute(select(Staff.gender, func.count()).where(active).group_by(Staff.gender))).all()
        out["gender_distribution"] = [{"name": g or "Unspecified", "value": c} for g, c in gender]

    if user.role in (Role.HR, Role.DIRECTOR, Role.SUPERADMIN):
        grades = (await session.execute(
            select(GradeLevel.code, GradeLevel.level, func.count(Staff.id)).join(Staff, Staff.grade_level_id == GradeLevel.id)
            .where(active).group_by(GradeLevel.code, GradeLevel.level).order_by(GradeLevel.level))).all()
        out["grade_distribution"] = [{"grade": g, "count": c} for g, _, c in grades]

    # Leave: requests per month this year, within scope.
    month_rows = (await session.execute(
        select(extract("month", LeaveRequest.start_date), func.count()).where(
            leave_service.visible_filter(user), extract("year", LeaveRequest.start_date) == year)
        .group_by(extract("month", LeaveRequest.start_date)))).all()
    by_month = {int(m): c for m, c in month_rows}
    out["leave_trend"] = [{"month": MONTHS[i], "requests": by_month.get(i + 1, 0)} for i in range(12)]

    if user.role != Role.STAFF:
        out["kpis"]["pending_leave"] = await session.scalar(
            select(func.count()).select_from(LeaveRequest).where(leave_service.awaiting_filter(user))) or 0
    else:
        bal = await leave_service.balances(session, user.staff_id, year) if user.staff_id else None
        out["kpis"]["leave_balance_days"] = bal.total_remaining if bal else 0

    return out
