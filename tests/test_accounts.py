"""Automatic account creation when HR creates a staff record, the initial password, and the forced first-login change."""
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.config import Settings, settings
from app.core.db import SessionLocal
from app.core.security import utcnow
from app.models import AuditLog, Staff, User

from .conftest import API, dept_id, login, make_person

INITIAL = "hospital123"  # the configured shared initial password in development/tests


def unique_email():
    return f"new.{uuid.uuid4().hex[:8]}@hospital.test"


async def create_staff(client, hr, **extra):
    body = {"first_name": "Ngozi", "last_name": f"Newhire{uuid.uuid4().hex[:6]}",
            "department_id": await dept_id(client, hr, "Pharmacy"), **extra}
    return await client.post(f"{API}/staff", headers=hr, json=body)


async def first_login(client, email, password=INITIAL):
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    return r


def bearer(r):
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def db_user(email):
    async with SessionLocal() as s:
        return await s.scalar(select(User).where(User.email == email))


# ------------------------------------------------------------------ automatic creation
async def test_creating_staff_creates_a_login_with_the_initial_password(client, hr):
    email = unique_email()
    r = await create_staff(client, hr, email=email)
    assert r.status_code == 201, r.text
    body = r.json()
    acct = body["account"]
    assert acct["email"] == email and acct["must_change_password"] is True and acct["initial_password_expires_at"]
    assert acct["temporary_password"] is None  # shared mode: the configured password is not echoed back
    assert body["account_note"] is None

    # The account exists, is a plain staff account linked to the new record, and the password is stored hashed.
    u = await db_user(email)
    assert u.role == "staff" and str(u.staff_id) == body["id"] and u.is_active
    assert u.password_hash.startswith("$argon2") and INITIAL not in u.password_hash

    # And the new person can sign in with the initial password.
    r = await first_login(client, email)
    assert r.status_code == 200
    me = r.json()["user"]
    assert me["must_change_password"] is True and me["file_number"] == body["file_number"] and me["role"] == "staff"


async def test_login_name_is_normalised_to_lowercase(client, hr):
    mixed = f"Mixed.{uuid.uuid4().hex[:6]}@Hospital.Test"
    r = await create_staff(client, hr, email=mixed)
    assert r.json()["account"]["email"] == mixed.lower()
    assert (await first_login(client, mixed.upper())).status_code == 200


# ------------------------------------------------------------------ forced first-login change
async def test_new_account_is_locked_to_the_password_change_until_done(client, hr):
    email = unique_email()
    await create_staff(client, hr, email=email)
    h = bearer(await first_login(client, email))

    # Everything is refused with a machine-readable code the frontend can act on...
    for path in ("/staff/me", "/leave/types", "/leave/balance", "/departments", "/reports/dashboard"):
        r = await client.get(f"{API}{path}", headers=h)
        assert r.status_code == 403, path
        assert r.json()["detail"]["code"] == "password_change_required"
    # ...except who-am-i, sign-out and the change itself.
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 200

    changed = await client.post(f"{API}/auth/change-password", headers=h,
                                json={"current_password": INITIAL, "new_password": "My-own-passw0rd"})
    assert changed.status_code == 200
    fresh = bearer(changed)
    assert changed.json()["user"]["must_change_password"] is False
    assert (await client.get(f"{API}/staff/me", headers=fresh)).status_code == 200  # free to use the system

    # The old initial password no longer works; the new one does.
    assert (await first_login(client, email)).status_code == 401
    assert (await first_login(client, email, "My-own-passw0rd")).status_code == 200


async def test_the_old_session_cannot_be_refreshed_after_the_change(client, hr):
    email = unique_email()
    await create_staff(client, hr, email=email)
    first = (await first_login(client, email)).json()
    await client.post(f"{API}/auth/change-password", headers={"Authorization": f"Bearer {first['access_token']}"},
                      json={"current_password": INITIAL, "new_password": "My-own-passw0rd"})
    assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})).status_code == 401


@pytest.mark.parametrize("new,expected", [
    (INITIAL, 422),            # the initial password itself
    ("short", 422),            # too short
    ("HOSPITAL123", 422),      # initial password in another case
    ("password", 422),
])
async def test_new_password_rules(client, hr, new, expected):
    email = unique_email()
    await create_staff(client, hr, email=email)
    h = bearer(await first_login(client, email))
    r = await client.post(f"{API}/auth/change-password", headers=h, json={"current_password": INITIAL, "new_password": new})
    assert r.status_code == expected
    # A failed attempt leaves the account locked to the password change.
    assert (await client.get(f"{API}/staff/me", headers=h)).status_code == 403


async def test_wrong_current_password_and_same_password(client, hr):
    email = unique_email()
    await create_staff(client, hr, email=email)
    h = bearer(await first_login(client, email))
    assert (await client.post(f"{API}/auth/change-password", headers=h,
                              json={"current_password": "nope-nope", "new_password": "My-own-passw0rd"})).status_code == 400
    # Once they have a password of their own, "changing" it to the same value is refused.
    changed = await client.post(f"{API}/auth/change-password", headers=h,
                                json={"current_password": INITIAL, "new_password": "My-own-passw0rd"})
    assert changed.status_code == 200
    same = await client.post(f"{API}/auth/change-password", headers=bearer(changed),
                             json={"current_password": "My-own-passw0rd", "new_password": "My-own-passw0rd"})
    assert same.status_code == 422 and "different" in same.json()["detail"]


async def test_the_initial_password_expires_if_unused(client, hr):
    email = unique_email()
    await create_staff(client, hr, email=email)
    async with SessionLocal() as s:  # pretend 15 days have passed
        u = await s.scalar(select(User).where(User.email == email))
        u.password_expires_at = utcnow() - timedelta(days=1)
        await s.commit()
    r = await first_login(client, email)
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    # A wrong password still gets the same generic answer as any other account, so expiry is not leaked.
    assert (await first_login(client, email, "wrong-wrong")).json()["detail"] == "Incorrect email or password"

    # HR resets it; the person can then get in and change it.
    uid = (await client.get(f"{API}/users", headers=hr, params={"q": email})).json()["items"][0]["id"]
    temp = (await client.post(f"{API}/users/{uid}/reset-password", headers=hr)).json()["temporary_password"]
    assert (await first_login(client, email, temp)).status_code == 200


async def test_expiry_clears_once_the_user_picks_their_own_password(client, hr):
    email = unique_email()
    await create_staff(client, hr, email=email)
    h = bearer(await first_login(client, email))
    await client.post(f"{API}/auth/change-password", headers=h,
                      json={"current_password": INITIAL, "new_password": "My-own-passw0rd"})
    assert (await db_user(email)).password_expires_at is None
    async with SessionLocal() as s:  # even long afterwards, their own password keeps working
        u = await s.scalar(select(User).where(User.email == email))
        u.updated_at = utcnow() - timedelta(days=400)
        await s.commit()
    assert (await first_login(client, email, "My-own-passw0rd")).status_code == 200


async def test_accounts_hr_creates_by_hand_are_also_forced_and_expire(client, hr):
    staff, email, _ = await make_person(client, hr, account=False)
    r = await client.post(f"{API}/users", headers=hr, json={"email": email, "role": "staff", "staff_id": staff["id"]})
    assert r.status_code == 201 and r.json()["must_change_password"] is True and r.json()["password_expires_at"]
    h = bearer(await first_login(client, email, r.json()["temporary_password"]))
    assert (await client.get(f"{API}/staff/me", headers=h)).status_code == 403


# ------------------------------------------------------------------ cases where no account is created
async def test_no_email_means_staff_is_created_without_a_login_then_added_later(client, hr):
    r = await create_staff(client, hr)  # no email
    assert r.status_code == 201
    body = r.json()
    assert body["account"] is None and "No email" in body["account_note"]

    # HR adds the email later and creates the login.
    email = unique_email()
    assert (await client.patch(f"{API}/staff/{body['file_number']}", headers=hr, json={"email": email})).status_code == 200
    r = await client.post(f"{API}/staff/{body['file_number']}/account", headers=hr)
    assert r.status_code == 201 and r.json()["email"] == email and r.json()["must_change_password"] is True
    assert (await first_login(client, email)).status_code == 200
    # Creating it twice is refused.
    assert (await client.post(f"{API}/staff/{body['file_number']}/account", headers=hr)).status_code == 409


async def test_create_account_can_be_switched_off_per_person(client, hr):
    email = unique_email()
    r = await create_staff(client, hr, email=email, create_account=False)
    assert r.status_code == 201 and r.json()["account"] is None and "create_account" in r.json()["account_note"]
    assert await db_user(email) is None
    assert (await first_login(client, email)).status_code == 401


async def test_email_clash_rejects_the_whole_request_and_creates_nothing(client, hr):
    last = f"Clash{uuid.uuid4().hex[:6]}"
    r = await client.post(f"{API}/staff", headers=hr, json={
        "first_name": "Ada", "last_name": last, "email": "hod@hospital.test",  # already someone's login
        "department_id": await dept_id(client, hr, "Pharmacy")})
    assert r.status_code == 409 and "already used" in r.json()["detail"]
    found = (await client.get(f"{API}/staff", headers=hr, params={"q": last})).json()
    assert found["total"] == 0  # atomic: no orphaned staff record without its account


async def test_invalid_email_is_rejected(client, hr):
    for bad in ("not-an-email", "a@b", "two words@x.com"):
        assert (await create_staff(client, hr, email=bad)).status_code == 422


async def test_only_hr_can_create_staff_accounts(client, hr, hod, director, superadmin):
    staff, _, _ = await make_person(client, hr, account=False)
    _, _, ordinary_staff = await make_person(client, hr)
    for who in (ordinary_staff, hod, director, superadmin):
        assert (await client.post(f"{API}/staff/{staff['file_number']}/account", headers=who)).status_code == 403


async def test_account_creation_is_audited_without_the_password(client, hr):
    email = unique_email()
    body = (await create_staff(client, hr, email=email)).json()
    trail = (await client.get(f"{API}/staff/{body['file_number']}/audit", headers=hr)).json()["items"]
    created = next(a for a in trail if a["action"] == "user_created")
    assert created["by"] == "hr@hospital.test"
    assert created["after"]["email"] == email and created["after"]["automatic"] is True
    assert INITIAL not in str(trail) and "argon2" not in str(trail)


# ------------------------------------------------------------------ random mode and configuration
async def test_random_mode_gives_each_person_their_own_one_time_password(client, hr, monkeypatch):
    monkeypatch.setattr(settings, "initial_password_mode", "random")
    e1, e2 = unique_email(), unique_email()
    a = (await create_staff(client, hr, email=e1)).json()["account"]
    b = (await create_staff(client, hr, email=e2)).json()["account"]
    assert a["temporary_password"] and b["temporary_password"] and a["temporary_password"] != b["temporary_password"]
    assert (await first_login(client, e1, a["temporary_password"])).status_code == 200
    assert (await first_login(client, e1, INITIAL)).status_code == 401  # the shared password does not work in this mode


async def test_auto_creation_can_be_disabled_globally(client, hr, monkeypatch):
    monkeypatch.setattr(settings, "auto_create_accounts", False)
    r = await create_staff(client, hr, email=unique_email())
    assert r.status_code == 201 and r.json()["account"] is None


def test_initial_password_configuration_rules():
    base = dict(environment="production", secret_key="s" * 40, field_encryption_key="k", storage_backend="r2",
                r2_account_id="a", r2_access_key_id="b", r2_secret_access_key="c", r2_bucket="d")
    with pytest.raises(ValueError, match="INITIAL_PASSWORD must be set in production"):
        Settings(**base)  # shared mode with no explicit password
    assert Settings(**base, initial_password_mode="random").initial_password_mode == "random"
    assert Settings(**base, initial_password="A-deliberate-one").initial_password == "A-deliberate-one"
    with pytest.raises(ValueError, match="at least"):
        Settings(**base, initial_password="short")
    with pytest.raises(ValueError, match="'shared' or 'random'"):
        Settings(initial_password_mode="whatever")
    assert Settings().initial_password == "hospital123"  # development convenience only


# ------------------------------------------------------------------ nothing else regressed
async def test_seeded_demo_accounts_are_not_forced(client):
    h = await login(client, "staff@hospital.test")
    assert (await client.get(f"{API}/staff/me", headers=h)).status_code == 200


async def test_deactivated_account_cannot_sign_in_even_with_the_initial_password(client, hr):
    email = unique_email()
    await create_staff(client, hr, email=email)
    uid = (await client.get(f"{API}/users", headers=hr, params={"q": email})).json()["items"][0]["id"]
    await client.patch(f"{API}/users/{uid}", headers=hr, json={"is_active": False})
    assert (await first_login(client, email)).status_code == 401
