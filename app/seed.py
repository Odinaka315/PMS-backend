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
    LeaveType, LeaveWorkflowStep, MedicalLicense, Staff, User,
)

DEMO_PASSWORD = "hospital123"

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
