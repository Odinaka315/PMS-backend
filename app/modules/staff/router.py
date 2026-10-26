import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import settings
from app.core.db import get_session
from app.core.deps import (
    get_accessible_staff, get_current_user, get_staff_or_404, reload_staff, require_roles, staff_scope_clause,
)
from app.core.roles import Role
from app.core.security import decrypt_field, encrypt_field, mask
from app.models import (
    AuditLog, BankAccount, Dependant, Department, Education, EmergencyContact, GradeLevel, MedicalLicense, Staff, User,
)

from app.modules.users.service import provision_account

from .schemas import (
    AccountInfo, StaffCreated, BankIn, BankOut, Computed, DependantIn, DependantOut, EducationIn, EducationOut, EmergencyContactIn, LicenseIn,
    LicenseOut, StaffCreate, StaffLookup, StaffPage, StaffProfile, StaffSelfUpdate, StaffSummary, StaffUpdate,
)

router = APIRouter(prefix="/staff", tags=["staff"])

HR_ONLY = require_roles(Role.HR)
DIRECTORY_ROLES = require_roles(Role.HOD, Role.HR, Role.DIRECTOR, Role.SUPERADMIN)


# ---------------------------------------------------------------- helpers
def summary(s: Staff) -> StaffSummary:
    return StaffSummary(
        id=s.id, file_number=s.file_number, name=s.full_name, dept=s.department.name, department_id=s.department_id,
        grade=s.grade_level.code if s.grade_level else None, step=s.step, status=s.status, avatar=s.avatar_url,
        designation=s.designation,
    )


def _snapshot(s: Staff) -> dict:
    cols = ("title", "first_name", "last_name", "other_names", "email", "phone", "date_of_birth", "gender",
            "marital_status", "nationality", "state_of_origin", "address", "avatar_url", "department_id", "designation",
            "grade_level_id", "step", "employment_date", "confirmation_date", "supervisor_id", "status")
    return {c: getattr(s, c) for c in cols}


def _add_years(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year + years)
    except ValueError:  # 29 Feb
        return d.replace(year=d.year + years, day=28)


def _ym(days: int) -> str:
    months = max(days, 0) // 30
    return f"{months // 12}y {months % 12}m"


def computed_for(s: Staff, today: date | None = None) -> Computed:
    today = today or date.today()
    service_days = (today - s.employment_date).days if s.employment_date else None
    candidates = []
    if s.date_of_birth:
        candidates.append(_add_years(s.date_of_birth, settings.retirement_age))
    if s.employment_date:
        candidates.append(_add_years(s.employment_date, settings.retirement_service_years))
    retirement = min(candidates) if candidates else None  # whichever comes first (PLACEHOLDER policy, see config)
    return Computed(
        years_of_service=round(service_days / 365.25, 2) if service_days is not None else None,
        service_label=_ym(service_days) if service_days is not None else None,
        retirement_date=retirement,
        time_to_retirement_label=_ym((retirement - today).days) if retirement else None,
    )


def license_out(lic: MedicalLicense | None) -> LicenseOut | None:
    if lic is None:
        return None
    today = date.today()
    if lic.expires_on is None:
        state = "Active"
    elif lic.expires_on < today:
        state = "Expired"
    elif (lic.expires_on - today).days <= 90:
        state = "Expiring Soon"
    else:
        state = "Active"
    return LicenseOut(license_number=lic.license_number, issued_on=lic.issued_on, expires_on=lic.expires_on,
                      body=lic.body, status=state)


def bank_out(viewer: User, s: Staff) -> BankOut | None:
    """Bank details are only returned to the owner and HR. The BVN is always masked."""
    if s.bank is None or not (viewer.role == Role.HR or viewer.staff_id == s.id):
        return None
    number = decrypt_field(s.bank.account_number_enc) or ""
    bvn = decrypt_field(s.bank.bvn_enc)
    return BankOut(bank_name=s.bank.bank_name, account_name=s.bank.account_name, account_number=number,
                   bvn=(bvn[:2] + "*" * 7 + bvn[-2:]) if bvn else None)


def profile_out(viewer: User, s: Staff) -> StaffProfile:
    ec = s.emergency_contact
    return StaffProfile(
        id=s.id, file_number=s.file_number, name=s.full_name, avatar=s.avatar_url, status=s.status,
        personal={
            "first_name": s.first_name, "last_name": s.last_name, "other_names": s.other_names, "title": s.title,
            "email": s.email, "phone": s.phone, "date_of_birth": s.date_of_birth, "gender": s.gender,
            "marital_status": s.marital_status, "nationality": s.nationality, "state_of_origin": s.state_of_origin,
            "address": s.address,
        },
        employment={
            "file_number": s.file_number, "employment_date": s.employment_date, "confirmation_date": s.confirmation_date,
            "department": s.department.name, "department_id": s.department_id, "designation": s.designation,
            "grade": s.grade_level.code if s.grade_level else None, "step": s.step,
            "supervisor": s.supervisor.full_name if s.supervisor else None,
            "supervisor_id": s.supervisor_id, "status": s.status,
        },
        education=[EducationOut(id=e.id, institution=e.institution, qualification=e.qualification, year=e.year)
                   for e in sorted(s.education, key=lambda e: e.year or 0)],
        license=license_out(s.license),
        emergency_contact=({"name": ec.name, "relationship": ec.relationship_, "phone": ec.phone, "email": ec.email}
                           if ec else None),
        dependants=[DependantOut(id=d.id, name=d.name, relationship=d.relationship_, date_of_birth=d.date_of_birth)
                    for d in s.dependants],
        bank=bank_out(viewer, s),
        computed=computed_for(s),
    )


async def _validate_refs(session: AsyncSession, data: dict) -> None:
    if data.get("department_id") and not await session.get(Department, data["department_id"]):
        raise HTTPException(422, "Unknown department_id")
    if data.get("grade_level_id") and not await session.get(GradeLevel, data["grade_level_id"]):
        raise HTTPException(422, "Unknown grade_level_id")
    if data.get("supervisor_id") and not await session.get(Staff, data["supervisor_id"]):
        raise HTTPException(422, "Unknown supervisor_id")


async def _owner_or_hr(session: AsyncSession, user: User, ident: str) -> Staff:
    s = await get_staff_or_404(session, ident)
    if not (user.role == Role.HR or user.staff_id == s.id):
        raise HTTPException(403, "You can only change your own records")
    return s


# ---------------------------------------------------------------- directory
@router.get("", response_model=StaffPage)
async def list_staff(
    q: str | None = None, department_id: uuid.UUID | None = None, status: str | None = None,
    grade: str | None = Query(None, description="e.g. 'CONHESS 7'"),
    page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
    user: User = Depends(DIRECTORY_ROLES), session: AsyncSession = Depends(get_session),
):
    stmt = select(Staff).where(staff_scope_clause(user))
    if q:
        like = f"%{q.strip().lower()}%"
        stmt = stmt.where(or_(func.lower(Staff.first_name).like(like), func.lower(Staff.last_name).like(like),
                              func.lower(Staff.file_number).like(like), func.lower(Staff.email).like(like)))
    if department_id:
        stmt = stmt.where(Staff.department_id == department_id)
    if status:
        stmt = stmt.where(Staff.status == status)
    if grade:
        stmt = stmt.join(GradeLevel, Staff.grade_level_id == GradeLevel.id).where(GradeLevel.code == grade)
    total = await session.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await session.scalars(
        stmt.order_by(Staff.last_name, Staff.first_name).offset((page - 1) * size).limit(size))).unique().all()
    return StaffPage(items=[summary(s) for s in rows], total=total or 0, page=page, size=size)


@router.get("/lookup", response_model=list[StaffLookup])
async def lookup_colleagues(q: str = Query(min_length=2), user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    """Minimal colleague search for the 'Covering colleague / replacement officer' field.
    Open to every role but limited to active staff in the caller's own department, and to name/file number only."""
    if user.department_id is None:
        return []
    like = f"%{q.strip().lower()}%"
    rows = (await session.scalars(
        select(Staff).where(Staff.deleted_at.is_(None), Staff.status == "Active", Staff.department_id == user.department_id,
                            Staff.id != user.staff_id,
                            or_(func.lower(Staff.first_name).like(like), func.lower(Staff.last_name).like(like),
                                func.lower(Staff.file_number).like(like))).limit(10))).unique().all()
    return [StaffLookup(id=s.id, file_number=s.file_number, name=s.full_name, dept=s.department.name) for s in rows]


@router.get("/me", response_model=StaffProfile)
async def my_profile(user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    if user.staff_id is None:
        raise HTTPException(404, "This account has no staff profile")
    s = await get_staff_or_404(session, str(user.staff_id))
    return profile_out(user, s)


@router.patch("/me", response_model=StaffProfile)
async def update_my_profile(body: StaffSelfUpdate, request: Request, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session)):
    if user.staff_id is None:
        raise HTTPException(404, "This account has no staff profile")
    s = await get_staff_or_404(session, str(user.staff_id))
    return await _apply_update(session, request, user, s, body.model_dump(exclude_unset=True))


def _account_info(u: User, temp: str | None) -> AccountInfo:
    return AccountInfo(user_id=u.id, email=u.email, must_change_password=u.must_change_password,
                       initial_password_expires_at=u.password_expires_at, temporary_password=temp)


@router.post("", response_model=StaffCreated, status_code=201)
async def create_staff(body: StaffCreate, request: Request, user: User = Depends(HR_ONLY),
                       session: AsyncSession = Depends(get_session)):
    """Creates the staff record and, by default, their login in the same transaction: either both exist or neither.
    The login name is the staff member's email; the starting password follows INITIAL_PASSWORD_MODE, and the person
    must choose their own password at first sign-in."""
    data = body.model_dump()
    want_account = data.pop("create_account") and settings.auto_create_accounts
    await _validate_refs(session, data)
    dept = await session.get(Department, body.department_id)
    file_number = (data.pop("file_number") or "").strip().upper()
    if not file_number:
        n = (await session.scalar(select(func.count()).select_from(Staff).where(Staff.department_id == dept.id)) or 0) + 1
        while await session.scalar(select(Staff.id).where(Staff.file_number == f"YAB-{dept.code}-{n:03d}")):
            n += 1
        file_number = f"YAB-{dept.code}-{n:03d}"
    elif await session.scalar(select(Staff.id).where(Staff.file_number == file_number)):
        raise HTTPException(409, "File number already exists")
    s = Staff(file_number=file_number, **data)
    session.add(s)
    await session.flush()
    audit.record(session, user=user, action="staff_created", entity="staff", entity_id=s.id, subject_staff_id=s.id,
                 after={"file_number": file_number, **_snapshot(s)}, request=request)

    account, note, temp = None, None, None
    if want_account and not (s.email or "").strip():
        note = ("No email address was given, so no login was created. Add one, then use "
                "POST /staff/{id}/account to create the login.")
    elif want_account:
        # Raises 409 on an email clash; nothing has been committed yet, so the staff record is not created either.
        account, temp = await provision_account(session, staff=s, actor=user, request=request, automatic=True)
    else:
        note = "No login was created (create_account was off)."
    await session.commit()
    profile = profile_out(user, await reload_staff(session, s.id))
    return StaffCreated(**profile.model_dump(), account=_account_info(account, temp) if account else None,
                        account_note=note)


@router.post("/{ident}/account", response_model=AccountInfo, status_code=201)
async def create_account_for_staff(ident: str, request: Request, user: User = Depends(HR_ONLY),
                                   session: AsyncSession = Depends(get_session)):
    """Create the login later: for staff added without an email, or with create_account off."""
    s = await get_staff_or_404(session, ident)
    account, temp = await provision_account(session, staff=s, actor=user, request=request)
    await session.commit()
    return _account_info(account, temp)


# ---------------------------------------------------------------- one person
@router.get("/{ident}", response_model=StaffProfile)
async def get_profile(ident: str, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session)):
    s = await get_accessible_staff(session, user, ident)
    if s.id != user.staff_id:  # reading someone else's personnel file is itself audited
        audit.record(session, user=user, action="staff_viewed", entity="staff", entity_id=s.id,
                     subject_staff_id=s.id, request=request)
        await session.commit()
    return profile_out(user, s)


async def _apply_update(session, request, user, s: Staff, changes: dict) -> StaffProfile:
    await _validate_refs(session, changes)
    if "supervisor_id" in changes and changes["supervisor_id"] == s.id:
        raise HTTPException(422, "A staff member cannot supervise themselves")
    before = _snapshot(s)
    for k, v in changes.items():
        setattr(s, k, v)
    b, a = audit.diff(before, _snapshot(s))
    if a:
        audit.record(session, user=user, action="staff_updated", entity="staff", entity_id=s.id,
                     subject_staff_id=s.id, before=b, after=a, request=request)
    await session.commit()
    return profile_out(user, await reload_staff(session, s.id))


@router.patch("/{ident}", response_model=StaffProfile)
async def update_staff(ident: str, body: StaffUpdate, request: Request, user: User = Depends(HR_ONLY),
                       session: AsyncSession = Depends(get_session)):
    s = await get_staff_or_404(session, ident)
    return await _apply_update(session, request, user, s, body.model_dump(exclude_unset=True))


@router.delete("/{ident}", status_code=204)
async def archive_staff(ident: str, request: Request, user: User = Depends(HR_ONLY),
                        session: AsyncSession = Depends(get_session)):
    """Soft delete: the record is hidden but kept for the audit trail."""
    from app.core.security import utcnow
    s = await get_staff_or_404(session, ident)
    s.deleted_at = utcnow()
    audit.record(session, user=user, action="staff_archived", entity="staff", entity_id=s.id,
                 subject_staff_id=s.id, request=request)
    await session.commit()
    return Response(status_code=204)


@router.get("/{ident}/audit")
async def staff_audit(ident: str, page: int = Query(1, ge=1), size: int = Query(25, ge=1, le=100),
                      user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    """The 'Audit Log' tab. HR/director/super admin see everything about a person; the person sees their own trail."""
    s = await get_staff_or_404(session, ident)
    if not (user.role in (Role.HR, Role.DIRECTOR, Role.SUPERADMIN) or user.staff_id == s.id):
        raise HTTPException(403, "You do not have permission to view this audit trail")
    stmt = select(AuditLog).where(AuditLog.subject_staff_id == s.id)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery()))
    rows = (await session.scalars(stmt.order_by(AuditLog.at.desc()).offset((page - 1) * size).limit(size))).all()
    return {"total": total or 0, "page": page, "size": size, "items": [
        {"id": r.id, "at": r.at, "action": r.action, "by": r.user_email or "System", "before": r.before, "after": r.after}
        for r in rows]}


# ---------------------------------------------------------------- profile sections
@router.post("/{ident}/education", response_model=EducationOut, status_code=201)
async def add_education(ident: str, body: EducationIn, request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    s = await _owner_or_hr(session, user, ident)
    e = Education(staff_id=s.id, **body.model_dump())
    session.add(e)
    await session.flush()
    audit.record(session, user=user, action="education_added", entity="education", entity_id=e.id,
                 subject_staff_id=s.id, after=body.model_dump(), request=request)
    await session.commit()
    return EducationOut(id=e.id, **body.model_dump())


@router.delete("/{ident}/education/{item_id}", status_code=204)
async def delete_education(ident: str, item_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    s = await _owner_or_hr(session, user, ident)
    e = await session.scalar(select(Education).where(Education.id == item_id, Education.staff_id == s.id))
    if e is None:
        raise HTTPException(404, "Not found")
    audit.record(session, user=user, action="education_removed", entity="education", entity_id=e.id,
                 subject_staff_id=s.id, before={"institution": e.institution, "qualification": e.qualification},
                 request=request)
    await session.delete(e)
    await session.commit()
    return Response(status_code=204)


@router.post("/{ident}/dependants", response_model=DependantOut, status_code=201)
async def add_dependant(ident: str, body: DependantIn, request: Request, user: User = Depends(get_current_user),
                        session: AsyncSession = Depends(get_session)):
    s = await _owner_or_hr(session, user, ident)
    d = Dependant(staff_id=s.id, name=body.name, relationship_=body.relationship, date_of_birth=body.date_of_birth)
    session.add(d)
    await session.flush()
    audit.record(session, user=user, action="dependant_added", entity="dependant", entity_id=d.id,
                 subject_staff_id=s.id, after=body.model_dump(), request=request)
    await session.commit()
    return DependantOut(id=d.id, **body.model_dump())


@router.delete("/{ident}/dependants/{item_id}", status_code=204)
async def delete_dependant(ident: str, item_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                           session: AsyncSession = Depends(get_session)):
    s = await _owner_or_hr(session, user, ident)
    d = await session.scalar(select(Dependant).where(Dependant.id == item_id, Dependant.staff_id == s.id))
    if d is None:
        raise HTTPException(404, "Not found")
    audit.record(session, user=user, action="dependant_removed", entity="dependant", entity_id=d.id,
                 subject_staff_id=s.id, before={"name": d.name}, request=request)
    await session.delete(d)
    await session.commit()
    return Response(status_code=204)


@router.put("/{ident}/emergency-contact", status_code=204)
async def put_emergency_contact(ident: str, body: EmergencyContactIn, request: Request,
                                user: User = Depends(get_current_user), session: AsyncSession = Depends(get_session)):
    s = await _owner_or_hr(session, user, ident)
    ec = s.emergency_contact or EmergencyContact(staff_id=s.id, name="", relationship_="", phone="")
    ec.name, ec.relationship_, ec.phone, ec.email = body.name, body.relationship, body.phone, body.email
    session.add(ec)
    audit.record(session, user=user, action="emergency_contact_updated", entity="emergency_contact",
                 subject_staff_id=s.id, after=body.model_dump(), request=request)
    await session.commit()
    return Response(status_code=204)


@router.put("/{ident}/license", status_code=204)
async def put_license(ident: str, body: LicenseIn, request: Request, user: User = Depends(HR_ONLY),
                      session: AsyncSession = Depends(get_session)):
    """HR-only: licence details are verified records."""
    s = await get_staff_or_404(session, ident)
    lic = s.license or MedicalLicense(staff_id=s.id, license_number="")
    lic.license_number, lic.issued_on, lic.expires_on, lic.body = body.license_number, body.issued_on, body.expires_on, body.body
    session.add(lic)
    audit.record(session, user=user, action="license_updated", entity="license", subject_staff_id=s.id,
                 after=body.model_dump(), request=request)
    await session.commit()
    return Response(status_code=204)


@router.put("/{ident}/bank", status_code=204)
async def put_bank(ident: str, body: BankIn, request: Request, user: User = Depends(HR_ONLY),
                   session: AsyncSession = Depends(get_session)):
    """HR-only for now. Account number and BVN are stored encrypted and never written to the audit log."""
    s = await get_staff_or_404(session, ident)
    bank = s.bank or BankAccount(staff_id=s.id, bank_name="", account_name="", account_number_enc="")
    bank.bank_name, bank.account_name = body.bank_name, body.account_name
    bank.account_number_enc = encrypt_field(body.account_number)
    bank.bvn_enc = encrypt_field(body.bvn)
    session.add(bank)
    audit.record(session, user=user, action="bank_updated", entity="bank_account", subject_staff_id=s.id,
                 after={"bank_name": body.bank_name, "account_number": "x", "bvn": "x"}, request=request)
    await session.commit()
    return Response(status_code=204)
