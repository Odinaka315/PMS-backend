"""Accounts created for staff: random one-time password returned to HR, forced change at first sign-in, expiry."""
import re
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.core.security import generate_temp_password, utcnow
from app.models import User

from .conftest import API, dept_id, login, make_person

READABLE = re.compile(r"^[a-hj-km-np-z2-9]{4}-[a-hj-km-np-z2-9]{4}-[a-hj-km-np-z2-9]{4}$")


def unique_email():
    return f"new.{uuid.uuid4().hex[:8]}@hospital.test"


async def create_staff(client, hr, **extra):
    body = {"first_name": "Ngozi", "last_name": f"Newhire{uuid.uuid4().hex[:6]}",
            "department_id": await dept_id(client, hr, "Pharmacy"), **extra}
    return await client.post(f"{API}/staff", headers=hr, json=body)


async def sign_in(client, email, password):
    return await client.post(f"{API}/auth/login", json={"email": email, "password": password})


def bearer(r):
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


async def new_hire(client, hr):
    """HR creates a staff member; returns (email, temporary password, response body)."""
    email = unique_email()
    r = await create_staff(client, hr, email=email)
    assert r.status_code == 201, r.text
    return email, r.json()["account"]["temporary_password"], r.json()


async def db_user(email):
    async with SessionLocal() as s:
        return await s.scalar(select(User).where(User.email == email))


# ------------------------------------------------------------------ the password itself
def test_generated_passwords_are_readable_unique_and_long_enough():
    samples = {generate_temp_password() for _ in range(2000)}
    assert len(samples) == 2000  # no repeats
    assert all(READABLE.match(p) for p in samples)  # lowercase + digits, no 0/o/1/l/i, groups of four
    assert all(len(p) == 14 for p in samples)


# ------------------------------------------------------------------ automatic creation, returned to HR
async def test_creating_staff_creates_a_login_and_returns_the_password_to_hr(client, hr):
    email = unique_email()
    r = await create_staff(client, hr, email=email)
    assert r.status_code == 201, r.text
    body = r.json()
    acct = body["account"]
    assert acct["email"] == email and acct["must_change_password"] is True and acct["initial_password_expires_at"]
    assert READABLE.match(acct["temporary_password"])
    assert body["account_note"] is None

    # A plain staff account linked to the new record; only the hash is stored.
    u = await db_user(email)
    assert u.role == "staff" and str(u.staff_id) == body["id"] and u.is_active
    assert u.password_hash.startswith("$argon2") and acct["temporary_password"] not in u.password_hash

    # The new person can sign in with the password HR was given.
    r = await sign_in(client, email, acct["temporary_password"])
    assert r.status_code == 200
    me = r.json()["user"]
    assert me["must_change_password"] is True and me["file_number"] == body["file_number"] and me["role"] == "staff"


async def test_every_person_gets_their_own_password(client, hr):
    (e1, p1, _), (e2, p2, _) = await new_hire(client, hr), await new_hire(client, hr)
    assert p1 != p2
    # One person's password does not open the other's account, and the old shared demo password opens neither.
    assert (await sign_in(client, e1, p2)).status_code == 401
    assert (await sign_in(client, e2, p1)).status_code == 401
    assert (await sign_in(client, e1, "hospital123")).status_code == 401


async def test_the_password_is_never_stored_logged_or_cached(client, hr):
    email, temp, body = await new_hire(client, hr)
    # Not in the audit trail.
    trail = (await client.get(f"{API}/staff/{body['file_number']}/audit", headers=hr)).json()["items"]
    assert temp not in str(trail) and "argon2" not in str(trail)
    # Browsers and proxies are told never to cache API responses (this one carries a password).
    r = await create_staff(client, hr, email=unique_email())
    assert r.headers["cache-control"] == "no-store" and r.headers["pragma"] == "no-cache"
    assert (await client.get(f"{API}/staff", headers=hr)).headers["cache-control"] == "no-store"
    # It cannot be fetched again: no endpoint returns it after creation.
    for path in (f"/staff/{body['file_number']}", f"/users?q={email}"):
        assert temp not in (await client.get(f"{API}{path}", headers=hr)).text


async def test_login_name_is_normalised_to_lowercase(client, hr):
    mixed = f"Mixed.{uuid.uuid4().hex[:6]}@Hospital.Test"
    r = await create_staff(client, hr, email=mixed)
    assert r.json()["account"]["email"] == mixed.lower()
    assert (await sign_in(client, mixed.upper(), r.json()["account"]["temporary_password"])).status_code == 200


async def test_hr_cannot_choose_a_password(client, hr):
    staff, email, _ = await make_person(client, hr, account=False)
    r = await client.post(f"{API}/users", headers=hr, json={
        "email": email, "role": "staff", "staff_id": staff["id"], "password": "Chosen-by-HR-1"})
    assert r.status_code == 201
    assert READABLE.match(r.json()["temporary_password"])  # the supplied field is ignored
    assert (await sign_in(client, email, "Chosen-by-HR-1")).status_code == 401
    assert (await sign_in(client, email, r.json()["temporary_password"])).status_code == 200


# ------------------------------------------------------------------ forced first-login change
async def test_new_account_is_locked_to_the_password_change_until_done(client, hr):
    email, temp, _ = await new_hire(client, hr)
    h = bearer(await sign_in(client, email, temp))

    for path in ("/staff/me", "/leave/types", "/leave/balance", "/departments", "/reports/dashboard"):
        r = await client.get(f"{API}{path}", headers=h)
        assert r.status_code == 403, path
        assert r.json()["detail"]["code"] == "password_change_required"
    assert (await client.get(f"{API}/auth/me", headers=h)).status_code == 200

    changed = await client.post(f"{API}/auth/change-password", headers=h,
                                json={"current_password": temp, "new_password": "My-own-passw0rd"})
    assert changed.status_code == 200 and changed.json()["user"]["must_change_password"] is False
    assert (await client.get(f"{API}/staff/me", headers=bearer(changed))).status_code == 200

    # The one-time password is now dead; their own works.
    assert (await sign_in(client, email, temp)).status_code == 401
    assert (await sign_in(client, email, "My-own-passw0rd")).status_code == 200


async def test_the_old_session_cannot_be_refreshed_after_the_change(client, hr):
    email, temp, _ = await new_hire(client, hr)
    first = (await sign_in(client, email, temp)).json()
    await client.post(f"{API}/auth/change-password", headers={"Authorization": f"Bearer {first['access_token']}"},
                      json={"current_password": temp, "new_password": "My-own-passw0rd"})
    assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})).status_code == 401


@pytest.mark.parametrize("new", ["short", "password", "hospital123", "HOSPITAL123"])
async def test_new_password_rules(client, hr, new):
    email, temp, _ = await new_hire(client, hr)
    h = bearer(await sign_in(client, email, temp))
    r = await client.post(f"{API}/auth/change-password", headers=h, json={"current_password": temp, "new_password": new})
    assert r.status_code == 422
    assert (await client.get(f"{API}/staff/me", headers=h)).status_code == 403  # still locked to the change


async def test_the_one_time_password_cannot_be_reused_as_the_new_one(client, hr):
    email, temp, _ = await new_hire(client, hr)
    h = bearer(await sign_in(client, email, temp))
    r = await client.post(f"{API}/auth/change-password", headers=h, json={"current_password": temp, "new_password": temp})
    assert r.status_code == 422 and "different" in r.json()["detail"]


async def test_wrong_current_password_is_refused(client, hr):
    email, temp, _ = await new_hire(client, hr)
    h = bearer(await sign_in(client, email, temp))
    assert (await client.post(f"{API}/auth/change-password", headers=h,
                              json={"current_password": "nope-nope", "new_password": "My-own-passw0rd"})).status_code == 400


# ------------------------------------------------------------------ expiry, reissue, chasing
async def test_an_unused_password_expires_and_hr_can_reissue(client, hr):
    email, temp, _ = await new_hire(client, hr)
    async with SessionLocal() as s:  # pretend 15 days have passed
        u = await s.scalar(select(User).where(User.email == email))
        u.password_expires_at = utcnow() - timedelta(days=1)
        await s.commit()
    r = await sign_in(client, email, temp)
    assert r.status_code == 401 and "expired" in r.json()["detail"]
    # A wrong password still gets the generic answer, so expiry is not revealed to someone guessing.
    assert (await sign_in(client, email, "wrong-wrong")).json()["detail"] == "Incorrect email or password"

    uid = (await client.get(f"{API}/users", headers=hr, params={"q": email})).json()["items"][0]["id"]
    again = (await client.post(f"{API}/users/{uid}/reset-password", headers=hr)).json()["temporary_password"]
    assert READABLE.match(again) and again != temp
    assert (await sign_in(client, email, temp)).status_code == 401  # the lost one is dead
    assert (await sign_in(client, email, again)).status_code == 200


async def test_hr_can_list_accounts_still_awaiting_first_login(client, hr):
    pending_email, temp, _ = await new_hire(client, hr)
    done_email, temp2, _ = await new_hire(client, hr)
    h = bearer(await sign_in(client, done_email, temp2))
    await client.post(f"{API}/auth/change-password", headers=h, json={"current_password": temp2, "new_password": "My-own-passw0rd"})

    waiting = (await client.get(f"{API}/users", headers=hr, params={"awaiting_first_login": True, "size": 100})).json()
    emails = {u["email"] for u in waiting["items"]}
    assert pending_email in emails and done_email not in emails
    row = next(u for u in waiting["items"] if u["email"] == pending_email)
    assert row["must_change_password"] is True and row["password_expires_at"]
    assert "temporary_password" not in str(waiting)  # the list never exposes passwords


async def test_accounts_hr_creates_by_hand_are_also_forced_and_expire(client, hr):
    staff, email, _ = await make_person(client, hr, account=False)
    r = await client.post(f"{API}/users", headers=hr, json={"email": email, "role": "staff", "staff_id": staff["id"]})
    assert r.status_code == 201 and r.json()["must_change_password"] is True and r.json()["password_expires_at"]
    h = bearer(await sign_in(client, email, r.json()["temporary_password"]))
    assert (await client.get(f"{API}/staff/me", headers=h)).status_code == 403


# ------------------------------------------------------------------ cases where no account is created
async def test_no_email_means_staff_is_created_without_a_login_then_added_later(client, hr):
    r = await create_staff(client, hr)  # no email
    assert r.status_code == 201
    body = r.json()
    assert body["account"] is None and "No email" in body["account_note"]

    email = unique_email()
    assert (await client.patch(f"{API}/staff/{body['file_number']}", headers=hr, json={"email": email})).status_code == 200
    r = await client.post(f"{API}/staff/{body['file_number']}/account", headers=hr)
    assert r.status_code == 201 and r.json()["email"] == email and READABLE.match(r.json()["temporary_password"])
    assert r.headers["cache-control"] == "no-store"
    assert (await sign_in(client, email, r.json()["temporary_password"])).status_code == 200
    assert (await client.post(f"{API}/staff/{body['file_number']}/account", headers=hr)).status_code == 409


async def test_create_account_can_be_switched_off_per_person(client, hr):
    email = unique_email()
    r = await create_staff(client, hr, email=email, create_account=False)
    assert r.status_code == 201 and r.json()["account"] is None and "create_account" in r.json()["account_note"]
    assert await db_user(email) is None


async def test_email_clash_rejects_the_whole_request_and_creates_nothing(client, hr):
    last = f"Clash{uuid.uuid4().hex[:6]}"
    r = await client.post(f"{API}/staff", headers=hr, json={
        "first_name": "Ada", "last_name": last, "email": "hod@hospital.test",
        "department_id": await dept_id(client, hr, "Pharmacy")})
    assert r.status_code == 409 and "already used" in r.json()["detail"]
    assert (await client.get(f"{API}/staff", headers=hr, params={"q": last})).json()["total"] == 0


async def test_invalid_email_is_rejected(client, hr):
    for bad in ("not-an-email", "a@b", "two words@x.com"):
        assert (await create_staff(client, hr, email=bad)).status_code == 422


async def test_only_hr_can_create_staff_accounts(client, hr, hod, director, superadmin):
    staff, _, _ = await make_person(client, hr, account=False)
    _, _, ordinary_staff = await make_person(client, hr)
    for who in (ordinary_staff, hod, director, superadmin):
        assert (await client.post(f"{API}/staff/{staff['file_number']}/account", headers=who)).status_code == 403


async def test_account_creation_is_audited(client, hr):
    email, temp, body = await new_hire(client, hr)
    trail = (await client.get(f"{API}/staff/{body['file_number']}/audit", headers=hr)).json()["items"]
    created = next(a for a in trail if a["action"] == "user_created")
    assert created["by"] == "hr@hospital.test" and created["after"] == {"email": email, "role": "staff", "automatic": True}


async def test_auto_creation_can_be_disabled_globally(client, hr, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "auto_create_accounts", False)
    r = await create_staff(client, hr, email=unique_email())
    assert r.status_code == 201 and r.json()["account"] is None


# ------------------------------------------------------------------ nothing else regressed
async def test_seeded_demo_accounts_are_not_forced(client):
    h = await login(client, "staff@hospital.test")  # dev-only demo logins keep their demo password
    assert (await client.get(f"{API}/staff/me", headers=h)).status_code == 200


async def test_deactivated_account_cannot_sign_in_even_with_the_one_time_password(client, hr):
    email, temp, _ = await new_hire(client, hr)
    uid = (await client.get(f"{API}/users", headers=hr, params={"q": email})).json()["items"][0]["id"]
    await client.patch(f"{API}/users/{uid}", headers=hr, json={"is_active": False})
    assert (await sign_in(client, email, temp)).status_code == 401
