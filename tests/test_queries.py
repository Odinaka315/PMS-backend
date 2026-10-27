"""Queries: the issue -> reply -> resolve -> appeal flow, who may do what, and who may see what."""
import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import StaffQuery

from .conftest import API, dept_id, login, make_person
from .test_notifications import inbox, titles, unread


async def issue(client, h, staff, **over):
    body = {"staff": staff, "title": "Absence without notice", "description": "You were away from the ward.",
            "severity": "Medium", **over}
    return await client.post(f"{API}/queries", headers=h, json=body)


async def act(client, h, qid, action, body, expect=200):
    r = await client.post(f"{API}/queries/{qid}/{action}", headers=h, json=body)
    assert r.status_code == expect, (action, r.status_code, r.text)
    return r.json()


async def tweak(qid, **fields):
    async with SessionLocal() as s:
        q = await s.get(StaffQuery, uuid.UUID(qid))
        for k, v in fields.items():
            setattr(q, k, v)
        await s.commit()


async def new_case(client, hr, hod):
    """A staff member in the HOD's department with an open query issued by the HOD."""
    staff, _, h = await make_person(client, hr)
    q = (await issue(client, hod, staff["file_number"])).json()
    return staff, h, q


# ------------------------------------------------------------------ issuing
async def test_hod_issues_a_query_and_the_person_is_told(client, hr, hod):
    staff, _, h = await make_person(client, hr)
    r = await issue(client, hod, staff["file_number"], severity="High")
    assert r.status_code == 201, r.text
    q = r.json()
    assert q["status"] == "Pending Reply" and q["severity"] == "High" and q["issued_by"] == "hod@hospital.test"
    assert q["reply_due"] == (date.today() + timedelta(days=3)).isoformat() and q["overdue"] is False
    assert [e["kind"] for e in q["events"]] == ["issued"]
    assert q["can_withdraw"] is False and q["can_resolve"] is False
    assert await titles(client, h) == ["A query has been issued to you"]
    mine = (await client.get(f"{API}/queries/{q['id']}", headers=h)).json()
    assert mine["can_reply"] is True and mine["can_resolve"] is False and mine["can_appeal"] is False


async def test_who_may_issue_and_to_whom(client, hr, hod, director, superadmin):
    inside, _, staff_h = await make_person(client, hr)
    outside, _, _ = await make_person(client, hr, dept="Surgery")
    assert (await issue(client, staff_h, inside["file_number"])).status_code == 403
    assert (await issue(client, superadmin, inside["file_number"])).status_code == 403
    assert (await issue(client, hod, outside["file_number"])).status_code == 404   # not their department
    assert (await issue(client, hod, "YAB-HOD-007")).status_code == 422            # not themselves
    assert (await issue(client, hr, "HR-001")).status_code == 422
    assert (await issue(client, hr, outside["file_number"])).status_code == 201
    assert (await issue(client, director, outside["file_number"])).status_code == 201
    assert (await issue(client, hr, "NOBODY-9")).status_code == 404


@pytest.mark.parametrize("over", [
    {"title": "ab"}, {"description": "  "}, {"severity": "Severe"},
    {"reply_due": (date.today() - timedelta(days=1)).isoformat()},
    {"reply_due": (date.today() + timedelta(days=31)).isoformat()},
])
async def test_invalid_queries_are_rejected(client, hr, hod, over):
    staff, _, _ = await make_person(client, hr)
    assert (await issue(client, hod, staff["file_number"], **over)).status_code == 422


async def test_reply_deadline_can_be_set(client, hr, hod):
    staff, _, _ = await make_person(client, hr)
    due = (date.today() + timedelta(days=10)).isoformat()
    assert (await issue(client, hod, staff["file_number"], reply_due=due)).json()["reply_due"] == due


# ------------------------------------------------------------------ the whole flow
async def test_full_flow_with_appeal_overturned(client, hr, hod, director, superadmin):
    staff, h, q = await new_case(client, hr, hod)
    qid, hr0, dir0 = q["id"], await unread(client, hr), await unread(client, director)

    q = await act(client, h, qid, "reply", {"text": "I told the ward sister."})
    assert q["status"] == "Replied" and q["replied_at"] and q["can_reply"] is False
    assert await unread(client, hr) == hr0 + 1  # waiting for a decision
    assert "A query has been replied to" in await titles(client, hod)

    q = await act(client, hr, qid, "resolve", {"outcome": "Warning", "note": "Reply not satisfactory"})
    assert q["status"] == "Resolved" and q["outcome"] == "Warning" and q["resolved_by"] == "hr@hospital.test"
    assert q["appeal_open_until"] == (date.today() + timedelta(days=14)).isoformat()
    note = (await inbox(client, h))["items"][0]
    assert note["title"] == "A query has been resolved" and "appeal" in note["text"] and "Absence" not in note["text"]

    mine = await act(client, h, qid, "appeal", {"grounds": "I was on approved leave"})
    assert mine["status"] == "Appealed" and mine["can_appeal"] is False
    assert await unread(client, director) == dir0 + 1

    done = await act(client, director, qid, "appeal-decision", {"decision": "Overturned", "note": "Leave records confirm"})
    assert done["status"] == "Resolved" and done["appeal_outcome"] == "Overturned" and done["outcome"] == "Cleared"
    assert [e["kind"] for e in done["events"]] == ["issued", "reply", "resolved", "appeal", "appeal_decision"]
    assert [e["role"] for e in done["events"]] == ["hod", "staff", "hr", "staff", "director"]
    assert done["events"][1]["by"] == done["events"][3]["by"] and done["events"][2]["detail"] == "Warning"
    assert done["events"][4]["detail"] == "Overturned"
    assert (await titles(client, h))[0] == "Your appeal has been decided"


async def test_an_upheld_appeal_keeps_the_original_outcome(client, hr, hod, director, superadmin):
    _, h, q = await new_case(client, hr, hod)
    await act(client, h, q["id"], "reply", {"text": "Explanation"})
    await act(client, director, q["id"], "resolve", {"outcome": "Reprimand", "note": "Noted"})
    await act(client, h, q["id"], "appeal", {"grounds": "Unfair"})
    # the director resolved it, so the director may not also judge the appeal; HR is the wrong role
    assert (await client.post(f"{API}/queries/{q['id']}/appeal-decision", headers=director,
                              json={"decision": "Upheld", "note": "x"})).status_code == 403
    assert (await client.post(f"{API}/queries/{q['id']}/appeal-decision", headers=hr,
                              json={"decision": "Upheld", "note": "x"})).status_code == 403
    done = await act(client, superadmin, q["id"], "appeal-decision", {"decision": "Upheld", "note": "Stands"})
    assert done["outcome"] == "Reprimand" and done["appeal_outcome"] == "Upheld"
    assert (await client.post(f"{API}/queries/{q['id']}/appeal-decision", headers=superadmin,
                              json={"decision": "Upheld", "note": "x"})).status_code == 409


# ------------------------------------------------------------------ rules on each step
async def test_only_the_person_queried_can_reply_and_only_while_waiting(client, hr, hod, director):
    _, h, q = await new_case(client, hr, hod)
    for who in (hod, hr, director):
        assert (await client.post(f"{API}/queries/{q['id']}/reply", headers=who, json={"text": "x"})).status_code == 403
    _, _, other = await make_person(client, hr)
    assert (await client.post(f"{API}/queries/{q['id']}/reply", headers=other, json={"text": "x"})).status_code == 404
    assert (await client.post(f"{API}/queries/{q['id']}/reply", headers=h, json={"text": "  "})).status_code == 422
    await act(client, h, q["id"], "reply", {"text": "Here is my reply"})
    await act(client, h, q["id"], "reply", {"text": "again"}, expect=409)


async def test_resolving_needs_a_reply_or_a_missed_deadline(client, hr, hod):
    _, h, q = await new_case(client, hr, hod)
    body = {"outcome": "Cleared", "note": "ok"}
    await act(client, hr, q["id"], "resolve", body, expect=409)        # no reply yet, not overdue
    await tweak(q["id"], reply_due=date.today() - timedelta(days=1))
    assert (await client.get(f"{API}/queries/{q['id']}", headers=hr)).json()["overdue"] is True
    done = await act(client, hr, q["id"], "resolve", body)             # deadline passed without a reply
    assert done["status"] == "Resolved" and done["outcome"] == "Cleared"
    await act(client, hr, q["id"], "resolve", body, expect=409)


async def test_nobody_resolves_their_own_issue_or_a_query_issued_to_them(client, hr, hod, director):
    staff, h, _ = await new_case(client, hr, hod)
    mine = (await issue(client, hr, staff["file_number"])).json()
    await act(client, h, mine["id"], "reply", {"text": "reply"})
    body = {"outcome": "Cleared", "note": "ok"}
    await act(client, hr, mine["id"], "resolve", body, expect=403)       # HR issued it
    await act(client, hod, mine["id"], "resolve", body, expect=403)      # wrong role
    await act(client, h, mine["id"], "resolve", body, expect=403)
    assert (await act(client, director, mine["id"], "resolve", body))["status"] == "Resolved"
    # a query issued TO the director cannot be decided by the director
    about_director = (await issue(client, hr, "DIR-001")).json()
    await act(client, director, about_director["id"], "reply", {"text": "My reply"})
    await act(client, director, about_director["id"], "resolve", body, expect=403)
    await act(client, director, about_director["id"], "withdraw", {"reason": "x"}, expect=403)
    assert (await client.get(f"{API}/queries/{about_director['id']}", headers=director)).json()["can_resolve"] is False


async def test_appeal_rules(client, hr, hod, director):
    _, h, q = await new_case(client, hr, hod)
    await act(client, h, q["id"], "appeal", {"grounds": "x"}, expect=409)       # not resolved yet
    await act(client, h, q["id"], "reply", {"text": "reply"})
    await act(client, hr, q["id"], "resolve", {"outcome": "Cleared", "note": "fine"})
    await act(client, h, q["id"], "appeal", {"grounds": "x"}, expect=409)       # nothing adverse to appeal

    _, h2, q2 = await new_case(client, hr, hod)
    await act(client, h2, q2["id"], "reply", {"text": "reply"})
    await act(client, hr, q2["id"], "resolve", {"outcome": "Warning", "note": "warned"})
    await act(client, hod, q2["id"], "appeal", {"grounds": "x"}, expect=403)    # only the person queried
    await act(client, h2, q2["id"], "appeal", {"grounds": " "}, expect=422)
    await tweak(q2["id"], resolved_at=__import__("app.core.security", fromlist=["utcnow"]).utcnow() - timedelta(days=15))
    out = await act(client, h2, q2["id"], "appeal", {"grounds": "too late"}, expect=409)
    assert "appeal period" in out["detail"]

    _, h3, q3 = await new_case(client, hr, hod)
    await act(client, h3, q3["id"], "reply", {"text": "reply"})
    await act(client, hr, q3["id"], "resolve", {"outcome": "Further Action", "note": "referred"})
    await act(client, h3, q3["id"], "appeal", {"grounds": "first"})
    await act(client, h3, q3["id"], "appeal", {"grounds": "second"}, expect=409)  # once only


async def test_withdrawing_a_query(client, hr, hod, director):
    staff, h, q = await new_case(client, hr, hod)
    await act(client, hod, q["id"], "withdraw", {"reason": "x"}, expect=403)
    await act(client, h, q["id"], "withdraw", {"reason": "x"}, expect=403)
    await act(client, hr, q["id"], "withdraw", {"reason": "  "}, expect=422)
    out = await act(client, hr, q["id"], "withdraw", {"reason": "Issued in error"})
    assert out["status"] == "Withdrawn" and out["events"][-1]["kind"] == "withdrawn"
    assert (await titles(client, h))[0] == "A query has been withdrawn"
    await act(client, h, q["id"], "reply", {"text": "late"}, expect=409)
    await act(client, hr, q["id"], "withdraw", {"reason": "again"}, expect=409)

    _, h2, q2 = await new_case(client, hr, hod)
    await act(client, h2, q2["id"], "reply", {"text": "r"})
    await act(client, director, q2["id"], "resolve", {"outcome": "Cleared", "note": "ok"})
    await act(client, hr, q2["id"], "withdraw", {"reason": "too late"}, expect=409)  # resolved queries stay on record


# ------------------------------------------------------------------ visibility
async def test_queries_are_visible_only_within_scope(client, hr, hod, director, superadmin):
    staff, h, q = await new_case(client, hr, hod)
    _, _, other = await make_person(client, hr)
    outsider, _, _ = await make_person(client, hr, dept="Surgery")
    url = f"{API}/queries/{q['id']}"
    assert (await client.get(url, headers=other)).status_code == 404
    for who in (h, hod, hr, director, superadmin):
        assert (await client.get(url, headers=who)).status_code == 200
    assert (await client.get(f"{API}/staff/{staff['file_number']}/queries", headers=other)).status_code == 404
    assert (await client.get(f"{API}/staff/{staff['file_number']}/queries", headers=h)).json()["total"] == 1
    assert (await client.get(f"{API}/queries/{uuid.uuid4()}", headers=hr)).status_code == 404
    # a person's own list holds only their own queries; an HOD's holds only their department's
    assert [x["id"] for x in (await client.get(f"{API}/queries", headers=h)).json()["items"]] == [q["id"]]
    assert (await client.get(f"{API}/queries", headers=other)).json()["total"] == 0
    sur = (await issue(client, hr, outsider["file_number"])).json()
    ids = {x["id"] for x in (await client.get(f"{API}/queries", headers=hod, params={"size": 100})).json()["items"]}
    assert q["id"] in ids and sur["id"] not in ids
    assert sur["id"] in {x["id"] for x in (await client.get(f"{API}/queries", headers=hr, params={"size": 100})).json()["items"]}
    assert (await client.get(f"{API}/queries", headers=h, params={"staff": outsider["file_number"]})).status_code == 404
    assert (await client.get(f"{API}/queries")).status_code == 401


async def test_reading_someone_elses_query_is_audited_but_your_own_is_not(client, hr, hod):
    staff, h, q = await new_case(client, hr, hod)
    trail = lambda: client.get(f"{API}/staff/{staff['file_number']}/audit", headers=hr, params={"size": 100})  # noqa: E731
    await client.get(f"{API}/queries/{q['id']}", headers=h)
    assert not [a for a in (await trail()).json()["items"] if a["action"] == "query_viewed"]
    await client.get(f"{API}/queries/{q['id']}", headers=hr)
    seen = [a for a in (await trail()).json()["items"] if a["action"] == "query_viewed"]
    assert len(seen) == 1 and seen[0]["by"] == "hr@hospital.test"
    actions = {a["action"] for a in (await trail()).json()["items"]}
    assert "query_issued" in actions


async def test_filters_awaiting_me_overdue_and_summary(client, hr, hod, director):
    staff, h, q = await new_case(client, hr, hod)
    awaiting = lambda who: client.get(f"{API}/queries", headers=who, params={"awaiting_me": True, "size": 100})  # noqa: E731
    assert [x["id"] for x in (await awaiting(h)).json()["items"]] == [q["id"]]       # they must reply
    assert q["id"] not in {x["id"] for x in (await awaiting(hr)).json()["items"]}     # nothing for HR yet
    await act(client, h, q["id"], "reply", {"text": "reply"})
    assert (await awaiting(h)).json()["total"] == 0
    assert q["id"] in {x["id"] for x in (await awaiting(hr)).json()["items"]}
    assert q["id"] in {x["id"] for x in (await awaiting(director)).json()["items"]}
    mine_hr = (await client.post(f"{API}/queries", headers=hr, json={
        "staff": staff["file_number"], "title": "HR issued", "description": "Details"})).json()
    await act(client, h, mine_hr["id"], "reply", {"text": "r"})
    assert mine_hr["id"] not in {x["id"] for x in (await awaiting(hr)).json()["items"]}  # HR cannot resolve its own
    assert mine_hr["id"] in {x["id"] for x in (await awaiting(director)).json()["items"]}

    _, h2, q2 = await new_case(client, hr, hod)
    await tweak(q2["id"], reply_due=date.today() - timedelta(days=2))
    late = (await client.get(f"{API}/queries", headers=hr, params={"overdue": True, "size": 100})).json()["items"]
    assert q2["id"] in {x["id"] for x in late} and all(x["overdue"] for x in late)

    s = (await client.get(f"{API}/queries/summary", headers=hr)).json()
    for key, status in (("pending_reply", "Pending Reply"), ("replied", "Replied"), ("appealed", "Appealed"),
                        ("resolved", "Resolved")):
        total = (await client.get(f"{API}/queries", headers=hr, params={"status": status})).json()["total"]
        assert s[key] == total, key
    assert s["overdue"] >= 1 and s["awaiting_me"] == (await awaiting(hr)).json()["total"]
    own = (await client.get(f"{API}/queries/summary", headers=h2)).json()
    assert own["pending_reply"] == 1 and own["overdue"] == 1 and own["awaiting_me"] == 1


async def test_seeded_demo_queries_match_the_frontend(client):
    sarah = await login(client, "staff@hospital.test")
    items = (await client.get(f"{API}/queries", headers=sarah, params={"size": 100})).json()["items"]
    by = {x["title"]: x for x in items}
    assert by["Attendance Irregularity"]["status"] == "Pending Reply" and by["Attendance Irregularity"]["can_reply"]
    assert by["Late Submission of Report"]["status"] == "Resolved" and by["Late Submission of Report"]["outcome"] == "Cleared"
    assert by["Patient Complaint"]["status"] == "Appealed" and by["Patient Complaint"]["severity"] == "High"


async def test_query_actions_are_audited(client, hr, hod):
    staff, h, q = await new_case(client, hr, hod)
    await act(client, h, q["id"], "reply", {"text": "reply"})
    await act(client, hr, q["id"], "resolve", {"outcome": "Warning", "note": "n"})
    trail = (await client.get(f"{API}/staff/{staff['file_number']}/audit", headers=hr, params={"size": 100})).json()["items"]
    got = {a["action"]: a["by"] for a in trail if a["action"].startswith("query_") and a["action"] != "query_viewed"}
    assert got["query_issued"] == "hod@hospital.test" and got["query_resolved"] == "hr@hospital.test"
    assert got["query_replied"] not in ("hod@hospital.test", "hr@hospital.test") and set(got) == {
        "query_issued", "query_replied", "query_resolved"}
