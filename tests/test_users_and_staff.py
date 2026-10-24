from app.core.db import normalize_database_url

from .conftest import API, dept_id, login, make_person


async def test_only_hr_and_superadmin_can_create_accounts(client, hr, hod, director, superadmin):
    staff, _, staff_headers = await make_person(client, hr)
    body = {"email": "x@hospital.test", "role": "staff", "staff_id": staff["id"]}
    for h in (staff_headers, hod, director):
        assert (await client.post(f"{API}/users", headers=h, json=body)).status_code == 403


async def test_hr_cannot_mint_privileged_accounts_but_superadmin_can(client, hr, superadmin):
    staff, _, _ = await make_person(client, hr, dept="Administration", account=False)
    r = await client.post(f"{API}/users", headers=hr, json={"email": "dir2@hospital.test", "role": "director", "staff_id": staff["id"]})
    assert r.status_code == 403
    r = await client.post(f"{API}/users", headers=superadmin, json={"email": "dir2@hospital.test", "role": "director", "staff_id": staff["id"]})
    assert r.status_code == 201 and r.json()["temporary_password"]
    # The generated temporary password works and forces a change.
    tok = await client.post(f"{API}/auth/login", json={"email": "dir2@hospital.test", "password": r.json()["temporary_password"]})
    assert tok.status_code == 200 and tok.json()["user"]["must_change_password"] is True


async def test_duplicate_email_and_double_account(client, hr):
    staff, email, _ = await make_person(client, hr)
    r = await client.post(f"{API}/users", headers=hr, json={"email": email, "role": "staff", "staff_id": staff["id"]})
    assert r.status_code == 409


async def test_staff_role_cannot_browse_directory_but_sees_self(client, hr):
    staff, _, h = await make_person(client, hr)
    assert (await client.get(f"{API}/staff", headers=h)).status_code == 403
    me = await client.get(f"{API}/staff/me", headers=h)
    assert me.status_code == 200 and me.json()["file_number"] == staff["file_number"]
    other = await client.get(f"{API}/staff/YAB-SUR-018", headers=h)
    assert other.status_code == 404  # existence is not leaked


async def test_hod_is_scoped_to_own_department(client, hod):
    r = await client.get(f"{API}/staff", headers=hod, params={"size": 100})
    assert r.status_code == 200 and r.json()["total"] > 0
    assert {i["dept"] for i in r.json()["items"]} == {"Internal Medicine"}
    assert (await client.get(f"{API}/staff/YAB-SUR-018", headers=hod)).status_code == 404
    assert (await client.get(f"{API}/staff/YAB-MED-042", headers=hod)).status_code == 200


async def test_directory_search_and_filters(client, hr):
    r = await client.get(f"{API}/staff", headers=hr, params={"q": "adebayo"})
    assert [i["file_number"] for i in r.json()["items"]] == ["YAB-MED-042"]
    r = await client.get(f"{API}/staff", headers=hr, params={"grade": "CONHESS 12"})
    assert [i["name"] for i in r.json()["items"]] == ["Dr. Chukwu Mensah"]
    r = await client.get(f"{API}/staff", headers=hr, params={"size": 5, "page": 2})
    assert r.json()["page"] == 2 and len(r.json()["items"]) == 5


async def test_hr_creates_updates_and_audit_trail_records_it(client, hr):
    dept = await dept_id(client, hr, "Pharmacy")
    r = await client.post(f"{API}/staff", headers=hr, json={
        "first_name": "Ada", "last_name": "Obi", "department_id": dept,
        "employment_date": "2020-01-10", "date_of_birth": "1990-05-01"})
    assert r.status_code == 201
    p = r.json()
    assert p["file_number"].startswith("YAB-PHA-")
    # Retirement is whichever comes first: age 60 (2050-05-01) or 35 years' service (2055-01-10).
    assert p["computed"]["retirement_date"] == "2050-05-01"

    r = await client.patch(f"{API}/staff/{p['file_number']}", headers=hr, json={"phone": "+234 800 111 2222", "step": 3})
    assert r.status_code == 200 and r.json()["personal"]["phone"] == "+234 800 111 2222" and r.json()["employment"]["step"] == 3

    trail = (await client.get(f"{API}/staff/{p['file_number']}/audit", headers=hr)).json()["items"]
    actions = [a["action"] for a in trail]
    assert "staff_created" in actions and "staff_updated" in actions
    updated = next(a for a in trail if a["action"] == "staff_updated")
    assert updated["after"] == {"phone": "+234 800 111 2222", "step": 3} and updated["by"] == "hr@hospital.test"


async def test_unknown_references_and_duplicate_file_numbers_rejected(client, hr):
    dept = await dept_id(client, hr, "Pharmacy")
    bad = await client.post(f"{API}/staff", headers=hr, json={"first_name": "A", "last_name": "B",
                                                              "department_id": "00000000-0000-0000-0000-000000000000"})
    assert bad.status_code == 422
    dup = await client.post(f"{API}/staff", headers=hr, json={"first_name": "A", "last_name": "B", "department_id": dept,
                                                              "file_number": "YAB-MED-042"})
    assert dup.status_code == 409


async def test_only_hr_can_edit_staff_records(client, hr, hod):
    _, _, h = await make_person(client, hr)
    for headers in (h, hod):
        assert (await client.patch(f"{API}/staff/YAB-MED-042", headers=headers, json={"step": 5})).status_code == 403
        assert (await client.post(f"{API}/staff", headers=headers, json={})).status_code == 403


async def test_self_service_edit_is_limited_to_safe_fields(client, hr):
    staff, _, h = await make_person(client, hr)
    r = await client.patch(f"{API}/staff/me", headers=h, json={"phone": "+234 700 000 0000", "grade_level_id": None, "step": 9})
    assert r.status_code == 200
    assert r.json()["personal"]["phone"] == "+234 700 000 0000"
    assert r.json()["employment"]["step"] is None  # 'step' is not self-editable; the field was ignored


async def test_bank_details_are_encrypted_masked_and_not_audited(client, hr, hod):
    staff, _, h = await make_person(client, hr)
    r = await client.put(f"{API}/staff/{staff['file_number']}/bank", headers=hr, json={
        "bank_name": "Access Bank", "account_name": "Test Person", "account_number": "0123456789", "bvn": "12345678901"})
    assert r.status_code == 204
    own = (await client.get(f"{API}/staff/me", headers=h)).json()["bank"]
    assert own["account_number"] == "0123456789" and own["bvn"] == "12*******01"
    assert (await client.get(f"{API}/staff/{staff['file_number']}", headers=hr)).json()["bank"]["bank_name"] == "Access Bank"
    # An HOD in the same department sees no bank block at all.
    assert (await client.get(f"{API}/staff/{staff['file_number']}", headers=hod)).json()["bank"] is None
    trail = (await client.get(f"{API}/staff/{staff['file_number']}/audit", headers=hr)).json()["items"]
    blob = str(trail)
    assert "0123456789" not in blob and "12345678901" not in blob

    # The raw column is ciphertext, not the number.
    from sqlalchemy import select
    from app.core.db import SessionLocal
    from app.models import BankAccount
    async with SessionLocal() as s:
        row = (await s.scalars(select(BankAccount).where(BankAccount.staff_id == __import__("uuid").UUID(staff["id"])))).one()
        assert "0123456789" not in row.account_number_enc


async def test_viewing_someone_elses_file_is_audited(client, hr, hod):
    await client.get(f"{API}/staff/YAB-MED-042", headers=hod)
    trail = (await client.get(f"{API}/staff/YAB-MED-042/audit", headers=hr)).json()["items"]
    assert any(a["action"] == "staff_viewed" and a["by"] == "hod@hospital.test" for a in trail)


async def test_audit_trail_visibility(client, hr, hod):
    staff, _, h = await make_person(client, hr)
    assert (await client.get(f"{API}/staff/{staff['file_number']}/audit", headers=h)).status_code == 200  # own trail
    assert (await client.get(f"{API}/staff/YAB-MED-042/audit", headers=h)).status_code == 403
    assert (await client.get(f"{API}/staff/YAB-MED-042/audit", headers=hod)).status_code == 403


async def test_profile_sections_and_license_status(client, hr):
    r = await client.get(f"{API}/staff/YAB-MED-042", headers=hr)
    p = r.json()
    assert len(p["education"]) == 3 and len(p["dependants"]) == 2
    assert p["license"]["license_number"] == "MDCN/2015/4421" and p["license"]["status"] in ("Active", "Expiring Soon")
    assert p["emergency_contact"]["relationship"] == "Spouse"

    staff, _, h = await make_person(client, hr)
    e = await client.post(f"{API}/staff/me/education".replace("me", staff["file_number"]), headers=h,
                          json={"institution": "UNILAG", "qualification": "BSc", "year": 2012})
    assert e.status_code == 201
    other = await client.post(f"{API}/staff/YAB-MED-042/education", headers=h,
                              json={"institution": "Some University", "qualification": "Some Degree"})
    assert other.status_code == 403


def test_neon_url_normalisation():
    url, args = normalize_database_url("postgresql://u:p@ep-cool-123-pooler.eu-west-2.aws.neon.tech/db?sslmode=require&channel_binding=require")
    assert url == "postgresql+asyncpg://u:p@ep-cool-123-pooler.eu-west-2.aws.neon.tech/db"
    assert args == {"ssl": "require", "statement_cache_size": 0}
    url, args = normalize_database_url("postgres://u:p@ep-cool-123.eu-west-2.aws.neon.tech/db?sslmode=require")
    assert url.startswith("postgresql+asyncpg://") and args == {"ssl": "require"}
    assert normalize_database_url("sqlite+aiosqlite:///x.db") == ("sqlite+aiosqlite:///x.db", {})
