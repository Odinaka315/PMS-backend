"""Exercises R2Storage (boto3 / S3 API) against a local S3-compatible server. This proves our code path and the shape
of the signed links; it cannot prove Cloudflare's own behaviour, so do one real upload against the dev bucket too."""
import socket
from urllib.parse import parse_qs, urlparse

import boto3
import httpx
import pytest
import pytest_asyncio
from botocore.config import Config
from moto.server import ThreadedMotoServer

from app.core.config import Settings, settings
from app.core.storage import R2Storage, get_storage
from app.main import app

from .conftest import API, make_person
from .test_documents import PDF, init

BUCKET = "pms-documents-test"


@pytest.fixture(scope="module")
def s3_server():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = ThreadedMotoServer(ip_address="127.0.0.1", port=port, verbose=False)
    server.start()
    yield f"http://127.0.0.1:{port}"
    server.stop()


@pytest.fixture
def r2(s3_server):
    saved = {k: getattr(settings, k) for k in ("r2_endpoint_url", "r2_account_id", "r2_access_key_id",
                                              "r2_secret_access_key", "r2_bucket")}
    settings.r2_endpoint_url, settings.r2_account_id = s3_server, "acct123"
    settings.r2_access_key_id, settings.r2_secret_access_key, settings.r2_bucket = "AKIATEST", "secret-test", BUCKET
    # Test-only admin client (the local S3 server wants a normal region name to create a bucket).
    admin = boto3.client("s3", endpoint_url=s3_server, aws_access_key_id="x", aws_secret_access_key="y",
                         region_name="us-east-1", config=Config(s3={"addressing_style": "path"}))
    admin.create_bucket(Bucket=BUCKET)
    yield R2Storage(), admin
    for obj in admin.list_objects_v2(Bucket=BUCKET).get("Contents", []):
        admin.delete_object(Bucket=BUCKET, Key=obj["Key"])
    for k, v in saved.items():
        setattr(settings, k, v)


async def raw_put(url, data, headers):
    async with httpx.AsyncClient() as c:
        return await c.put(url, content=data, headers=headers)


async def test_upload_link_is_bound_to_type_size_and_expiry(r2):
    storage, _ = r2
    p = await storage.presign_upload("quarantine/a/b", "application/pdf", 1234, 300)
    q = parse_qs(urlparse(p.url).query)
    signed = q["X-Amz-SignedHeaders"][0].split(";")
    assert "content-length" in signed and "content-type" in signed  # both are part of the signature
    assert q["X-Amz-Expires"] == ["300"] and q["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert urlparse(p.url).path == f"/{BUCKET}/quarantine/a/b"  # path-style, key is random, no filename
    assert p.method == "PUT" and p.headers == {"Content-Type": "application/pdf"}


async def test_download_link_forces_attachment_and_no_cache(r2):
    storage, _ = r2
    url = await storage.presign_download("documents/a/b", "My Cert.pdf", "application/pdf", 60)
    q = parse_qs(urlparse(url).query)
    assert q["X-Amz-Expires"] == ["60"]
    assert q["response-content-disposition"][0].startswith('attachment; filename="My Cert.pdf"')
    assert q["response-content-type"] == ["application/pdf"] and q["response-cache-control"] == ["private, no-store"]


async def test_storage_roundtrip(r2):
    storage, _ = r2
    assert await storage.head("quarantine/x/1") is None
    p = await storage.presign_upload("quarantine/x/1", "application/pdf", len(PDF), 300)
    assert (await raw_put(p.url, PDF, p.headers)).status_code == 200
    info = await storage.head("quarantine/x/1")
    assert info.size == len(PDF) and info.content_type == "application/pdf"
    assert await storage.read_start("quarantine/x/1", 5) == b"%PDF-"
    await storage.copy("quarantine/x/1", "documents/x/1")
    assert (await storage.head("documents/x/1")).size == len(PDF)
    await storage.delete("quarantine/x/1")
    assert await storage.head("quarantine/x/1") is None
    async with httpx.AsyncClient() as c:
        got = await c.get(await storage.presign_download("documents/x/1", "c.pdf", "application/pdf", 60))
    assert got.status_code == 200 and got.content == PDF


@pytest_asyncio.fixture
async def r2_api(r2):
    app.dependency_overrides[get_storage] = lambda: r2[0]
    yield r2
    app.dependency_overrides.pop(get_storage, None)


def keys(admin):
    return sorted(o["Key"] for o in admin.list_objects_v2(Bucket=BUCKET).get("Contents", []))


async def test_api_flow_end_to_end_on_r2_backend(client, hr, r2_api):
    storage, admin = r2_api
    staff, _, h = await make_person(client, hr)
    body = (await init(client, h, staff["file_number"])).json()
    assert body["upload"]["url"].startswith(settings.r2_endpoint_url)  # the browser talks to the bucket directly
    assert (await raw_put(body["upload"]["url"], PDF, body["upload"]["headers"])).status_code == 200
    assert keys(admin) == [f"quarantine/{staff['id']}/{body['document']['id']}"]

    done = await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=h)
    assert done.status_code == 200 and done.json()["status"] == "Pending"
    # Moved out of quarantine into its final key.
    assert keys(admin) == [f"documents/{staff['id']}/{body['document']['id']}"]

    link = (await client.get(f"{API}/documents/{done.json()['id']}/download", headers=h)).json()
    async with httpx.AsyncClient() as c:
        assert (await c.get(link["url"])).content == PDF


async def test_api_rejects_bad_content_and_cleans_the_bucket(client, hr, r2_api):
    _, admin = r2_api
    staff, _, h = await make_person(client, hr)
    fake = b"MZ\x90\x00 not a pdf at all"
    body = (await init(client, h, staff["file_number"], data=fake)).json()
    assert (await raw_put(body["upload"]["url"], fake, body["upload"]["headers"])).status_code == 200
    done = await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=h)
    assert done.status_code == 422
    assert keys(admin) == []  # nothing left in quarantine or the final location

    # Uploaded bytes shorter/longer than declared are caught too (moto does not enforce the signed length).
    body = (await init(client, h, staff["file_number"], size=len(PDF) + 50)).json()
    assert (await raw_put(body["upload"]["url"], PDF, body["upload"]["headers"])).status_code == 200
    assert (await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=h)).status_code == 422
    assert keys(admin) == []


def test_settings_guards():
    with pytest.raises(ValueError, match="R2_BUCKET"):
        Settings(storage_backend="r2", r2_account_id="a", r2_access_key_id="b", r2_secret_access_key="c")
    with pytest.raises(ValueError, match="STORAGE_BACKEND must be 'r2'"):
        Settings(environment="production", secret_key="s" * 40, field_encryption_key="k", storage_backend="local")
    with pytest.raises(ValueError, match="local' or 'r2"):
        Settings(storage_backend="s3")
    ok = Settings(environment="production", secret_key="s" * 40, field_encryption_key="k", storage_backend="r2",
                  r2_account_id="a", r2_access_key_id="b", r2_secret_access_key="c", r2_bucket="d")
    assert ok.storage_backend == "r2"
