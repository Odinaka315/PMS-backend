from .conftest import API, login, make_person


async def test_login_returns_frontend_shaped_user(client):
    r = await client.post(f"{API}/auth/login", json={"email": "staff@hospital.test", "password": "hospital123"})
    assert r.status_code == 200
    u = r.json()["user"]
    assert u["role"] == "staff" and u["file_number"] == "YAB-MED-042" and u["grade"] == "CONHESS 7" and u["step"] == "Step 2"


async def test_wrong_password_and_unknown_email_look_identical(client):
    a = await client.post(f"{API}/auth/login", json={"email": "staff@hospital.test", "password": "nope"})
    b = await client.post(f"{API}/auth/login", json={"email": "ghost@hospital.test", "password": "nope"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()


async def test_requires_token(client):
    assert (await client.get(f"{API}/auth/me")).status_code == 401
    assert (await client.get(f"{API}/auth/me", headers={"Authorization": "Bearer junk"})).status_code == 401


async def test_lockout_after_repeated_failures(client, hr):
    _, email, _ = await make_person(client, hr)
    for _ in range(5):
        assert (await client.post(f"{API}/auth/login", json={"email": email, "password": "bad"})).status_code == 401
    # Even the right password is refused while locked.
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": "S3cure-pass-1"})
    assert r.status_code == 429


async def test_refresh_rotation_and_reuse_detection(client, hr):
    _, email, _ = await make_person(client, hr)
    first = (await client.post(f"{API}/auth/login", json={"email": email, "password": "S3cure-pass-1"})).json()
    second = (await client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})).json()
    assert second["refresh_token"] != first["refresh_token"]
    # Replaying the old token fails and burns the whole family.
    assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": first["refresh_token"]})).status_code == 401
    assert (await client.post(f"{API}/auth/refresh", json={"refresh_token": second["refresh_token"]})).status_code == 401


async def test_deactivated_user_loses_access(client, hr):
    staff, email, headers = await make_person(client, hr)
    uid = next(u["id"] for u in (await client.get(f"{API}/users", headers=hr, params={"q": email})).json()["items"])
    assert (await client.patch(f"{API}/users/{uid}", headers=hr, json={"is_active": False})).status_code == 200
    assert (await client.get(f"{API}/auth/me", headers=headers)).status_code == 401


async def test_change_password(client, hr):
    _, email, headers = await make_person(client, hr)
    bad = await client.post(f"{API}/auth/change-password", headers=headers,
                            json={"current_password": "wrong", "new_password": "An0ther-pass-9"})
    assert bad.status_code == 400
    ok = await client.post(f"{API}/auth/change-password", headers=headers,
                           json={"current_password": "S3cure-pass-1", "new_password": "An0ther-pass-9"})
    assert ok.status_code == 200 and ok.json()["access_token"] and ok.json()["refresh_token"]
    await login(client, email, "An0ther-pass-9")
