import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["ENVIRONMENT"] = "test"
os.environ["STORAGE_BACKEND"] = "local"
os.environ["LOCAL_STORAGE_DIR"] = tempfile.mkdtemp()
os.environ["SECRET_KEY"] = "test-secret-key-that-is-long-enough-for-hs256"
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{_tmp}/test.db"

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.seed import DEMO_PASSWORD, seed  # noqa: E402

API = "/api/v1"


@pytest_asyncio.fixture(scope="session")
async def client():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)  # clean slate even on a persistent test database
        await conn.run_sync(Base.metadata.create_all)
    async with SessionLocal() as s:
        await seed(s, extra_staff=20)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    await engine.dispose()


async def login(client, email, password=DEMO_PASSWORD):
    r = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


@pytest_asyncio.fixture(scope="session")
async def hr(client):
    return await login(client, "hr@hospital.test")


@pytest_asyncio.fixture(scope="session")
async def hod(client):
    return await login(client, "hod@hospital.test")


@pytest_asyncio.fixture(scope="session")
async def director(client):
    return await login(client, "director@hospital.test")


@pytest_asyncio.fixture(scope="session")
async def superadmin(client):
    return await login(client, "superadmin@hospital.test")


async def dept_id(client, hr, name):
    r = await client.get(f"{API}/departments", headers=hr)
    return next(d["id"] for d in r.json() if d["name"] == name)


async def make_person(client, hr_headers, dept="Internal Medicine", role="staff", first="Test", account=True):
    """Creates a staff record plus an account (HR flow). Returns (staff_json, email, headers)."""
    import uuid
    tag = uuid.uuid4().hex[:8]
    r = await client.post(f"{API}/staff", headers=hr_headers, json={
        "first_name": first, "last_name": f"Person{tag}", "department_id": await dept_id(client, hr_headers, dept)})
    assert r.status_code == 201, r.text
    staff = r.json()
    email = f"{tag}@hospital.test"
    if not account:
        return staff, email, None
    r = await client.post(f"{API}/users", headers=hr_headers,
                          json={"email": email, "role": role, "staff_id": staff["id"], "password": "S3cure-pass-1"})
    assert r.status_code == 201, r.text
    return staff, email, await login(client, email, "S3cure-pass-1")
