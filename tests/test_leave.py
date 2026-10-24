from datetime import date, timedelta

from .conftest import API, login, make_person


async def types(client, h):
    return {t["code"]: t["id"] for t in (await client.get(f"{API}/leave/types", headers=h)).json()}


def future(days_ahead, length):
    start = date.today() + timedelta(days=days_ahead)
    return {"start_date": start.isoformat(), "end_date": (start + timedelta(days=length - 1)).isoformat()}


async def request_leave(client, h, code="annual", ahead=30, length=3, **extra):
    t = await types(client, h)
    return await client.post(f"{API}/leave/requests", headers=h,
                             json={"leave_type_id": t[code], "reason": "Rest", **future(ahead, length), **extra})


async def test_full_approval_chain(client, hr, hod, director):
    staff, _, h = await make_person(client, hr)
    r = await request_leave(client, h, length=4)
    assert r.status_code == 201
    req = r.json()
    assert req["status"] == "Pending" and req["current_step_role"] == "hod" and req["days"] == 4
    assert [s["label"] for s in req["steps"]] == ["Submitted", "HOD Approved", "HR Approved", "DA Approved", "Completed"]
    assert [s["state"] for s in req["steps"]] == ["done", "current", "upcoming", "upcoming", "upcoming"]

    rid = req["id"]
    # Out-of-turn approvers are refused.
    assert (await client.post(f"{API}/leave/requests/{rid}/decision", headers=hr, json={"decision": "approve"})).status_code == 403
    assert (await client.post(f"{API}/leave/requests/{rid}/decision", headers=director, json={"decision": "approve"})).status_code == 403

    pend = await client.get(f"{API}/leave/requests", headers=hod, params={"awaiting_me": True, "size": 100})
    assert rid in [i["id"] for i in pend.json()["items"]]

    for who, role_after in ((hod, "hr"), (hr, "director"), (director, None)):
        r = await client.post(f"{API}/leave/requests/{rid}/decision", headers=who, json={"decision": "approve", "comment": "ok"})
        assert r.status_code == 200, r.text
        assert r.json()["current_step_role"] == role_after
    final = r.json()
    assert final["status"] == "Approved" and [s["state"] for s in final["steps"]] == ["done"] * 5

    # Balance reflects the approved days (15 annual - 4).
    bal = (await client.get(f"{API}/leave/balance", headers=h)).json()
    annual = next(b for b in bal["items"] if b["code"] == "annual")
    assert annual["used"] == 4 and annual["remaining"] == 11
    # A finished request can't be decided again.
    assert (await client.post(f"{API}/leave/requests/{rid}/decision", headers=hod, json={"decision": "approve"})).status_code == 409


async def test_decline_stops_the_chain_and_frees_balance(client, hr, hod):
    _, _, h = await make_person(client, hr)
    rid = (await request_leave(client, h, length=5)).json()["id"]
    r = await client.post(f"{API}/leave/requests/{rid}/decision", headers=hod, json={"decision": "decline", "comment": "Short-staffed"})
    assert r.json()["status"] == "Declined"
    assert [s["state"] for s in r.json()["steps"]][:2] == ["done", "declined"]
    annual = next(b for b in (await client.get(f"{API}/leave/balance", headers=h)).json()["items"] if b["code"] == "annual")
    assert annual["pending"] == 0 and annual["used"] == 0


async def test_hod_only_decides_for_own_department(client, hr, hod):
    _, _, h = await make_person(client, hr, dept="Surgery")
    rid = (await request_leave(client, h)).json()["id"]
    assert (await client.get(f"{API}/leave/requests/{rid}", headers=hod)).status_code == 404
    assert (await client.post(f"{API}/leave/requests/{rid}/decision", headers=hod, json={"decision": "approve"})).status_code == 404


async def test_hod_own_request_skips_hod_step_and_cannot_self_approve(client, hr, director):
    staff, _, h = await make_person(client, hr, role="hod", dept="Surgery")
    req = (await request_leave(client, h)).json()
    assert req["current_step_role"] == "hr"
    # HR approves, then the director; the HOD step never appears as 'current'.
    assert (await client.post(f"{API}/leave/requests/{req['id']}/decision", headers=h, json={"decision": "approve"})).status_code == 403
    assert (await client.post(f"{API}/leave/requests/{req['id']}/decision", headers=hr, json={"decision": "approve"})).status_code == 200
    r = await client.post(f"{API}/leave/requests/{req['id']}/decision", headers=director, json={"decision": "approve"})
    assert r.json()["status"] == "Approved"


async def test_validation_rules(client, hr):
    _, _, h = await make_person(client, hr)
    t = await types(client, h)
    # end before start
    r = await client.post(f"{API}/leave/requests", headers=h, json={"leave_type_id": t["annual"], "start_date": "2030-05-10", "end_date": "2030-05-01"})
    assert r.status_code == 422
    # annual leave can't start in the past, sick leave can be backdated
    past = {"start_date": (date.today() - timedelta(days=3)).isoformat(), "end_date": (date.today() - timedelta(days=2)).isoformat()}
    assert (await client.post(f"{API}/leave/requests", headers=h, json={"leave_type_id": t["annual"], **past})).status_code == 422
    assert (await client.post(f"{API}/leave/requests", headers=h, json={"leave_type_id": t["sick"], **past})).status_code == 201
    # over balance (annual placeholder entitlement is 15)
    assert (await request_leave(client, h, length=16, ahead=100)).status_code == 422
    # overlap
    assert (await request_leave(client, h, ahead=50, length=5)).status_code == 201
    assert (await request_leave(client, h, ahead=52, length=5)).status_code == 409


async def test_replacement_officer_must_be_in_same_department(client, hr):
    _, _, h = await make_person(client, hr, dept="Internal Medicine")
    surgeon, _, _ = await make_person(client, hr, dept="Surgery")
    mate, _, _ = await make_person(client, hr, dept="Internal Medicine")
    assert (await request_leave(client, h, replacement_staff_id=surgeon["id"])).status_code == 422
    r = await request_leave(client, h, replacement_staff_id=mate["id"], ahead=200)
    assert r.status_code == 201 and r.json()["replacement"] == mate["name"]


async def test_cancel_only_own_pending_request(client, hr, hod):
    _, _, h = await make_person(client, hr)
    _, _, other = await make_person(client, hr)
    rid = (await request_leave(client, h, ahead=300)).json()["id"]
    assert (await client.post(f"{API}/leave/requests/{rid}/cancel", headers=other)).status_code == 404
    assert (await client.post(f"{API}/leave/requests/{rid}/cancel", headers=h)).json()["status"] == "Cancelled"
    assert (await client.post(f"{API}/leave/requests/{rid}/cancel", headers=h)).status_code == 409


async def test_staff_only_see_their_own_requests(client, hr):
    _, _, a = await make_person(client, hr)
    _, _, b = await make_person(client, hr)
    await request_leave(client, a)
    assert (await client.get(f"{API}/leave/requests", headers=b)).json()["total"] == 0
    assert (await client.get(f"{API}/leave/requests", headers=a)).json()["total"] == 1


async def test_seeded_demo_history_and_hod_queue(client, hod):
    staff = await login(client, "staff@hospital.test")
    mine = (await client.get(f"{API}/leave/requests", headers=staff, params={"size": 100})).json()
    assert mine["total"] == 4 and all(i["status"] == "Approved" for i in mine["items"])
    queue = (await client.get(f"{API}/leave/requests", headers=hod, params={"awaiting_me": True, "size": 100})).json()
    assert queue["total"] >= 3


async def test_dashboards_per_role(client, hr, hod, director, superadmin):
    staff = await login(client, "staff@hospital.test")
    s = (await client.get(f"{API}/reports/dashboard", headers=staff)).json()
    assert s["kpis"]["total_staff"] == 1 and "leave_balance_days" in s["kpis"] and "department_distribution" not in s
    h = (await client.get(f"{API}/reports/dashboard", headers=hod)).json()
    assert [d["name"] for d in h["department_distribution"]] == ["Internal Medicine"] and "grade_distribution" not in h
    for who in (hr, director, superadmin):
        d = (await client.get(f"{API}/reports/dashboard", headers=who)).json()
        assert d["kpis"]["total_staff"] > 20 and len(d["leave_trend"]) == 12 and d["grade_distribution"]
    summary = (await client.get(f"{API}/users/summary", headers=superadmin)).json()
    assert summary["by_role"]["superadmin"] == 1 and summary["total"] >= 5
