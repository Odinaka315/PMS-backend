"""Mock data for development. There is no real hospital data yet; every name, number and date here is invented.

    python -m app.seed            # add mock data to an empty database
    python -m app.seed --reset    # drop everything, recreate tables, then seed (development only)

Demo logins mirror the frontend's: staff|hod|hr|director|superadmin@hospital.test / hospital123
"""
import asyncio
import random
import sys
import uuid
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.security import encrypt_field, hash_password, utcnow
from app.core.storage import LocalStorage, get_storage
from app.models import (
    Base, BankAccount, Dependant, Department, Document, Education, EmergencyContact, GradeLevel, LeaveDecision, LeaveRequest,
    LeaveType, LeaveWorkflowStep, MedicalLicense, Notification, PayComponent, PayrollRun, SalaryScale, Staff, User,
    Appraisal, AppraisalCycle, PerformanceGoal, QueryEvent, StaffQuery,
)

DEMO_PASSWORD = "hospital123"

# PLACEHOLDER pay structure (monthly, naira). The hospital has not confirmed its real scales or allowances.
SCALE_BASE, SCALE_PER_LEVEL, SCALE_STEP_RATE, SCALE_STEPS = 50_000, 22_000, 0.03, 9
# (code, name, kind, method, percent_bps, sort_order)
PLACEHOLDER_COMPONENTS = [
    ("HOUSING", "Housing allowance", "earning", "percent_of_basic", 2500, 10),
    ("TRANSPORT", "Transport allowance", "earning", "percent_of_basic", 1000, 20),
    ("UTILITY", "Utility allowance", "earning", "percent_of_basic", 500, 30),
    ("HAZARD", "Hazard allowance", "earning", "percent_of_basic", 1500, 40),
    ("PENSION", "Pension (employee)", "deduction", "percent_of_basic", 800, 10),
    ("NHF", "National Housing Fund", "deduction", "percent_of_basic", 250, 20),
    ("TAX", "Income tax (placeholder flat rate)", "deduction", "percent_of_gross", 1000, 30),
]

# A tiny but valid one-page PDF, used as the content of every mock document.
MOCK_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")

# (name, type, status, expiry) mirroring the frontend's mock documents.
MOCK_DOCUMENTS = [
    ("Medical License", "License", "Verified", date(2027, 8, 15)),
    ("NYSC Certificate", "NYSC", "Verified", None),
    ("Promotion Letter 2024", "Promotion", "Verified", None),
    ("Appointment Letter", "Appointment", "Verified", None),
    ("First Aid Certificate", "Certificate", "Pending", date(2026, 12, 1)),
    ("Passport Photo", "Passport", "Verified", None),
    ("National ID", "NIN", "Verified", None),
    ("CV - 2025", "CV", "Rejected", None),
]

DEPARTMENTS = [
    ("MED", "Internal Medicine"), ("SUR", "Surgery"), ("PED", "Pediatrics"), ("RAD", "Radiology"),
    ("NUR", "Nursing"), ("PHA", "Pharmacy"), ("LAB", "Laboratory"), ("ADM", "Administration"),
    ("HRM", "Human Resources"), ("ICT", "IT"),
]

# (file_number, title, first, last, dept name, designation, grade level, step, status, avatar index)
FRONTEND_STAFF = [
    ("YAB-MED-042", "Dr.", "Sarah", "Adebayo", "Internal Medicine", "Medical Officer", 7, 2, "Active", 47),
    ("YAB-SUR-018", "Dr.", "Tunde", "Bakare", "Surgery", "Senior Medical Officer", 8, 3, "Active", 11),
    ("YAB-PED-033", "Dr.", "Ngozi", "Eze", "Pediatrics", "Medical Officer", 7, 2, "On Leave", 45),
    ("YAB-RAD-021", "Dr.", "Ibrahim", "Musa", "Radiology", "Senior Medical Officer", 8, 1, "Active", 13),
    ("YAB-NUR-112", None, "Grace", "Okonkwo", "Nursing", "Nursing Officer I", 6, 4, "Active", 48),
    ("YAB-PHA-009", None, "Blessing", "Adamu", "Pharmacy", "Pharmacist", 7, 1, "Active", 49),
    ("YAB-LAB-055", None, "David", "Chukwu", "Laboratory", "Medical Laboratory Scientist", 6, 3, "Active", 15),
    ("YAB-ADM-003", None, "Fatima", "Yusuf", "Administration", "Administrative Officer", 9, 2, "Active", 44),
    ("YAB-HOD-007", "Dr.", "Emeka", "Okafor", "Internal Medicine", "Head of Department", 9, 4, "Active", 12),
    ("HR-001", "Mrs.", "Aisha", "Bello", "Human Resources", "HR Manager", 10, 3, "Active", 32),
    ("DIR-001", "Dr.", "Chukwu", "Mensah", "Administration", "Director of Administration", 12, 5, "Active", 14),
]

FIRST = ["Chidi", "Amaka", "Yusuf", "Funke", "Emmanuel", "Hauwa", "Segun", "Ifeoma", "Musa", "Bisi", "Kelechi", "Zainab",
         "Obinna", "Halima", "Femi", "Adaeze", "Sani", "Titi", "Ikenna", "Rakiya", "Tayo", "Nneka", "Bala", "Chiamaka"]
LAST = ["Ogunleye", "Nwosu", "Abubakar", "Balogun", "Okoro", "Lawal", "Adeyemi", "Eze", "Garba", "Ojo", "Udoh", "Salami",
        "Onyeama", "Danjuma", "Afolabi", "Igwe", "Bello", "Fashola", "Mohammed", "Ibe"]
DESIGNATIONS = {
    "Internal Medicine": ["Medical Officer", "Registrar", "Consultant"], "Surgery": ["Medical Officer", "Registrar"],
    "Pediatrics": ["Medical Officer", "Registrar"], "Radiology": ["Radiographer", "Medical Officer"],
    "Nursing": ["Nursing Officer II", "Nursing Officer I", "Senior Nursing Officer"],
    "Pharmacy": ["Pharmacist", "Pharmacy Technician"], "Laboratory": ["Medical Laboratory Scientist", "Lab Technician"],
    "Administration": ["Administrative Officer", "Executive Officer"], "Human Resources": ["HR Officer"],
    "IT": ["ICT Officer"],
}


async def seed_payroll(session: AsyncSession, users: dict[str, User], grades: dict[int, GradeLevel]) -> None:
    """Placeholder pay structure, one paid run for last month so the demo staff member has a payslip, and a few
    notifications so the bell is not empty."""
    from app.modules.payroll import service as payroll

    for lv, g in grades.items():
        for step in range(1, SCALE_STEPS + 1):
            naira = round((SCALE_BASE + (lv - 1) * SCALE_PER_LEVEL) * (1 + SCALE_STEP_RATE * (step - 1)), -2)
            session.add(SalaryScale(grade_level_id=g.id, step=step, basic_kobo=int(naira) * 100))
    for code, name, kind, method, bps, order in PLACEHOLDER_COMPONENTS:
        session.add(PayComponent(code=code, name=name, kind=kind, method=method, rate_bps=bps, sort_order=order))
    await session.flush()

    last = date.today().replace(day=1) - timedelta(days=1)
    now = utcnow()
    run = PayrollRun(year=last.year, month=last.month, period=payroll.period_int(last.year, last.month),
                     created_by=users["hr"].id, status="Paid", approved_by=users["director"].id, approved_at=now,
                     paid_by=users["hr"].id, paid_at=now)
    session.add(run)
    await session.flush()
    await payroll.calculate(session, run)
    label = payroll.label(run.year, run.month)
    session.add_all([
        Notification(user_id=users["staff"].id, type="payroll", title="Payslip available",
                     text=f"Your {label} payslip is ready.", link="/salary", created_at=now),
        Notification(user_id=users["staff"].id, type="document", title="Document verified",
                     text="Your Appointment Letter was verified.", link="/documents", created_at=now - timedelta(days=3),
                     read_at=now - timedelta(days=2)),
        Notification(user_id=users["hr"].id, type="document", title="Document awaiting verification",
                     text="A First Aid Certificate was uploaded.", link="/documents", created_at=now - timedelta(hours=5)),
    ])


async def seed_queries_and_performance(session: AsyncSession, users: dict[str, User], sarah: Staff, hod: Staff) -> None:
    """Mirrors the frontend's mock queries, goals and appraisal history, and opens this year's appraisal cycle."""
    from app.modules.performance import rules as prules
    from app.modules.performance.router import _add_missing

    now = utcnow()
    today = date.today()

    def query(title, text, severity, status, days_ago, issuer, due_in=3, outcome=None, resolver=None, appealed=False):
        q = StaffQuery(staff_id=sarah.id, title=title, description=text, severity=severity, status=status,
                       issued_by=users[issuer].id, reply_due=today + timedelta(days=due_in), outcome=outcome,
                       created_at=now - timedelta(days=days_ago), updated_at=now - timedelta(days=days_ago))
        session.add(q)
        return q, resolver, appealed

    rows = [
        query("Attendance Irregularity", "You were absent from the ward on three occasions last week without notice.",
              "Medium", "Pending Reply", 10, "hod", due_in=2),
        query("Late Submission of Report", "The monthly clinical report was submitted two weeks late.", "Low", "Resolved",
              150, "hod", due_in=-140, outcome="Cleared", resolver="hr"),
        query("Patient Complaint", "A patient's family complained about communication during a ward round.", "High",
              "Appealed", 200, "hr", due_in=-190, outcome="Warning", resolver="director", appealed=True),
    ]
    await session.flush()
    for q, resolver, appealed in rows:
        who = next(r for r in users if users[r].id == q.issued_by)
        session.add(QueryEvent(query_id=q.id, kind="issued", by_user=q.issued_by, by_email=f"{who}@hospital.test",
                               by_role=who, text=q.description, at=q.created_at))
        if q.status == "Pending Reply":
            continue
        session.add(QueryEvent(query_id=q.id, kind="reply", by_user=users["staff"].id, by_email="staff@hospital.test",
                               by_role="staff", text="Please see my explanation; I had informed the ward sister.",
                               at=q.created_at + timedelta(days=2)))
        q.replied_at = q.created_at + timedelta(days=2)
        q.resolved_by, q.resolved_at = users[resolver].id, q.created_at + timedelta(days=5)
        session.add(QueryEvent(query_id=q.id, kind="resolved", by_user=users[resolver].id,
                               by_email=f"{resolver}@hospital.test", by_role=resolver, text="Reviewed with the reply.",
                               detail=q.outcome, at=q.resolved_at))
        if appealed:
            q.appealed_at = q.resolved_at + timedelta(days=3)
            session.add(QueryEvent(query_id=q.id, kind="appeal", by_user=users["staff"].id, by_email="staff@hospital.test",
                                   by_role="staff", text="The complaint did not reflect what happened.", at=q.appealed_at))

    session.add_all([
        PerformanceGoal(staff_id=sarah.id, title="Clinical quality", target="Keep audit score at or above 95%",
                        progress=96, status="Active", created_by=users["staff"].id),
        PerformanceGoal(staff_id=sarah.id, title="Patient feedback", target="Maintain a 90%+ satisfaction rating",
                        progress=92, status="Active", created_by=users["hod"].id),
        PerformanceGoal(staff_id=sarah.id, title="Training completion", target="Complete the annual mandatory modules",
                        progress=88, status="Active", created_by=users["staff"].id),
    ])

    # Two past cycles with finalised results for the demo staff member, so the history chart has data.
    for yr, scores in ((today.year - 2, [90, 88, 95, 92, 85, 90]), (today.year - 1, [94, 92, 96, 93, 90, 95])):
        cycle = AppraisalCycle(name=f"Annual Appraisal {yr}", year=yr, status="Closed", start_date=date(yr, 1, 1),
                               end_date=date(yr, 12, 31), self_deadline=date(yr, 12, 1),
                               criteria=[dict(c) for c in prules.DEFAULT_CRITERIA],
                               rating_bands=[dict(b) for b in prules.DEFAULT_BANDS], created_by=users["hr"].id,
                               opened_at=now - timedelta(days=365 * (today.year - yr)), closed_at=now - timedelta(days=365 * (today.year - yr) - 30))
        session.add(cycle)
        await session.flush()
        mine = {c["key"]: v for c, v in zip(cycle.criteria, scores)}
        total = prules.weighted_x100(mine, cycle.criteria)
        session.add(Appraisal(cycle_id=cycle.id, staff_id=sarah.id, department="Internal Medicine",
                              appraiser_user_id=users["hod"].id, status="Finalised", self_scores=mine, self_comment="Good year.",
                              self_submitted_at=cycle.opened_at, appraiser_scores=mine, appraiser_comment="Consistently strong.",
                              reviewed_by=users["hod"].id, reviewed_at=cycle.opened_at, score_x100=total,
                              rating=prules.rating_for(total, cycle.rating_bands), finalised_by=users["hr"].id,
                              finalised_at=cycle.closed_at, acknowledged_at=cycle.closed_at))

    # This year's cycle is open: the demo staff member has yet to self-assess, one colleague is waiting for the HOD.
    cur = AppraisalCycle(name=f"Annual Appraisal {today.year}", year=today.year, status="Open",
                         start_date=date(today.year, 1, 1), end_date=date(today.year, 12, 31),
                         self_deadline=date(today.year, 12, 1), criteria=[dict(c) for c in prules.DEFAULT_CRITERIA],
                         rating_bands=[dict(b) for b in prules.DEFAULT_BANDS], created_by=users["hr"].id, opened_at=now)
    session.add(cur)
    await session.flush()
    await _add_missing(session, cur)
    waiting = (await session.scalars(select(Appraisal).join(Staff, Staff.id == Appraisal.staff_id).where(
        Appraisal.cycle_id == cur.id, Staff.department_id == hod.department_id, Appraisal.staff_id.notin_([sarah.id, hod.id]))
        .order_by(Staff.file_number).limit(1))).first()
    if waiting:
        waiting.status, waiting.self_submitted_at = "Self-Assessed", now
        waiting.self_scores = {c["key"]: 80 for c in cur.criteria}
        waiting.self_comment = "Met all my targets this year."


async def seed(session: AsyncSession, extra_staff: int = 40) -> None:
    if await session.scalar(select(Department.id).limit(1)):
        print("Database already has data; use --reset to start over.")
        return
    rng = random.Random(42)

    depts = {n: Department(code=c, name=n) for c, n in DEPARTMENTS}
    grades = {lv: GradeLevel(scale="CONHESS", level=lv, code=f"CONHESS {lv}") for lv in range(1, 16)}
    session.add_all([*depts.values(), *grades.values()])

    # PLACEHOLDER entitlements; the hospital has not yet confirmed its leave policy.
    annual = LeaveType(code="annual", name="Annual Leave", annual_entitlement_days=15)
    sick = LeaveType(code="sick", name="Sick Leave", annual_entitlement_days=10, allow_backdate=True)
    comp = LeaveType(code="compassionate", name="Compassionate Leave", annual_entitlement_days=5, allow_backdate=True)
    session.add_all([annual, sick, comp])
    await session.flush()
    session.add_all([
        LeaveWorkflowStep(leave_type_id=None, step_order=1, role="hod", label="HOD Approved"),
        LeaveWorkflowStep(leave_type_id=None, step_order=2, role="hr", label="HR Approved"),
        LeaveWorkflowStep(leave_type_id=None, step_order=3, role="director", label="DA Approved"),
    ])

    people: dict[str, Staff] = {}
    for fn, title, first, last, dept, desig, lv, step, status, img in FRONTEND_STAFF:
        s = Staff(
            file_number=fn, title=title, first_name=first, last_name=last, department_id=depts[dept].id,
            designation=desig, grade_level_id=grades[lv].id, step=step, status=status,
            email=f"{first}.{last}@hospital.test".lower(), phone=f"+234 80{rng.randint(1,9)} {rng.randint(100,999)} {rng.randint(1000,9999)}",
            avatar_url=f"https://i.pravatar.cc/150?img={img}", nationality="Nigerian",
            employment_date=date(2021, 3, 15), confirmation_date=date(2022, 3, 15),
            gender="Female" if first in ("Sarah", "Ngozi", "Grace", "Blessing", "Fatima", "Aisha") else "Male",
            date_of_birth=date(1988, 4, 12) if first == "Sarah" else date(rng.randint(1965, 1995), rng.randint(1, 12), rng.randint(1, 28)),
            state_of_origin="Lagos", marital_status="Married",
        )
        session.add(s)
        people[fn] = s
    await session.flush()

    hod = people["YAB-HOD-007"]
    for fn, s in people.items():
        if s.department_id == depts["Internal Medicine"].id and s is not hod:
            s.supervisor_id = hod.id

    # Extra generated staff so the directory has something to page through.
    counters = {n: 100 for n in depts}
    for i in range(extra_staff):
        dept = rng.choice(["Internal Medicine", "Internal Medicine", "Nursing", "Nursing", "Surgery", "Pediatrics",
                           "Radiology", "Pharmacy", "Laboratory", "Administration"])
        counters[dept] += 1
        first, last = rng.choice(FIRST), rng.choice(LAST)
        lv = rng.choice([5, 6, 6, 7, 7, 7, 8, 8, 9, 10])
        fn = f"YAB-{depts[dept].code}-{counters[dept]:03d}"
        s = Staff(
            file_number=fn, title="Dr." if dept in ("Internal Medicine", "Surgery", "Pediatrics") and lv >= 7 else None,
            first_name=first, last_name=last, department_id=depts[dept].id, designation=rng.choice(DESIGNATIONS[dept]),
            grade_level_id=grades[lv].id, step=rng.randint(1, 6), status=rng.choice(["Active"] * 9 + ["On Leave"]),
            email=f"{first}.{last}.{counters[dept]}@hospital.test".lower(), nationality="Nigerian",
            gender=rng.choice(["Female", "Male"]), state_of_origin=rng.choice(["Lagos", "Ogun", "Kano", "Enugu", "Rivers", "Oyo"]),
            date_of_birth=date(rng.randint(1964, 1998), rng.randint(1, 12), rng.randint(1, 28)),
            employment_date=date(rng.randint(2008, 2025), rng.randint(1, 12), rng.randint(1, 28)),
            supervisor_id=hod.id if dept == "Internal Medicine" else None,
        )
        session.add(s)
    await session.flush()

    # Richer profile for the demo staff member (mirrors the frontend's MyProfile mock).
    sarah = people["YAB-MED-042"]
    sarah.address = "12 Marina Road, Lagos Island"
    session.add_all([
        Education(staff_id=sarah.id, institution="University of Lagos", qualification="MBBS", year=2015),
        Education(staff_id=sarah.id, institution="National Postgraduate Medical College", qualification="FMCP Internal Medicine", year=2020),
        Education(staff_id=sarah.id, institution="Harvard Medical School (Online)", qualification="Healthcare Leadership", year=2023),
        MedicalLicense(staff_id=sarah.id, license_number="MDCN/2015/4421", issued_on=date(2015, 9, 1), expires_on=date(2027, 8, 15), body="MDCN"),
        EmergencyContact(staff_id=sarah.id, name="Mr. Adebayo Samuel", relationship_="Spouse", phone="+234 803 111 2233", email="samuel.a@example.test"),
        Dependant(staff_id=sarah.id, name="Samuel Jr.", relationship_="Son", date_of_birth=date(2019, 6, 10)),
        Dependant(staff_id=sarah.id, name="Grace Adebayo", relationship_="Daughter", date_of_birth=date(2021, 11, 22)),
        BankAccount(staff_id=sarah.id, bank_name="First Bank of Nigeria", account_name="Sarah Adebayo",
                    account_number_enc=encrypt_field("3045678901"), bvn_enc=encrypt_field("22123456790")),
    ])

    # Demo accounts. Every account but the super admin belongs to a staff record.
    pw = hash_password(DEMO_PASSWORD)
    accounts = [("staff", "YAB-MED-042"), ("hod", "YAB-HOD-007"), ("hr", "HR-001"), ("director", "DIR-001"), ("superadmin", None)]
    users: dict[str, User] = {}
    for role, fn in accounts:
        u = User(email=f"{role}@hospital.test", password_hash=pw, role=role, must_change_password=False,
                 staff_id=people[fn].id if fn else None)
        session.add(u)
        users[role] = u
    await session.flush()

    # Leave history for the demo staff member: fully approved through the whole chain.
    now = utcnow()

    def approved(staff: Staff, ltype: LeaveType, start: date, end: date, reason: str):
        r = LeaveRequest(staff_id=staff.id, leave_type_id=ltype.id, start_date=start, end_date=end,
                         days=(end - start).days + 1, reason=reason, status="Approved", created_at=now - timedelta(days=400))
        session.add(r)
        return r

    history = [
        approved(sarah, annual, date(2026, 8, 10), date(2026, 8, 19), "Family vacation"),
        approved(sarah, sick, date(2026, 3, 12), date(2026, 3, 14), "Illness"),
        approved(sarah, comp, date(2025, 11, 2), date(2025, 11, 5), "Family bereavement"),
        approved(sarah, annual, date(2025, 7, 15), date(2025, 7, 24), "Rest"),
    ]
    await session.flush()
    for r in history:
        for order, role in ((1, "hod"), (2, "hr"), (3, "director")):
            session.add(LeaveDecision(request_id=r.id, step_order=order, role=role, decided_by=users[role].id,
                                      decision="approve", comment=None, decided_at=now - timedelta(days=390)))

    # Pending requests so the HOD / HR / director approval screens have content.
    med_staff = (await session.scalars(select(Staff).where(
        Staff.department_id == depts["Internal Medicine"].id, Staff.id.notin_([sarah.id, hod.id]),
        Staff.status == "Active").order_by(Staff.file_number).limit(4))).all()
    reasons = [("Family vacation", annual, 12, 10), ("Medical appointment", sick, 20, 3),
               ("Family bereavement", comp, 25, 4), ("Wedding", annual, 40, 5)]
    for s, (why, lt, offset, days) in zip(med_staff, reasons):
        start = date.today() + timedelta(days=offset)
        session.add(LeaveRequest(staff_id=s.id, leave_type_id=lt.id, start_date=start, end_date=start + timedelta(days=days - 1),
                                 days=days, reason=why, status="Pending", current_step_order=1, current_step_role="hod"))
    # One already past the HOD, waiting on HR.
    if med_staff:
        s = med_staff[-1]
        start = date.today() + timedelta(days=70)
        r = LeaveRequest(staff_id=s.id, leave_type_id=annual.id, start_date=start, end_date=start + timedelta(days=4), days=5,
                         reason="Rest", status="Pending", current_step_order=2, current_step_role="hr")
        session.add(r)
        await session.flush()
        session.add(LeaveDecision(request_id=r.id, step_order=1, role="hod", decided_by=users["hod"].id,
                                  decision="approve", comment="Cover arranged", decided_at=now))

    # Mock documents for the demo staff member. Files are only written when using local storage;
    # with R2 there is nothing to seed because rows without real objects would break downloads.
    storage = get_storage()
    if isinstance(storage, LocalStorage):
        for name, dtype, status, expiry in MOCK_DOCUMENTS:
            doc_id = uuid.uuid4()
            key = f"documents/{sarah.id}/{doc_id}"
            storage.write(key, MOCK_PDF)
            session.add(Document(
                id=doc_id, staff_id=sarah.id, doc_type=dtype, name=name, original_filename=f"{name}.pdf",
                content_type="application/pdf", declared_size=len(MOCK_PDF), size_bytes=len(MOCK_PDF), object_key=key,
                status=status, expiry_date=expiry, uploaded_by=users["staff"].id,
                verified_by=users["hr"].id if status != "Pending" else None,
                verified_by_email="hr@hospital.test" if status != "Pending" else None,
                verified_at=now if status != "Pending" else None,
                rejection_reason="Scan is not legible; please upload a clearer copy." if status == "Rejected" else None))

    await seed_payroll(session, users, grades)
    await seed_queries_and_performance(session, users, sarah, hod)
    await session.commit()
    total = len(FRONTEND_STAFF) + extra_staff
    print(f"Seeded {len(DEPARTMENTS)} departments, {total} staff, {len(accounts)} demo accounts "
          f"({', '.join(r + '@hospital.test' for r, _ in accounts)}; password: {DEMO_PASSWORD}).")


async def main() -> None:
    if settings.environment == "production":
        sys.exit("Refusing to seed mock data in production.")
    from app.core.db import SessionLocal, engine

    if "--reset" in sys.argv:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as session:
        await seed(session)
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
