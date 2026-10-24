import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.db import get_session
from app.core.deps import get_current_user, require_roles
from app.core.roles import Role
from app.models import Department, GradeLevel, Staff, User

router = APIRouter(tags=["org"])


class DepartmentIn(BaseModel):
    code: str = Field(min_length=2, max_length=10, pattern=r"^[A-Za-z0-9]+$")
    name: str = Field(min_length=2, max_length=120)


class DepartmentOut(BaseModel):
    id: uuid.UUID
    code: str
    name: str
    staff_count: int = 0


class GradeOut(BaseModel):
    id: uuid.UUID
    code: str
    level: int
    scale: str


@router.get("/departments", response_model=list[DepartmentOut])
async def list_departments(_: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    counts = dict((await session.execute(
        select(Staff.department_id, func.count()).where(Staff.deleted_at.is_(None)).group_by(Staff.department_id))).all())
    depts = (await session.scalars(select(Department).order_by(Department.name))).all()
    return [DepartmentOut(id=d.id, code=d.code, name=d.name, staff_count=counts.get(d.id, 0)) for d in depts]


@router.post("/departments", response_model=DepartmentOut, status_code=201)
async def create_department(body: DepartmentIn, request: Request,
                            actor: User = Depends(require_roles(Role.HR, Role.SUPERADMIN)),
                            session: AsyncSession = Depends(get_session)):
    code = body.code.upper()
    if await session.scalar(select(Department.id).where((Department.code == code) | (Department.name == body.name))):
        raise HTTPException(409, "A department with this code or name already exists")
    dept = Department(code=code, name=body.name)
    session.add(dept)
    await session.flush()
    audit.record(session, user=actor, action="department_created", entity="department", entity_id=dept.id,
                 after={"code": code, "name": body.name}, request=request)
    await session.commit()
    return DepartmentOut(id=dept.id, code=dept.code, name=dept.name)


@router.get("/grades", response_model=list[GradeOut])
async def list_grades(_: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    rows = (await session.scalars(select(GradeLevel).order_by(GradeLevel.scale, GradeLevel.level))).all()
    return [GradeOut(id=g.id, code=g.code, level=g.level, scale=g.scale) for g in rows]
