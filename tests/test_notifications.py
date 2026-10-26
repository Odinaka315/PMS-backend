"""Notifications: who is told what, privacy between users, and that they follow the real events."""
import uuid
from datetime import date, timedelta

from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Notification, User
from app.modules.notifications.service import notify

from .conftest import API, login, make_person
from .test_documents import upload


async def inbox(client, h, **params):
    r = await client.get(f"{API}/notifications", headers=h, params={"size": 100, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def titles(client, h, **params):
    return [n["title"] for n in (await inbox(client, h, **params))["items"]]


async def unread(client, h):
    return (await client.get(f"{API}/notifications/unread-count", headers=h)).json()["unread"]


async def leave_request(client, h, ahead, length=2):
    types = {t["code"]: t["id"] for t in (await client.get(f"{API}/leave/types", headers=h)).json()}
    start = date.today() + timedelta(days=ahead)
    return await client.post(f"{API}/leave/requests", headers=h, json={
        "leave_type_id": types["annual"], "reason": "Rest", "start_date": start.isoformat(),
        "end_date": (start + timedelta(days=length - 1)).isoformat()})


async def decide(client, h, rid, decision="approve"):
    r = await client.post(f"{API}/leave/requests/{rid}/decision", headers=h, json={"decision": decision, "comment": "ok"})
    assert r.status_code == 200, r.text


# ------------------------------------------------------------------ leave events
async def test_leave_notifications_follow_the_approval_chain(client, hr, hod, director):
    _, _, h = await make_person(client, hr)  # Internal Medicine, so the demo HOD heads this department
    hod0, hr0, dir0 = await unread(client, hod), await unread(client, hr), await unread(client, director)

    req = (await leave_request(client, h, ahead=100)).json()
    assert await unread(client, hod) == hod0 + 1 and await unread(client, hr) == hr0 and await unread(client, director) == dir0
    n = (await inbox(client, hod))["items"][0]
    assert n["title"] == "Leave awaiting your approval" and n["type"] == "leave" and n["link"] == "/leave" and not n["read"]
    assert await unread(client, h) == 0  # the requester is not told about their own request

    await decide(client, hod, req["id"])
    assert await unread(client, hr) == hr0 + 1 and await unread(client, director) == dir0
    await decide(client, hr, req["id"])
    assert await unread(client, director) == dir0 + 1
    assert await unread(client, h) == 0   # still in progress: no noise for the requester
    await decide(client, director, req["id"])
    mine = await inbox(client, h)
    assert [x["title"] for x in mine["items"]] == ["Leave request approved"] and mine["unread"] == 1


async def test_a_declined_request_tells_the_requester_and_nobody_further_up(client, hr, hod, director):
    _, _, h = await make_person(client, hr)
    hr0 = await unread(client, hr)
    req = (await leave_request(client, h, ahead=130)).json()
    await decide(client, hod, req["id"], "decline")
    assert await titles(client, h) == ["Leave request declined"]
    assert await unread(client, hr) == hr0


async def test_a_rejected_request_creates_no_notification(client, hr, hod):
    _, _, h = await make_person(client, hr)
    before = await unread(client, hod)
    assert (await leave_request(client, h, ahead=150, length=40)).status_code == 422  # more than the entitlement
    assert await unread(client, hod) == before


async def test_an_hod_requesting_leave_notifies_hr_not_themselves(client, hr, hod):
    hod0, hr0 = await unread(client, hod), await unread(client, hr)
    r = await leave_request(client, hod, ahead=170)
    assert r.status_code == 201
    assert await unread(client, hr) == hr0 + 1 and await unread(client, hod) == hod0
    await client.post(f"{API}/leave/requests/{r.json()['id']}/cancel", headers=hod)


# ------------------------------------------------------------------ document events
async def test_document_events_notify_hr_then_the_owner(client, hr):
    staff, _, h = await make_person(client, hr)
    hr0 = await unread(client, hr)
    doc = await upload(client, h, staff["file_number"], doc_type="CV")
    assert await unread(client, hr) == hr0 + 1
    first = (await inbox(client, hr))["items"][0]
    assert first["title"] == "Document awaiting verification" and first["link"] == "/documents"
    assert staff["file_number"] and "Person" in first["text"]

    assert (await client.post(f"{API}/documents/{doc['id']}/verify", headers=hr, json={"decision": "reject"})).status_code == 422
    assert await titles(client, h) == []  # a refused action tells nobody anything
    await client.post(f"{API}/documents/{doc['id']}/verify", headers=hr, json={"decision": "reject", "reason": "Blurry scan"})
    mine = (await inbox(client, h))["items"]
    assert [n["title"] for n in mine] == ["Document rejected"] and "Blurry scan" in mine[0]["text"]

    doc2 = await upload(client, h, staff["file_number"], doc_type="Certificate")
    await client.post(f"{API}/documents/{doc2['id']}/verify", headers=hr, json={"decision": "verify"})
    assert (await titles(client, h))[0] == "Document verified"


async def test_hr_uploading_for_someone_does_not_notify_hr_about_their_own_upload(client, hr):
    staff, _, h = await make_person(client, hr)
    hr0 = await unread(client, hr)
    await upload(client, hr, staff["file_number"], doc_type="CV")
    assert await unread(client, hr) == hr0


# ------------------------------------------------------------------ privacy and housekeeping
async def test_everyone_sees_only_their_own_notifications(client, hr, hod):
    _, _, a = await make_person(client, hr)
    _, _, b = await make_person(client, hr)
    async with SessionLocal() as s:
        uid = await s.scalar(select(User.id).where(User.email == "hod@hospital.test"))
    ours = (await inbox(client, hod))["items"]
    assert ours and not (await inbox(client, a))["items"] and not (await inbox(client, b))["items"]
    nid = ours[0]["id"]
    for who in (a, b, hr):  # someone else's looks exactly like a missing one
        assert (await client.post(f"{API}/notifications/{nid}/read", headers=who)).status_code == 404
        assert (await client.delete(f"{API}/notifications/{nid}", headers=who)).status_code == 404
    assert (await client.post(f"{API}/notifications/{uuid.uuid4()}/read", headers=a)).status_code == 404
    assert uid  # (the HOD still has it, untouched)
    assert nid in [n["id"] for n in (await inbox(client, hod))["items"]]


async def test_mark_read_read_all_filter_and_dismiss(client, hr):
    staff, _, h = await make_person(client, hr)
    async with SessionLocal() as s:
        uid = await s.scalar(select(User.id).where(User.staff_id == uuid.UUID(staff["id"])))
        for i in range(5):
            await notify(s, [uid], type="leave" if i < 3 else "document", title=f"T{i}", text="x", link="/x")
        await s.commit()
    assert await unread(client, h) == 5
    page = await inbox(client, h, size=2)
    assert len(page["items"]) == 2 and page["total"] == 5 and page["unread"] == 5
    assert page["items"][0]["title"] == "T4" or page["items"][0]["created_at"] >= page["items"][1]["created_at"]
    assert (await inbox(client, h, type="document"))["total"] == 2

    first = page["items"][0]["id"]
    r = await client.post(f"{API}/notifications/{first}/read", headers=h)
    assert r.status_code == 200 and r.json()["read"] is True
    assert (await client.post(f"{API}/notifications/{first}/read", headers=h)).status_code == 200  # idempotent
    assert await unread(client, h) == 4 and (await inbox(client, h, unread_only=True))["total"] == 4

    assert (await client.delete(f"{API}/notifications/{first}", headers=h)).status_code == 204
    assert (await inbox(client, h))["total"] == 4

    assert (await client.post(f"{API}/notifications/read-all", headers=h)).json() == {"marked": 4}
    assert await unread(client, h) == 0 and (await client.post(f"{API}/notifications/read-all", headers=h)).json() == {"marked": 0}
    assert (await inbox(client, h))["total"] == 4  # read, not removed


async def test_read_all_only_touches_your_own(client, hr, hod):
    before = await unread(client, hod)
    assert before > 0
    _, _, h = await make_person(client, hr)
    await client.post(f"{API}/notifications/read-all", headers=h)
    assert await unread(client, hod) == before


async def test_notifications_need_a_signed_in_user(client):
    for method, path in (("get", ""), ("get", "/unread-count"), ("post", "/read-all"), ("post", "/sweep")):
        assert (await getattr(client, method)(f"{API}/notifications{path}")).status_code == 401


async def test_the_demo_accounts_start_with_a_few_notifications(client):
    staff = await login(client, "staff@hospital.test")
    got = await inbox(client, staff)
    assert got["total"] >= 2 and any(n["title"] == "Payslip available" for n in got["items"])


# ------------------------------------------------------------------ the helper itself
async def test_notify_skips_the_actor_and_repeats_of_the_same_event():
    async with SessionLocal() as s:
        ids = list((await s.scalars(select(User.id).where(User.email.in_(["staff@hospital.test", "hr@hospital.test"])))).all())
        actor, other = ids
        key = f"test:{uuid.uuid4()}"
        assert await notify(s, ids, type="system", title="t", text="x", dedupe_key=key, exclude=actor) == 1
        await s.commit()
        assert await notify(s, ids, type="system", title="t", text="x", dedupe_key=key) == 1  # only the one not yet told
        await s.commit()
        assert await notify(s, ids, type="system", title="t", text="x", dedupe_key=key) == 0
        assert await notify(s, [], type="system", title="t", text="x") == 0
        rows = (await s.scalars(select(Notification).where(Notification.dedupe_key == key))).all()
        assert sorted(str(r.user_id) for r in rows) == sorted(str(i) for i in ids) and other


# ------------------------------------------------------------------ expiry sweep
async def test_expiry_sweep_warns_owners_once_and_only_hr_can_run_it(client, hr, hod, director, superadmin):
    _, _, staff_h = await make_person(client, hr)
    for who in (staff_h, hod, director):
        assert (await client.post(f"{API}/notifications/sweep", headers=who)).status_code == 403
    assert (await client.post(f"{API}/notifications/sweep", headers=hr, params={"days": 0})).status_code == 422

    sarah = await login(client, "staff@hospital.test")
    first = (await client.post(f"{API}/notifications/sweep", headers=hr, params={"days": 365})).json()["sent"]
    assert first >= 2  # Sarah's licence and her verified Medical Licence document both fall inside a year
    kinds = {n["type"] for n in (await inbox(client, sarah))["items"]}
    assert {"license", "document"} <= kinds
    assert (await client.post(f"{API}/notifications/sweep", headers=hr, params={"days": 365})).json() == {"sent": 0}
    assert (await client.post(f"{API}/notifications/sweep", headers=superadmin)).status_code == 200


async def test_notification_text_never_carries_money_or_personal_numbers(client, hr, director):
    sarah = await login(client, "staff@hospital.test")
    everything = str((await inbox(client, sarah))["items"]) + str((await inbox(client, director))["items"]) \
        + str((await inbox(client, hr))["items"])
    assert "₦" not in everything and "3045678901" not in everything and "22123456790" not in everything
