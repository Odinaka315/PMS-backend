"""Performance appraisals and goals."""
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models import Appraisal, Staff, User
from app.modules.performance import rules

from .conftest import API, login, make_person
from .test_notifications import inbox, titles, unread

CRITERIA = rules.DEFAULT_CRITERIA
KEYS = [c["key"] for c in CRITERIA]


def scores(v):
    return {k: v for k in KEYS}


async def make_cycle(client, hr, *, year=2099, past=False, **over):
    y = 2001 if past else year
    body = {"name": f"Test cycle {uuid.uuid4().hex[:8]}", "year": y, "start_date": f"{y}-01-01", "end_date": f"{y}-12-31",
            "self_deadline": f"{y}-12-01", **over}
    r = await client.post(f"{API}/performance/cycles", headers=hr, json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def open_cycle(client, hr, **kw):
    c = await make_cycle(client, hr, **kw)
    r = await client.post(f"{API}/performance/cycles/{c['id']}/open", headers=hr)
    assert r.status_code == 200, r.text
    return r.json()


async def appraisal_of(client, h, cycle, ident):
    r = await client.get(f"{API}/performance/appraisals", headers=h, params={"cycle": cycle["id"], "staff": ident})
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 1, r.text
    return r.json()["items"][0]


async def post(client, h, aid, action, body, expect=200):
    r = await client.post(f"{API}/performance/appraisals/{aid}/{action}", headers=h, json=body)
    assert r.status_code == expect, (action, r.status_code, r.text)
    return r.json()


async def person_and_cycle(client, hr, **kw):
    staff, _, h = await make_person(client, hr)  # Internal Medicine, so the demo HOD is the appraiser
    cycle = await open_cycle(client, hr, **kw)
    return staff, h, cycle, await appraisal_of(client, hr, cycle, staff["file_number"])


async def reviewed(client, hr, hod, value=80):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    await post(client, h, a["id"], "self-assessment", {"scores": scores(70), "comment": "Good"})
    await post(client, hod, a["id"], "review", {"scores": scores(value), "comment": "Solid"})
    return staff, h, cycle, a


# ------------------------------------------------------------------ the arithmetic
def test_weighted_score_and_rating_bands():
    assert sum(c["weight"] for c in CRITERIA) == 100
    assert rules.weighted_x100(scores(80), CRITERIA) == 8000
    mixed = {**scores(0), "job_quality": 100}
    assert rules.weighted_x100(mixed, CRITERIA) == 3000
    b = rules.DEFAULT_BANDS
    assert rules.rating_for(9000, b) == "Excellent" and rules.rating_for(8999, b) == "Very Good"
    assert rules.rating_for(7500, b) == "Very Good" and rules.rating_for(7499, b) == "Good"
    assert rules.rating_for(5000, b) == "Fair" and rules.rating_for(0, b) == "Poor" and rules.rating_for(10000, b) == "Excellent"


def test_score_validation():
    assert rules.validate_scores(scores(50), CRITERIA) == scores(50)
    for bad in ({**scores(50), "extra": 1}, {k: 50 for k in KEYS[:-1]}, {**scores(50), "teamwork": 101},
                {**scores(50), "teamwork": -1}, {**scores(50), "teamwork": True}, {**scores(50), "teamwork": 5.5}):
        with pytest.raises(HTTPException) as e:
            rules.validate_scores(bad, CRITERIA)
        assert e.value.status_code == 422


# ------------------------------------------------------------------ cycles
async def test_cycle_rules(client, hr, hod, director, superadmin):
    _, _, staff_h = await make_person(client, hr)
    c = await make_cycle(client, hr)
    assert c["status"] == "Draft" and c["criteria"] == CRITERIA and c["appraisals"] == 0
    body = {"name": c["name"], "year": c["year"], "start_date": "2099-01-01", "end_date": "2099-12-31", "self_deadline": "2099-12-01"}
    assert (await client.post(f"{API}/performance/cycles", headers=hr, json=body)).status_code == 409
    for who in (staff_h, hod, director, superadmin):
        assert (await client.post(f"{API}/performance/cycles", headers=who, json={**body, "name": "Another one"})).status_code == 403
        assert (await client.post(f"{API}/performance/cycles/{c['id']}/open", headers=who)).status_code == 403
    for who in (staff_h, hod):
        assert (await client.get(f"{API}/performance/cycles", headers=who)).status_code == 403
    for who in (hr, director, superadmin):
        assert c["id"] in [x["id"] for x in (await client.get(f"{API}/performance/cycles", headers=who)).json()]
    assert (await client.get(f"{API}/performance/cycles/{uuid.uuid4()}", headers=hr)).status_code == 404


@pytest.mark.parametrize("over", [
    {"criteria": [{"key": "a_one", "name": "A", "weight": 60}, {"key": "b_two", "name": "B", "weight": 30}]},   # not 100
    {"criteria": [{"key": "a_one", "name": "A", "weight": 50}, {"key": "a_one", "name": "B", "weight": 50}]},   # duplicate
    {"criteria": [{"key": "Bad Key", "name": "A", "weight": 100}]},
    {"criteria": []},
    {"rating_bands": [{"min": 50, "label": "Pass"}]},                                  # must end at 0
    {"rating_bands": [{"min": 0, "label": "Low"}, {"min": 50, "label": "High"}]},      # wrong order
    {"rating_bands": [{"min": 50, "label": "A"}, {"min": 50, "label": "B"}, {"min": 0, "label": "C"}]},
    {"self_deadline": "2098-01-01"}, {"end_date": "2098-12-31"},
])
async def test_invalid_cycles_are_rejected(client, hr, over):
    y = 2099
    body = {"name": f"Bad {uuid.uuid4().hex[:6]}", "year": y, "start_date": f"{y}-01-01", "end_date": f"{y}-12-31",
            "self_deadline": f"{y}-12-01", **over}
    assert (await client.post(f"{API}/performance/cycles", headers=hr, json=body)).status_code == 422


async def test_custom_criteria_and_bands_are_used(client, hr):
    c = await make_cycle(client, hr, criteria=[{"key": "only", "name": "Only one", "weight": 100}],
                         rating_bands=[{"min": 50, "label": "Pass"}, {"min": 0, "label": "Fail"}])
    assert [x["key"] for x in c["criteria"]] == ["only"] and c["rating_bands"][0]["label"] == "Pass"
    up = await client.put(f"{API}/performance/cycles/{c['id']}", headers=hr, json={
        "name": c["name"], "year": 2099, "start_date": "2099-01-01", "end_date": "2099-12-31", "self_deadline": "2099-11-01"})
    assert up.status_code == 200 and up.json()["self_deadline"] == "2099-11-01" and up.json()["criteria"] == CRITERIA


async def test_opening_creates_one_appraisal_per_eligible_person_and_freezes_the_form(client, hr, director):
    staff, _, _ = await make_person(client, hr)
    cycle = await open_cycle(client, hr)
    async with SessionLocal() as s:
        eligible = await s.scalar(select(func.count()).select_from(Staff).where(
            Staff.deleted_at.is_(None), Staff.status.in_(("Active", "On Leave"))))
    assert cycle["status"] == "Open" and cycle["appraisals"] == eligible and cycle["opened_at"]
    assert (await client.post(f"{API}/performance/cycles/{cycle['id']}/open", headers=hr)).status_code == 409
    up = {"name": cycle["name"], "year": 2099, "start_date": "2099-01-01", "end_date": "2099-12-31", "self_deadline": "2099-12-01"}
    assert (await client.put(f"{API}/performance/cycles/{cycle['id']}", headers=hr, json=up)).status_code == 409

    # People who join afterwards are added by sync, and only once.
    late, _, late_h = await make_person(client, hr)
    assert (await client.get(f"{API}/performance/appraisals", headers=hr, params={
        "cycle": cycle["id"], "staff": late["file_number"]})).json()["total"] == 0
    synced = await client.post(f"{API}/performance/cycles/{cycle['id']}/sync", headers=hr)
    assert synced.json()["appraisals"] == eligible + 1
    assert (await client.post(f"{API}/performance/cycles/{cycle['id']}/sync", headers=hr)).json()["appraisals"] == eligible + 1
    assert "Appraisal open" in await titles(client, late_h)
    # Each person is told once, even though two syncs ran.
    assert (await titles(client, late_h)).count("Appraisal open") == 1


async def test_an_empty_draft_cannot_hide_in_the_list_of_appraisals(client, hr):
    c = await make_cycle(client, hr)  # draft: nobody can see appraisals of a draft cycle
    got = await client.get(f"{API}/performance/appraisals", headers=hr, params={"cycle": c["id"]})
    assert got.json()["total"] == 0


async def test_who_is_assigned_as_appraiser(client, hr, hod):
    cycle = await open_cycle(client, hr)
    who = {}
    for ident in ("YAB-MED-042", "YAB-HOD-007", "HR-001", "DIR-001"):
        who[ident] = (await appraisal_of(client, hr, cycle, ident))["appraiser"]
    assert who == {"YAB-MED-042": "hod@hospital.test", "YAB-HOD-007": "director@hospital.test",
                   "HR-001": "director@hospital.test", "DIR-001": "superadmin@hospital.test"}


# ------------------------------------------------------------------ the main flow
async def test_full_appraisal_flow_and_what_the_person_can_see(client, hr, hod, director):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    aid, hod0 = a["id"], await unread(client, hod)
    mine = (await client.get(f"{API}/performance/appraisals", headers=h, params={"mine": True, "cycle": cycle["id"]})).json()
    assert mine["total"] == 1 and mine["items"][0]["status"] == "Not Started" and mine["items"][0]["can_self_assess"]
    assert "Appraisal open" in await titles(client, h)

    await post(client, h, aid, "self-assessment", {"scores": {**scores(60), "teamwork": 101}, "comment": "x"}, expect=422)
    await post(client, h, aid, "self-assessment", {"scores": {k: 60 for k in KEYS[1:]}, "comment": "x"}, expect=422)
    done = await post(client, h, aid, "self-assessment", {"scores": scores(60), "comment": "I did well"})
    assert done["status"] == "Self-Assessed" and done["criteria"][0]["self_score"] == 60
    assert await unread(client, hod) == hod0 + 1
    again = await post(client, h, aid, "self-assessment", {"scores": scores(75), "comment": "Changed my mind"})
    assert again["criteria"][0]["self_score"] == 75                       # allowed until the appraiser starts

    rev = await post(client, hod, aid, "review", {"scores": scores(92), "comment": "Excellent year"})
    assert rev["status"] == "Reviewed" and rev["score"] == 92.0 and rev["rating"] == "Excellent"
    await post(client, h, aid, "self-assessment", {"scores": scores(75), "comment": "x"}, expect=409)  # locked now

    # The person being appraised sees nothing of the scoring until it is final.
    seen = (await client.get(f"{API}/performance/appraisals/{aid}", headers=h)).json()
    assert seen["score"] is None and seen["rating"] is None and seen["appraiser_comment"] is None
    assert all(c["appraiser_score"] is None for c in seen["criteria"]) and seen["reviewed_at"] is None
    assert all(c["self_score"] == 75 for c in seen["criteria"]) and seen["can_acknowledge"] is False
    listed = (await client.get(f"{API}/performance/appraisals", headers=h, params={"mine": True, "cycle": cycle["id"]})).json()
    assert listed["items"][0]["score"] is None
    assert (await client.get(f"{API}/performance/appraisals/{aid}", headers=hod)).json()["score"] == 92.0

    fin = await post(client, hr, aid, "finalise", {"comment": "Agreed"})
    assert fin["status"] == "Finalised" and fin["finalised_at"]
    assert "Your appraisal is final" in await titles(client, h)
    seen = (await client.get(f"{API}/performance/appraisals/{aid}", headers=h)).json()
    assert seen["score"] == 92.0 and seen["rating"] == "Excellent" and seen["appraiser_comment"] == "Excellent year"
    assert seen["can_acknowledge"] is True
    ack = await post(client, h, aid, "acknowledge", {"comment": "Thank you"})
    assert ack["acknowledged_at"] and ack["staff_comment"] == "Thank you" and ack["can_acknowledge"] is False
    await post(client, h, aid, "acknowledge", {}, expect=409)

    hist = (await client.get(f"{API}/performance/history", headers=h)).json()
    assert [x["score"] for x in hist] == [92.0] and hist[0]["rating"] == "Excellent"


async def test_rules_for_who_can_score_and_finalise(client, hr, hod, director, superadmin):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    aid = a["id"]
    body = {"scores": scores(70), "comment": "c"}
    await post(client, hod, aid, "review", body, expect=409)         # no self-assessment yet, deadline in the future
    await post(client, h, aid, "self-assessment", {"scores": scores(70), "comment": "c"})
    await post(client, h, aid, "review", body, expect=403)           # nobody appraises themselves
    await post(client, hr, aid, "review", body, expect=403)          # not the appraiser
    await post(client, director, aid, "review", body, expect=403)
    await post(client, hr, aid, "finalise", {}, expect=409)          # nothing to finalise yet
    await post(client, hod, aid, "review", body)
    await post(client, hod, aid, "finalise", {}, expect=403)         # appraisers do not finalise
    await post(client, h, aid, "finalise", {}, expect=403)
    await post(client, superadmin, aid, "finalise", {}, expect=403)
    await post(client, hod, aid, "review", {"scores": scores(85), "comment": "rescored"})  # may re-score until finalised
    assert (await client.get(f"{API}/performance/appraisals/{aid}", headers=hod)).json()["score"] == 85.0
    await post(client, hr, aid, "finalise", {})
    await post(client, hod, aid, "review", body, expect=409)         # final is final
    await post(client, hr, aid, "finalise", {}, expect=409)


async def test_nobody_finalises_an_appraisal_they_scored(client, hr, director):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    async with SessionLocal() as s:
        hr_id = await s.scalar(select(User.id).where(User.email == "hr@hospital.test"))
    r = await client.put(f"{API}/performance/appraisals/{a['id']}/appraiser", headers=hr, json={"user_id": str(hr_id)})
    assert r.status_code == 200 and r.json()["appraiser"] == "hr@hospital.test"
    await post(client, h, a["id"], "self-assessment", {"scores": scores(70), "comment": "c"})
    await post(client, hr, a["id"], "review", {"scores": scores(70), "comment": "c"})
    await post(client, hr, a["id"], "finalise", {}, expect=403)
    assert (await post(client, director, a["id"], "finalise", {}))["status"] == "Finalised"


async def test_sending_an_appraisal_back(client, hr, hod, director):
    staff, h, cycle, a = await reviewed(client, hr, hod)
    await post(client, hod, a["id"], "return", {"reason": "x"}, expect=403)
    await post(client, hr, a["id"], "return", {"reason": " "}, expect=422)
    back = await post(client, hr, a["id"], "return", {"reason": "Scores look too generous"})
    assert back["status"] == "Self-Assessed" and back["score"] is None and back["return_reason"]
    assert "Appraisal sent back to you" in await titles(client, hod)
    mine = (await client.get(f"{API}/performance/appraisals/{a['id']}", headers=h)).json()
    assert mine["return_reason"] is None                              # an internal note, never shown to the person
    await post(client, hr, a["id"], "return", {"reason": "again"}, expect=409)
    again = await post(client, hod, a["id"], "review", {"scores": scores(75), "comment": "Re-scored"})
    assert again["status"] == "Reviewed" and again["score"] == 75.0 and again["return_reason"] is None


async def test_appraiser_can_score_without_self_assessment_after_the_deadline(client, hr, hod):
    staff, _, cycle, a = await person_and_cycle(client, hr, past=True)
    assert a["status"] == "Not Started"
    out = await post(client, hod, a["id"], "review", {"scores": scores(65), "comment": "No self-assessment provided"})
    assert out["status"] == "Reviewed" and out["rating"] == "Good" and out["self_submitted_at"] is None


async def test_closing_a_cycle_stops_further_work_but_not_acknowledgement(client, hr, hod):
    staff, h, cycle, a = await reviewed(client, hr, hod)
    await post(client, hr, a["id"], "finalise", {})
    other, h2, _, a2 = await person_and_cycle(client, hr)
    closed = await client.post(f"{API}/performance/cycles/{a2['cycle_id']}/close", headers=hr)
    assert closed.status_code == 200 and closed.json()["status"] == "Closed" and closed.json()["closed_at"]
    assert (await client.post(f"{API}/performance/cycles/{a2['cycle_id']}/close", headers=hr)).status_code == 409
    await post(client, h2, a2["id"], "self-assessment", {"scores": scores(70), "comment": "c"}, expect=409)
    await post(client, hod, a2["id"], "review", {"scores": scores(70), "comment": "c"}, expect=409)
    assert (await post(client, h, a["id"], "acknowledge", {}))["acknowledged_at"]


async def test_appraiser_assignment_rules(client, hr, hod, director):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    async with SessionLocal() as s:
        ids = {u.email: u.id for u in (await s.scalars(select(User))).all()}
        own = await s.scalar(select(User.id).where(User.staff_id == uuid.UUID(staff["id"])))
    put = lambda who, uid: client.put(f"{API}/performance/appraisals/{a['id']}/appraiser", headers=who,  # noqa: E731
                                     json={"user_id": str(uid)})
    assert (await put(hod, ids["director@hospital.test"])).status_code == 403
    assert (await put(director, ids["director@hospital.test"])).status_code == 403
    assert (await put(hr, ids["staff@hospital.test"])).status_code == 422     # ordinary staff cannot appraise
    assert (await put(hr, own)).status_code == 422                           # nor oneself (also not a valid role)
    assert (await put(hr, uuid.uuid4())).status_code == 422
    ok = await put(hr, ids["director@hospital.test"])
    assert ok.status_code == 200 and ok.json()["appraiser"] == "director@hospital.test"
    await post(client, h, a["id"], "self-assessment", {"scores": scores(70), "comment": "c"})
    await post(client, director, a["id"], "review", {"scores": scores(70), "comment": "c"})
    assert (await put(hr, ids["hod@hospital.test"])).status_code == 409       # too late once scored


# ------------------------------------------------------------------ visibility
async def test_appraisals_are_visible_only_to_those_with_a_reason_to_see_them(client, hr, hod, director):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    outsider, _, _ = await make_person(client, hr, dept="Surgery")
    await client.post(f"{API}/performance/cycles/{cycle['id']}/sync", headers=hr)
    sur = await appraisal_of(client, hr, cycle, outsider["file_number"])
    _, _, other = await make_person(client, hr)
    url = f"{API}/performance/appraisals/{a['id']}"
    assert (await client.get(url, headers=other)).status_code == 404
    for who in (h, hod, hr, director):
        assert (await client.get(url, headers=who)).status_code == 200
    assert (await client.get(f"{API}/performance/appraisals/{sur['id']}", headers=hod)).status_code == 404  # other department
    assert (await client.get(f"{API}/performance/appraisals/{sur['id']}", headers=h)).status_code == 404
    mine = (await client.get(f"{API}/performance/appraisals", headers=h, params={"size": 100})).json()
    assert {x["staff_id"] for x in mine["items"]} == {staff["id"]}
    ids = {x["id"] for x in (await client.get(f"{API}/performance/appraisals", headers=hod, params={
        "cycle": cycle["id"], "size": 100})).json()["items"]}
    assert a["id"] in ids and sur["id"] not in ids
    for who in (h, other):  # others cannot act on it either
        await post(client, who, a["id"], "review", {"scores": scores(50), "comment": "x"}, expect=403 if who is h else 404)
    await post(client, other, a["id"], "self-assessment", {"scores": scores(50), "comment": "x"}, expect=404)
    assert (await client.get(f"{API}/performance/appraisals/{uuid.uuid4()}", headers=hr)).status_code == 404
    assert (await client.get(f"{API}/performance/appraisals")).status_code == 401


async def test_an_assigned_appraiser_outside_the_department_can_still_see_it(client, hr, hod):
    staff, _, h = await make_person(client, hr, dept="Surgery")
    cycle = await open_cycle(client, hr)
    a = await appraisal_of(client, hr, cycle, staff["file_number"])
    assert a["appraiser"] != "hod@hospital.test"       # the demo HOD heads a different department
    assert (await client.get(f"{API}/performance/appraisals/{a['id']}", headers=hod)).status_code == 404
    async with SessionLocal() as s:
        hod_id = await s.scalar(select(User.id).where(User.email == "hod@hospital.test"))
    assert (await client.put(f"{API}/performance/appraisals/{a['id']}/appraiser", headers=hr,
                             json={"user_id": str(hod_id)})).status_code == 200
    assert (await client.get(f"{API}/performance/appraisals/{a['id']}", headers=hod)).status_code == 200
    assert (await post(client, h, a["id"], "self-assessment", {"scores": scores(70), "comment": "c"}))["status"] == "Self-Assessed"
    assert (await post(client, hod, a["id"], "review", {"scores": scores(70), "comment": "c"}))["status"] == "Reviewed"


async def test_reading_someone_elses_appraisal_is_audited(client, hr, hod):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    trail = lambda: client.get(f"{API}/staff/{staff['file_number']}/audit", headers=hr, params={"size": 100})  # noqa: E731
    await client.get(f"{API}/performance/appraisals/{a['id']}", headers=h)
    assert not [x for x in (await trail()).json()["items"] if x["action"] == "appraisal_viewed"]
    await client.get(f"{API}/performance/appraisals/{a['id']}", headers=hod)
    assert [x["by"] for x in (await trail()).json()["items"] if x["action"] == "appraisal_viewed"] == ["hod@hospital.test"]


async def test_awaiting_me_points_each_person_at_their_next_step(client, hr, hod, director):
    staff, h, cycle, a = await person_and_cycle(client, hr)
    mine = lambda who: client.get(f"{API}/performance/appraisals", headers=who, params={  # noqa: E731
        "awaiting_me": True, "cycle": cycle["id"], "size": 100})
    ids = lambda r: {x["id"] for x in r.json()["items"]}  # noqa: E731
    assert a["id"] in ids(await mine(h)) and a["id"] not in ids(await mine(hod)) and a["id"] not in ids(await mine(hr))
    await post(client, h, a["id"], "self-assessment", {"scores": scores(70), "comment": "c"})
    assert a["id"] not in ids(await mine(h)) and a["id"] in ids(await mine(hod))
    await post(client, hod, a["id"], "review", {"scores": scores(70), "comment": "c"})
    assert a["id"] not in ids(await mine(hod)) and a["id"] in ids(await mine(hr)) and a["id"] in ids(await mine(director))
    await post(client, hr, a["id"], "finalise", {})
    assert a["id"] in ids(await mine(h))                      # now they should acknowledge it
    await post(client, h, a["id"], "acknowledge", {})
    assert a["id"] not in ids(await mine(h))


async def test_summary_counts_and_averages(client, hr, hod, director):
    staff, h, cycle, a = await reviewed(client, hr, hod, value=80)
    await post(client, hr, a["id"], "finalise", {})
    s = (await client.get(f"{API}/performance/summary", headers=hr, params={"cycle": cycle["id"]})).json()
    assert s["cycle"]["id"] == cycle["id"] and [x["status"] for x in s["by_status"]] == [
        "Not Started", "Self-Assessed", "Reviewed", "Finalised"]
    counts = {x["status"]: x["count"] for x in s["by_status"]}
    assert counts["Finalised"] == 1 and sum(counts.values()) == cycle["appraisals"]
    assert s["average_score"] == 80.0 and {x["status"]: x["count"] for x in s["ratings"]}["Very Good"] == 1
    med = next(d for d in s["by_department"] if d["department"] == "Internal Medicine")
    assert med["finalised"] == 1 and med["average"] == 80.0
    scoped = (await client.get(f"{API}/performance/summary", headers=hod, params={"cycle": cycle["id"]})).json()
    assert [d["department"] for d in scoped["by_department"]] == ["Internal Medicine"]
    _, _, staff_h = await make_person(client, hr)
    assert (await client.get(f"{API}/performance/summary", headers=staff_h)).status_code == 403
    draft = await make_cycle(client, hr)
    assert (await client.get(f"{API}/performance/summary", headers=hr, params={"cycle": draft["id"]})).json()["cycle"] is None


async def test_seeded_history_and_open_cycle_for_the_demo_staff_member(client, hr, hod):
    sarah = await login(client, "staff@hospital.test")
    hist = (await client.get(f"{API}/performance/history", headers=sarah)).json()
    years = [x["year"] for x in hist]
    assert len(hist) >= 2 and years == sorted(years) and all(x["rating"] == "Excellent" for x in hist[:2])
    assert (await client.get(f"{API}/performance/history", params={"staff": "YAB-MED-042"}, headers=hod)).json() == hist
    _, _, other = await make_person(client, hr)
    assert (await client.get(f"{API}/performance/history", params={"staff": "YAB-MED-042"}, headers=other)).status_code == 404
    todo = (await client.get(f"{API}/performance/appraisals", headers=sarah, params={"awaiting_me": True})).json()["items"]
    assert any(x["status"] == "Not Started" and x["can_self_assess"] for x in todo)


async def test_appraisal_actions_are_audited(client, hr, hod):
    staff, h, cycle, a = await reviewed(client, hr, hod)
    await post(client, hr, a["id"], "finalise", {})
    await post(client, h, a["id"], "acknowledge", {})
    trail = (await client.get(f"{API}/staff/{staff['file_number']}/audit", headers=hr, params={"size": 100})).json()["items"]
    got = {x["action"]: x["by"] for x in trail if x["action"].startswith("appraisal_") and x["action"] != "appraisal_viewed"}
    assert set(got) == {"appraisal_self_assessed", "appraisal_reviewed", "appraisal_finalised", "appraisal_acknowledged"}
    assert got["appraisal_reviewed"] == "hod@hospital.test" and got["appraisal_finalised"] == "hr@hospital.test"
    assert "Solid" not in str(trail)  # comments are not copied into the audit trail


# ------------------------------------------------------------------ goals
async def test_staff_set_and_update_their_own_goals(client, hr):
    _, _, h = await make_person(client, hr)
    r = await client.post(f"{API}/performance/goals", headers=h, json={"title": "Finish ward audit", "target": "By June"})
    assert r.status_code == 201, r.text
    g = r.json()
    assert g["progress"] == 0 and g["status"] == "Active" and g["can_edit"] and g["can_delete"]
    assert [x["id"] for x in (await client.get(f"{API}/performance/goals", headers=h)).json()] == [g["id"]]
    up = (await client.patch(f"{API}/performance/goals/{g['id']}", headers=h, json={"progress": 60})).json()
    assert up["progress"] == 60 and up["status"] == "Active"
    done = (await client.patch(f"{API}/performance/goals/{g['id']}", headers=h, json={"progress": 100})).json()
    assert done["status"] == "Achieved"
    other = (await client.post(f"{API}/performance/goals", headers=h, json={"title": "Learn a skill"})).json()
    ach = (await client.patch(f"{API}/performance/goals/{other['id']}", headers=h, json={"status": "Achieved"})).json()
    assert ach["progress"] == 100
    assert (await client.patch(f"{API}/performance/goals/{g['id']}", headers=h, json={"progress": 101})).status_code == 422
    assert (await client.patch(f"{API}/performance/goals/{g['id']}", headers=h, json={"status": "Lost"})).status_code == 422
    assert (await client.post(f"{API}/performance/goals", headers=h, json={"title": "ab"})).status_code == 422
    assert len((await client.get(f"{API}/performance/goals", headers=h, params={"status": "Achieved"})).json()) == 2
    assert (await client.delete(f"{API}/performance/goals/{g['id']}", headers=h)).status_code == 204
    assert (await client.delete(f"{API}/performance/goals/{g['id']}", headers=h)).status_code == 404


async def test_who_can_set_goals_for_others(client, hr, hod, director):
    staff, _, h = await make_person(client, hr)
    outsider, _, _ = await make_person(client, hr, dept="Surgery")
    _, _, peer = await make_person(client, hr)
    body = {"title": "Reduce waiting time", "staff": staff["file_number"]}
    assert (await client.post(f"{API}/performance/goals", headers=peer, json=body)).status_code in (403, 404)
    assert (await client.post(f"{API}/performance/goals", headers=h, json={**body, "staff": outsider["file_number"]})).status_code == 404
    assert (await client.post(f"{API}/performance/goals", headers=director, json=body)).status_code == 403
    assert (await client.post(f"{API}/performance/goals", headers=hod, json={**body, "staff": outsider["file_number"]})).status_code == 404
    r = await client.post(f"{API}/performance/goals", headers=hod, json=body)
    assert r.status_code == 201 and r.json()["created_by"] == "hod@hospital.test"
    gid = r.json()["id"]
    assert "A goal was set for you" in await titles(client, h)
    # the person can update progress but cannot delete a goal their HOD set; HR and the HOD can
    assert (await client.patch(f"{API}/performance/goals/{gid}", headers=h, json={"progress": 40})).json()["can_delete"] is False
    assert (await client.delete(f"{API}/performance/goals/{gid}", headers=h)).status_code == 403
    assert (await client.patch(f"{API}/performance/goals/{gid}", headers=peer, json={"progress": 1})).status_code == 404
    assert (await client.patch(f"{API}/performance/goals/{gid}", headers=director, json={"progress": 1})).status_code == 403
    assert (await client.patch(f"{API}/performance/goals/{gid}", headers=hod, json={"title": "Reduce waiting times"})).status_code == 200
    assert (await client.get(f"{API}/performance/goals", headers=hod, params={"staff": staff["file_number"]})).status_code == 200
    assert (await client.get(f"{API}/performance/goals", headers=peer, params={"staff": staff["file_number"]})).status_code == 404
    assert (await client.delete(f"{API}/performance/goals/{gid}", headers=hr)).status_code == 204


async def test_the_seeded_goals_match_the_frontend(client):
    sarah = await login(client, "staff@hospital.test")
    goals = {g["title"]: g["progress"] for g in (await client.get(f"{API}/performance/goals", headers=sarah)).json()}
    assert goals == {"Clinical quality": 96, "Patient feedback": 92, "Training completion": 88}
