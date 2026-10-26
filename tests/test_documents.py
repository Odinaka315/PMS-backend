import uuid
from datetime import date, timedelta

import pytest

from .conftest import API, login, make_person

PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


async def init(client, h, ident, *, filename="cert.pdf", content_type="application/pdf", size=None, data=PDF,
               doc_type="Certificate", **extra):
    return await client.post(f"{API}/staff/{ident}/documents", headers=h, json={
        "doc_type": doc_type, "filename": filename, "content_type": content_type,
        "size": len(data) if size is None else size, **extra})


async def put(client, target, data, headers=None):
    return await client.put(target["url"], content=data, headers=headers or target["headers"])


async def upload(client, h, ident, data=PDF, **kw):
    """Runs the whole 3-step flow and returns the completed document JSON."""
    r = await init(client, h, ident, data=data, **kw)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (await put(client, body["upload"], data)).status_code == 200
    done = await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=h)
    assert done.status_code == 200, done.text
    return done.json()


# ------------------------------------------------------------------ happy path
async def test_full_upload_download_flow(client, hr):
    staff, _, h = await make_person(client, hr)
    r = await init(client, h, staff["file_number"], expiry_date="2027-01-01")
    assert r.status_code == 201
    body = r.json()
    assert body["document"]["status"] == "Awaiting Upload" and body["upload"]["method"] == "PUT"
    assert body["upload"]["headers"] == {"Content-Type": "application/pdf"} and body["upload"]["expires_in"] == 300
    # The storage key never reaches the client.
    assert "quarantine" not in str(body) and "object_key" not in str(body)

    # Not visible in lists until it has been uploaded and checked.
    assert (await client.get(f"{API}/staff/{staff['file_number']}/documents", headers=h)).json() == []

    assert (await put(client, body["upload"], PDF)).status_code == 200
    done = (await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=h)).json()
    assert done["status"] == "Pending" and done["size_bytes"] == len(PDF) and done["expiry"] == "2027-01-01"
    assert done["can_download"] and done["can_delete"] and not done["can_verify"]

    link = (await client.get(f"{API}/documents/{done['id']}/download", headers=h)).json()
    assert link["expires_in"] == 60 and link["filename"] == "cert.pdf"
    got = await client.get(link["url"])
    assert got.status_code == 200 and got.content == PDF
    assert got.headers["content-disposition"].startswith("attachment;")
    assert got.headers["x-content-type-options"] == "nosniff" and got.headers["content-type"] == "application/pdf"

    # Completing twice is refused.
    assert (await client.post(f"{API}/documents/{done['id']}/complete", headers=h)).status_code == 409
    docs = (await client.get(f"{API}/staff/{staff['file_number']}/documents", headers=h)).json()
    assert [d["id"] for d in docs] == [done["id"]]


async def test_png_and_jpeg_accepted(client, hr):
    staff, _, h = await make_person(client, hr)
    assert (await upload(client, h, staff["file_number"], PNG, filename="photo.png", content_type="image/png"))["status"] == "Pending"
    assert (await upload(client, h, staff["file_number"], JPG, filename="id.JPEG", content_type="image/jpeg"))["status"] == "Pending"


async def test_hr_can_upload_for_staff_and_it_is_audited(client, hr):
    staff, _, h = await make_person(client, hr)
    doc = await upload(client, hr, staff["file_number"], doc_type="Appointment", filename="letter.pdf")
    assert doc["status"] == "Pending"
    link = (await client.get(f"{API}/documents/{doc['id']}/download", headers=h)).json()  # the owner can fetch it
    assert (await client.get(link["url"])).content == PDF
    trail = (await client.get(f"{API}/staff/{staff['file_number']}/audit", headers=hr)).json()["items"]
    actions = [a["action"] for a in trail]
    assert "document_uploaded" in actions and "document_downloaded" in actions
    assert "quarantine" not in str(trail) and "documents/" not in str(trail)  # storage keys are never logged


# ------------------------------------------------------------------ validation
@pytest.mark.parametrize("filename,ctype,size,fragment", [
    ("virus.exe", "application/x-msdownload", 100, "Only PDF"),
    ("page.html", "text/html", 100, "Only PDF"),
    ("logo.svg", "image/svg+xml", 100, "Only PDF"),
    ("macro.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", 100, "Only PDF"),
    ("doc.png", "application/pdf", 100, "extension"),   # extension does not match the declared type
    ("empty.pdf", "application/pdf", 0, "empty"),
    ("huge.pdf", "application/pdf", 10 * 1024 * 1024 + 1, "too large"),
])
async def test_declared_upload_is_validated(client, hr, filename, ctype, size, fragment):
    staff, _, h = await make_person(client, hr)
    r = await init(client, h, staff["file_number"], filename=filename, content_type=ctype, size=size)
    assert r.status_code == 422 and fragment in r.json()["detail"]


async def test_contents_must_match_declared_type(client, hr):
    staff, _, h = await make_person(client, hr)
    fake = b"MZ\x90\x00 this is really an executable"
    r = await init(client, h, staff["file_number"], data=fake)  # claims to be a PDF
    body = r.json()
    assert (await put(client, body["upload"], fake)).status_code == 200
    done = await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=h)
    assert done.status_code == 422 and "do not match" in done.json()["detail"]
    # Rejected files never show up, cannot be downloaded, and leave nothing in storage.
    assert (await client.get(f"{API}/staff/{staff['file_number']}/documents", headers=h)).json() == []
    assert (await client.get(f"{API}/documents/{body['document']['id']}/download", headers=h)).status_code == 404
    from app.core.storage import get_storage
    store = get_storage()
    assert not store.path(f"quarantine/{staff['id']}/{body['document']['id']}").exists()
    assert not store.path(f"documents/{staff['id']}/{body['document']['id']}").exists()
    trail = (await client.get(f"{API}/staff/{staff['file_number']}/audit", headers=h)).json()["items"]
    assert "document_upload_rejected" in [a["action"] for a in trail]


async def test_upload_size_must_match_declaration(client, hr):
    staff, _, h = await make_person(client, hr)
    r = await init(client, h, staff["file_number"], size=len(PDF))
    body = r.json()
    # Local storage enforces the signed length like R2 does.
    assert (await put(client, body["upload"], PDF + b"extra")).status_code == 403
    assert (await put(client, body["upload"], PDF, headers={"Content-Type": "image/png"})).status_code == 403


async def test_complete_before_upload_and_others_cannot_complete(client, hr):
    staff, _, h = await make_person(client, hr)
    _, _, other = await make_person(client, hr)
    body = (await init(client, h, staff["file_number"])).json()
    assert (await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=h)).status_code == 409
    assert (await client.post(f"{API}/documents/{body['document']['id']}/complete", headers=other)).status_code == 404


async def test_signed_links_are_unforgeable_and_expire(client, hr):
    staff, _, h = await make_person(client, hr)
    body = (await init(client, h, staff["file_number"])).json()
    url = body["upload"]["url"]
    tampered = url[:-4] + ("AAAA" if not url.endswith("AAAA") else "BBBB")
    assert (await client.put(tampered, content=PDF, headers=body["upload"]["headers"])).status_code == 403
    assert (await client.put(f"{API}/dev-storage/not-a-token", content=PDF)).status_code == 403
    # An upload link cannot be used to read, and an access token is not a storage token.
    assert (await client.get(url)).status_code == 403
    import time
    import jwt
    from app.core.config import settings
    expired = jwt.encode({"type": "storage", "op": "put", "k": "x", "ct": "application/pdf", "sz": 1,
                          "exp": int(time.time()) - 5}, settings.secret_key, algorithm="HS256")
    assert (await client.put(f"{API}/dev-storage/{expired}", content=b"x", headers={"Content-Type": "application/pdf"})).status_code == 403


async def test_filename_is_sanitised_and_not_used_in_keys(client, hr):
    staff, _, h = await make_person(client, hr)
    doc = await upload(client, h, staff["file_number"], filename="../../etc/passwd\r\nX-Evil: 1.pdf")
    assert doc["original_filename"] == "passwdX-Evil: 1.pdf"  # path stripped, control characters removed
    link = (await client.get(f"{API}/documents/{doc['id']}/download", headers=h)).json()
    got = await client.get(link["url"])
    assert "\n" not in got.headers["content-disposition"] and "X-Evil" not in got.headers


# ------------------------------------------------------------------ who can see / download
async def test_scope_and_download_rules(client, hr, hod, director, superadmin):
    # Same department as the seeded HOD (Internal Medicine) so the HOD is in scope.
    staff, _, owner = await make_person(client, hr, dept="Internal Medicine")
    _, _, colleague = await make_person(client, hr, dept="Internal Medicine")
    doc = await upload(client, owner, staff["file_number"])
    did = doc["id"]

    # Another staff member cannot even see that it exists.
    assert (await client.get(f"{API}/documents/{did}", headers=colleague)).status_code == 404
    assert (await client.get(f"{API}/staff/{staff['file_number']}/documents", headers=colleague)).status_code == 404

    # HOD, director and super admin see details but cannot download the file.
    for who in (hod, director, superadmin):
        d = await client.get(f"{API}/documents/{did}", headers=who)
        assert d.status_code == 200 and d.json()["can_download"] is False
        assert (await client.get(f"{API}/documents/{did}/download", headers=who)).status_code == 403
        assert len((await client.get(f"{API}/staff/{staff['file_number']}/documents", headers=who)).json()) == 1

    # HOD of another department sees nothing.
    other_dept, _, _ = await make_person(client, hr, dept="Surgery")
    sdoc = await upload(client, hr, other_dept["file_number"])
    assert (await client.get(f"{API}/documents/{sdoc['id']}", headers=hod)).status_code == 404

    # Owner and HR can download.
    for who in (owner, hr):
        assert (await client.get(f"{API}/documents/{did}/download", headers=who)).status_code == 200


async def test_staff_cannot_upload_to_someone_elses_record(client, hr):
    a, _, ha = await make_person(client, hr)
    b, _, _ = await make_person(client, hr)
    r = await init(client, ha, b["file_number"])
    assert r.status_code == 403


async def test_unauthenticated_access_refused(client):
    assert (await client.get(f"{API}/documents/{uuid.uuid4()}")).status_code == 401
    assert (await client.get(f"{API}/documents/{uuid.uuid4()}/download")).status_code == 401


# ------------------------------------------------------------------ verification and deletion
async def test_hr_verifies_and_rejects(client, hr):
    staff, _, owner = await make_person(client, hr)
    d1 = await upload(client, owner, staff["file_number"], doc_type="License", filename="lic.pdf")
    d2 = await upload(client, owner, staff["file_number"], doc_type="CV", filename="cv.pdf")

    assert (await client.post(f"{API}/documents/{d1['id']}/verify", headers=owner, json={"decision": "verify"})).status_code == 403
    r = await client.post(f"{API}/documents/{d1['id']}/verify", headers=hr, json={"decision": "verify"})
    assert r.status_code == 200 and r.json()["status"] == "Verified" and r.json()["verified_by"] == "hr@hospital.test"

    assert (await client.post(f"{API}/documents/{d2['id']}/verify", headers=hr, json={"decision": "reject"})).status_code == 422
    r = await client.post(f"{API}/documents/{d2['id']}/verify", headers=hr, json={"decision": "reject", "reason": "Unreadable"})
    assert r.json()["status"] == "Rejected" and r.json()["rejection_reason"] == "Unreadable"
    assert (await client.post(f"{API}/documents/{d1['id']}/verify", headers=hr, json={"decision": "reject", "reason": "x"})).status_code == 409


async def test_hr_cannot_verify_own_or_self_uploaded_documents(client, hr, superadmin):
    # HR's own staff record (HR-001): they uploaded it, so a different person must verify it.
    mine = await upload(client, hr, "HR-001", filename="mine.pdf")
    assert mine["can_verify"] is False  # the UI is told not to offer the button either
    r = await client.post(f"{API}/documents/{mine['id']}/verify", headers=hr, json={"decision": "verify"})
    assert r.status_code == 403
    # And HR can't rubber-stamp a document they uploaded on someone else's behalf.
    staff, _, _ = await make_person(client, hr)
    theirs = await upload(client, hr, staff["file_number"], filename="uploaded-by-hr.pdf")
    assert (await client.post(f"{API}/documents/{theirs['id']}/verify", headers=hr, json={"decision": "verify"})).status_code == 403


async def test_delete_rules(client, hr):
    staff, _, owner = await make_person(client, hr)
    pending = await upload(client, owner, staff["file_number"])
    verified = await upload(client, owner, staff["file_number"], filename="v.pdf")
    await client.post(f"{API}/documents/{verified['id']}/verify", headers=hr, json={"decision": "verify"})

    # The owner may withdraw a pending document but not a verified one; HR may delete either.
    assert (await client.delete(f"{API}/documents/{verified['id']}", headers=owner)).status_code == 403
    assert (await client.delete(f"{API}/documents/{pending['id']}", headers=owner)).status_code == 204
    assert (await client.get(f"{API}/documents/{pending['id']}", headers=owner)).status_code == 404
    assert (await client.delete(f"{API}/documents/{verified['id']}", headers=hr)).status_code == 204
    assert (await client.get(f"{API}/staff/{staff['file_number']}/documents", headers=owner)).json() == []

    # Soft delete: the stored file is retained and the deletion is on the audit trail.
    from app.core.storage import get_storage
    assert get_storage().path(f"documents/{staff['id']}/{verified['id']}").exists()
    trail = (await client.get(f"{API}/staff/{staff['file_number']}/audit", headers=hr)).json()["items"]
    assert [a["action"] for a in trail].count("document_deleted") == 2


async def test_expiring_documents_report(client, hr, director, hod):
    staff, _, h = await make_person(client, hr)
    soon = (date.today() + timedelta(days=20)).isoformat()
    later = (date.today() + timedelta(days=400)).isoformat()
    await upload(client, h, staff["file_number"], filename="soon.pdf", doc_type="License", expiry_date=soon)
    await upload(client, h, staff["file_number"], filename="later.pdf", doc_type="License", expiry_date=later)
    rows = (await client.get(f"{API}/documents/expiring", headers=hr, params={"days": 60})).json()
    mine = [r for r in rows if r["staff_id"] == staff["id"]]
    assert len(mine) == 1 and mine[0]["days_left"] == 20 and mine[0]["file_number"] == staff["file_number"]
    assert (await client.get(f"{API}/documents/expiring", headers=director)).status_code == 200
    assert (await client.get(f"{API}/documents/expiring", headers=hod)).status_code == 403
    assert (await client.get(f"{API}/documents/expiring", headers=h)).status_code == 403
    # Seeded demo data: the mock First Aid certificate expiring 2026-12-01 shows up in a wide window.
    wide = (await client.get(f"{API}/documents/expiring", headers=hr, params={"days": 730})).json()
    assert any(r["name"] == "First Aid Certificate" for r in wide)


async def test_seeded_demo_documents(client):
    staff = await login(client, "staff@hospital.test")
    docs = (await client.get(f"{API}/staff/YAB-MED-042/documents", headers=staff)).json()
    assert len(docs) == 8
    assert {d["status"] for d in docs} == {"Verified", "Pending", "Rejected"}
    cv = next(d for d in docs if d["name"] == "CV - 2025")
    assert cv["status"] == "Rejected" and cv["rejection_reason"]
    link = (await client.get(f"{API}/documents/{docs[0]['id']}/download", headers=staff)).json()
    assert (await client.get(link["url"])).content.startswith(b"%PDF-")
    filtered = (await client.get(f"{API}/staff/YAB-MED-042/documents", headers=staff, params={"status": "Pending"})).json()
    assert [d["name"] for d in filtered] == ["First Aid Certificate"]


async def test_too_many_open_uploads_is_limited(client, hr):
    staff, _, h = await make_person(client, hr)
    for _ in range(10):
        assert (await init(client, h, staff["file_number"])).status_code == 201
    assert (await init(client, h, staff["file_number"])).status_code == 429
