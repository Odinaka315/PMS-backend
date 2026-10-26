"""Payroll: calculation, run lifecycle, who can see what, and that payslips are snapshots."""
import itertools

import pytest
from sqlalchemy import func, select

from app.core.db import SessionLocal
from app.models import AuditLog, PayrollRun, Payslip, User
from app.modules.payroll import calc
from app.modules.payroll.router import _safe

from .conftest import API, login, make_person

_months = itertools.count(0)


def next_period():
    """Every test gets its own month (the past, from 2001) so runs never collide, as one run per month is allowed."""
    n = next(_months)
    return 2001 + n // 12, 1 + n % 12


async def new_run(client, hr, **override):
    y, m = next_period()
    r = await client.post(f"{API}/payroll/runs", headers=hr, json={"year": y, "month": m, **override})
    assert r.status_code == 201, r.text
    return r.json()


async def payslip_for(client, hr, run_id, file_number):
    r = await client.get(f"{API}/payroll/runs/{run_id}/payslips", headers=hr, params={"q": file_number})
    assert r.status_code == 200, r.text
    items = [p for p in r.json()["items"] if p["file_number"] == file_number]
    assert len(items) == 1, r.text
    return items[0]


async def approve_and_pay(client, hr, director, run_id):
    a = await client.post(f"{API}/payroll/runs/{run_id}/approve", headers=director, json={"acknowledge_exceptions": True})
    assert a.status_code == 200, a.text
    p = await client.post(f"{API}/payroll/runs/{run_id}/pay", headers=hr)
    assert p.status_code == 200, p.text
    return p.json()


@pytest.fixture(scope="module")
async def sarah(client):
    return await login(client, "staff@hospital.test")


# ------------------------------------------------------------------ the arithmetic
def test_percentages_round_half_up_in_whole_kobo():
    assert calc.percent(10_000_00, 2500) == 2_500_00
    assert calc.percent(1, 5000) == 1       # 0.5 kobo rounds up
    assert calc.percent(1, 4999) == 0
    assert calc.percent(333, 1000) == 33    # 33.3 -> 33


def test_payslip_arithmetic_and_ordering():
    comps = [
        calc.Component("HOUSING", "Housing", "earning", "percent_of_basic", None, 2500, None, None, 10),
        calc.Component("BONUS", "Fixed bonus", "earning", "fixed", 5_000_00, None, None, None, 20),
        calc.Component("PENSION", "Pension", "deduction", "percent_of_basic", None, 800, None, None, 10),
        calc.Component("TAX", "Tax", "deduction", "percent_of_gross", None, 1000, None, None, 20),
    ]
    lines, gross, ded, net = calc.compute(100_000_00, 8, comps, [])
    assert gross == 100_000_00 + 25_000_00 + 5_000_00
    # tax is a share of GROSS (130,000), pension a share of BASIC (100,000)
    assert ded == 8_000_00 + 13_000_00 and net == gross - ded
    assert [x["code"] for x in lines] == ["BASIC", "HOUSING", "BONUS", "PENSION", "TAX"]


def test_level_limits_and_individual_adjustments():
    only_senior = calc.Component("SENIOR", "Senior", "earning", "fixed", 1_000_00, None, 10, 15)
    assert calc.applies(only_senior, 10) and calc.applies(only_senior, 15)
    assert not calc.applies(only_senior, 9) and not calc.applies(only_senior, None)
    extras = [calc.Extra("1", "earning", "Call duty", 2_000_00), calc.Extra("2", "deduction", "Loan", 500_00)]
    lines, gross, ded, net = calc.compute(10_000_00, 5, [only_senior], extras)
    assert (gross, ded, net) == (12_000_00, 500_00, 11_500_00)
    assert "SENIOR" not in [x["code"] for x in lines]


# ------------------------------------------------------------------ a full run
async def test_run_totals_match_the_payslips_and_each_payslip_adds_up(client, hr, director):
    run = await new_run(client, hr)
    assert run["status"] == "Draft" and run["staff_count"] > 0 and run["created_by"] == "hr@hospital.test"
    page = (await client.get(f"{API}/payroll/runs/{run['id']}/payslips", headers=hr, params={"size": 100})).json()
    assert page["total"] == run["staff_count"]
    pages = [page]
    for n in range(2, (page["total"] + 99) // 100 + 1):
        pages.append((await client.get(f"{API}/payroll/runs/{run['id']}/payslips", headers=hr,
                                       params={"size": 100, "page": n})).json())
    items = [p for pg in pages for p in pg["items"]]
    assert round(sum(p["net"] for p in items), 2) == run["total_net"]
    assert round(sum(p["gross"] for p in items), 2) == run["total_gross"]
    assert round(sum(p["deductions"] for p in items), 2) == run["total_deductions"]
    assert all(round(p["gross"] - p["deductions"], 2) == p["net"] for p in items)
    assert sum(1 for p in items if p["exception"]) == run["exception_count"]

    # One payslip in detail: earnings add to gross, deductions to deductions.
    s = await payslip_for(client, hr, run["id"], "YAB-MED-042")
    d = (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hr)).json()
    assert round(sum(x["amount"] for x in d["earnings"]), 2) == d["gross"] == round(d["basic"] + sum(
        x["amount"] for x in d["earnings"] if x["code"] != "BASIC"), 2)
    assert round(sum(x["amount"] for x in d["deduction_lines"]), 2) == d["deductions"]
    assert d["earnings"][0]["code"] == "BASIC" and d["run_status"] == "Draft"
    assert d["bank_name"] == "First Bank of Nigeria" and d["account_last4"] == "8901"
    assert d["exception"] is None


async def test_the_full_account_number_never_appears_in_any_payroll_output(client, hr, director):
    run = await new_run(client, hr)
    s = await payslip_for(client, hr, run["id"], "YAB-MED-042")
    texts = [(await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hr)).text,
             (await client.get(f"{API}/payroll/runs/{run['id']}", headers=hr)).text,
             (await client.get(f"{API}/payroll/runs/{run['id']}/export", headers=hr)).text,
             (await client.get(f"{API}/payroll/runs/{run['id']}/payslips", headers=hr, params={"size": 100})).text]
    assert all("3045678901" not in t for t in texts)
    assert "8901" in texts[0]


async def test_staff_without_a_scale_or_bank_are_flagged_not_silently_paid(client, hr):
    person, _, _ = await make_person(client, hr, account=False)  # created with no grade and no bank account
    run = await new_run(client, hr)
    p = await payslip_for(client, hr, run["id"], person["file_number"])
    assert p["exception"] == "No grade level or step on record" and p["net"] == 0 and p["gross"] == 0
    only = (await client.get(f"{API}/payroll/runs/{run['id']}/payslips", headers=hr,
                             params={"exceptions_only": True, "size": 100})).json()
    assert only["total"] == run["exception_count"] and all(i["exception"] for i in only["items"])
    detail = (await client.get(f"{API}/payroll/runs/{run['id']}", headers=hr)).json()
    assert any(e["file_number"] == person["file_number"] for e in detail["exceptions"])
    # Someone with a grade but no bank account is still calculated, but flagged.
    seeded = await payslip_for(client, hr, run["id"], "YAB-HOD-007")
    assert seeded["exception"] == "No bank account on record" and seeded["net"] > 0


# ------------------------------------------------------------------ lifecycle rules
async def test_lifecycle_and_state_guards(client, hr, director, superadmin):
    run = await new_run(client, hr)
    rid = run["id"]
    base = f"{API}/payroll/runs/{rid}"

    assert (await client.post(f"{base}/pay", headers=hr)).status_code == 409          # not approved yet
    assert (await client.post(f"{base}/approve", headers=hr, json={})).status_code == 403  # HR does not approve
    assert (await client.post(f"{base}/approve", headers=director, json={})).status_code == 409  # exceptions unacknowledged
    ok = await client.post(f"{base}/approve", headers=director, json={"acknowledge_exceptions": True})
    assert ok.status_code == 200 and ok.json()["status"] == "Approved" and ok.json()["approved_by"] == "director@hospital.test"
    assert ok.json()["exceptions_acknowledged"] is True

    assert (await client.post(f"{base}/approve", headers=superadmin, json={"acknowledge_exceptions": True})).status_code == 409
    assert (await client.post(f"{base}/recalculate", headers=hr)).status_code == 409
    assert (await client.delete(base, headers=hr)).status_code == 409
    assert (await client.post(f"{base}/pay", headers=director)).status_code == 403      # only HR records payment
    paid = await client.post(f"{base}/pay", headers=hr)
    assert paid.status_code == 200 and paid.json()["status"] == "Paid" and paid.json()["paid_at"]
    assert (await client.post(f"{base}/pay", headers=hr)).status_code == 409
    assert (await client.post(f"{base}/approve", headers=director, json={"acknowledge_exceptions": True})).status_code == 409


async def test_super_admin_can_approve(client, hr, superadmin):
    run = await new_run(client, hr)
    r = await client.post(f"{API}/payroll/runs/{run['id']}/approve", headers=superadmin,
                          json={"acknowledge_exceptions": True})
    assert r.status_code == 200 and r.json()["approved_by"] == "superadmin@hospital.test"


async def test_the_creator_of_a_run_cannot_approve_it(client, hr, director):
    run = await new_run(client, hr)
    async with SessionLocal() as s:  # make the director the creator, to prove the rule bites whoever it is
        r = await s.get(PayrollRun, __import__("uuid").UUID(run["id"]))
        r.created_by = await s.scalar(select(User.id).where(User.email == "director@hospital.test"))
        await s.commit()
    r = await client.post(f"{API}/payroll/runs/{run['id']}/approve", headers=director, json={"acknowledge_exceptions": True})
    assert r.status_code == 403 and "created" in r.json()["detail"]


async def test_period_rules(client, hr):
    y, m = next_period()
    assert (await client.post(f"{API}/payroll/runs", headers=hr, json={"year": y, "month": m})).status_code == 201
    dup = await client.post(f"{API}/payroll/runs", headers=hr, json={"year": y, "month": m})
    assert dup.status_code == 409 and "already exists" in dup.json()["detail"]
    assert (await client.post(f"{API}/payroll/runs", headers=hr, json={"year": 2999, "month": 1})).status_code == 422
    from datetime import date
    nxt = date.today().replace(day=28) + __import__("datetime").timedelta(days=10)
    assert (await client.post(f"{API}/payroll/runs", headers=hr, json={"year": nxt.year, "month": nxt.month})).status_code == 422
    for bad in ({"year": 1999, "month": 1}, {"year": 2020, "month": 13}, {"year": 2020, "month": 0}):
        assert (await client.post(f"{API}/payroll/runs", headers=hr, json=bad)).status_code == 422


async def test_deleting_a_draft_removes_its_payslips(client, hr):
    run = await new_run(client, hr)
    assert (await client.delete(f"{API}/payroll/runs/{run['id']}", headers=hr)).status_code == 204
    assert (await client.get(f"{API}/payroll/runs/{run['id']}", headers=hr)).status_code == 404
    async with SessionLocal() as s:
        left = await s.scalar(select(func.count()).select_from(Payslip).where(Payslip.run_id == __import__("uuid").UUID(run["id"])))
    assert left == 0


# ------------------------------------------------------------------ who may see what
async def test_only_hr_director_and_super_admin_see_payroll_overviews(client, hr, hod, director, superadmin):
    run = await new_run(client, hr)
    _, _, staff_h = await make_person(client, hr)
    paths = ["/payroll/runs", f"/payroll/runs/{run['id']}", f"/payroll/runs/{run['id']}/payslips", "/payroll/summary",
             "/payroll/scales", "/payroll/components"]
    for who in (staff_h, hod):
        for p in paths:
            assert (await client.get(f"{API}{p}", headers=who)).status_code == 403, p
        assert (await client.get(f"{API}/payroll/runs/{run['id']}/export", headers=who)).status_code == 403
    for who in (hr, director, superadmin):
        for p in paths:
            assert (await client.get(f"{API}{p}", headers=who)).status_code == 200, p
    assert (await client.get(f"{API}/payroll/runs")).status_code == 401


async def test_payslips_are_hidden_from_the_owner_until_the_run_is_paid(client, hr, director, sarah):
    run = await new_run(client, hr)
    s = await payslip_for(client, hr, run["id"], "YAB-MED-042")
    assert (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=sarah)).status_code == 404
    mine = (await client.get(f"{API}/payroll/payslips/me", headers=sarah)).json()["items"]
    assert s["id"] not in [p["id"] for p in mine]

    await client.post(f"{API}/payroll/runs/{run['id']}/approve", headers=director, json={"acknowledge_exceptions": True})
    assert (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=sarah)).status_code == 404  # approved is not paid
    await client.post(f"{API}/payroll/runs/{run['id']}/pay", headers=hr)

    got = await client.get(f"{API}/payroll/payslips/{s['id']}", headers=sarah)
    assert got.status_code == 200 and got.json()["net"] == s["net"] and got.json()["run_status"] == "Paid"
    mine = (await client.get(f"{API}/payroll/payslips/me", headers=sarah)).json()["items"]
    assert s["id"] in [p["id"] for p in mine]
    assert [p["period"] for p in mine] == sorted((p["period"] for p in mine), reverse=True)


async def test_the_seeded_paid_run_gives_the_demo_staff_member_a_payslip(client, sarah):
    mine = (await client.get(f"{API}/payroll/payslips/me", headers=sarah)).json()["items"]
    assert mine and mine[0]["file_number"] == "YAB-MED-042" and mine[0]["net"] > 0
    year = mine[-1]["period"][:4]
    assert all(p["period"].startswith(year) for p in (await client.get(
        f"{API}/payroll/payslips/me", headers=sarah, params={"year": int(year)})).json()["items"])


async def test_nobody_can_read_someone_elses_payslip_not_even_an_hod(client, hr, hod, director, sarah):
    run = await new_run(client, hr)
    await approve_and_pay(client, hr, director, run["id"])
    s = await payslip_for(client, hr, run["id"], "YAB-MED-042")  # Sarah's
    _, _, other = await make_person(client, hr)
    assert (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=other)).status_code == 404
    assert (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hod)).status_code == 404  # her own HOD
    own_hod = await payslip_for(client, hr, run["id"], "YAB-HOD-007")
    assert (await client.get(f"{API}/payroll/payslips/{own_hod['id']}", headers=hod)).status_code == 200
    import uuid
    assert (await client.get(f"{API}/payroll/payslips/{uuid.uuid4()}", headers=other)).status_code == 404


async def test_hr_viewing_a_payslip_is_audited_but_viewing_your_own_is_not(client, hr, director, sarah):
    run = await new_run(client, hr)
    await approve_and_pay(client, hr, director, run["id"])
    s = await payslip_for(client, hr, run["id"], "YAB-MED-042")
    await client.get(f"{API}/payroll/payslips/{s['id']}", headers=sarah)
    trail = lambda: client.get(f"{API}/staff/YAB-MED-042/audit", headers=hr, params={"size": 100})  # noqa: E731
    before = [a for a in (await trail()).json()["items"] if a["action"] == "payslip_viewed"]
    await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hr)
    after = [a for a in (await trail()).json()["items"] if a["action"] == "payslip_viewed"]
    assert len(after) == len(before) + 1 and after[0]["by"] == "hr@hospital.test"
    assert "3045678901" not in str(after) and "net" not in str(after)  # the trail records the event, not the figures


async def test_payroll_actions_are_audited(client, hr, director):
    import uuid
    run = await new_run(client, hr)
    await approve_and_pay(client, hr, director, run["id"])
    await client.get(f"{API}/payroll/runs/{run['id']}/export", headers=director)
    async with SessionLocal() as s:
        rows = (await s.scalars(select(AuditLog).where(AuditLog.entity_id == uuid.UUID(run["id"])))).all()
    by = {r.action: r.user_email for r in rows}
    assert by == {"payroll_run_created": "hr@hospital.test", "payroll_run_approved": "director@hospital.test",
                  "payroll_run_paid": "hr@hospital.test", "payroll_exported": "director@hospital.test"}
    assert "3045678901" not in str([r.after for r in rows])


# ------------------------------------------------------------------ pay structure
async def test_pay_structure_is_editable_by_hr_only(client, hr, hod, director, superadmin):
    _, _, staff_h = await make_person(client, hr)
    comp = {"code": "NOPE", "name": "Nope", "kind": "earning", "method": "fixed", "amount": 100}
    for who in (staff_h, hod, director, superadmin):
        assert (await client.post(f"{API}/payroll/components", headers=who, json=comp)).status_code == 403
        assert (await client.put(f"{API}/payroll/scales", headers=who,
                                 json={"entries": [{"level": 1, "step": 1, "basic": 1}]})).status_code == 403
        assert (await client.post(f"{API}/payroll/adjustments", headers=who, json={
            "staff": "YAB-MED-042", "kind": "earning", "name": "x", "amount": 1, "start_period": "2001-01"})).status_code == 403
        assert (await client.post(f"{API}/payroll/runs", headers=who, json={"year": 2001, "month": 1})).status_code == 403
    for who in (staff_h, hod, director, superadmin):  # adjustments are HR-only even to read
        assert (await client.get(f"{API}/payroll/adjustments", headers=who)).status_code == 403


@pytest.mark.parametrize("body", [
    {"code": "BAD1", "name": "x", "kind": "earning", "method": "fixed"},                                  # no amount
    {"code": "BAD2", "name": "x", "kind": "earning", "method": "fixed", "amount": 1, "percent": 5},       # both
    {"code": "BAD3", "name": "x", "kind": "earning", "method": "percent_of_basic"},                       # no percent
    {"code": "BAD4", "name": "x", "kind": "earning", "method": "percent_of_gross", "percent": 5},         # gross-based earning
    {"code": "BAD5", "name": "x", "kind": "deduction", "method": "percent_of_basic", "percent": 101},
    {"code": "BAD6", "name": "x", "kind": "deduction", "method": "fixed", "amount": -1},
    {"code": "bad7", "name": "x", "kind": "deduction", "method": "fixed", "amount": 1},                   # code style
    {"code": "BAD8", "name": "x", "kind": "earning", "method": "fixed", "amount": 1, "min_level": 9, "max_level": 3},
    {"code": "BAD9", "name": "x", "kind": "bonus", "method": "fixed", "amount": 1},
])
async def test_invalid_components_are_rejected(client, hr, body):
    assert (await client.post(f"{API}/payroll/components", headers=hr, json=body)).status_code == 422


async def test_a_new_component_changes_the_next_run_only_for_the_levels_it_covers(client, hr):
    run0 = await new_run(client, hr)
    s0 = await payslip_for(client, hr, run0["id"], "YAB-MED-042")
    level = int(s0["grade"].split()[-1])
    comp = (await client.post(f"{API}/payroll/components", headers=hr, json={
        "code": "TESTBONUS", "name": "Test bonus", "kind": "earning", "method": "fixed", "amount": 1000.5,
        "min_level": level, "max_level": level})).json()
    try:
        assert (await client.post(f"{API}/payroll/components", headers=hr, json={
            "code": "TESTBONUS", "name": "again", "kind": "earning", "method": "fixed", "amount": 1})).status_code == 409
        run1 = await new_run(client, hr)
        s1 = await payslip_for(client, hr, run1["id"], "YAB-MED-042")
        d1 = (await client.get(f"{API}/payroll/payslips/{s1['id']}", headers=hr)).json()
        assert any(x["code"] == "TESTBONUS" and x["amount"] == 1000.5 for x in d1["earnings"])
        # someone on a different level does not get it
        other = next(p for p in (await client.get(f"{API}/payroll/runs/{run1['id']}/payslips", headers=hr,
                                                  params={"size": 100})).json()["items"]
                     if p["grade"] and int(p["grade"].split()[-1]) != level)
        od = (await client.get(f"{API}/payroll/payslips/{other['id']}", headers=hr)).json()
        assert all(x["code"] != "TESTBONUS" for x in od["earnings"])
        # The earlier draft is untouched until someone recalculates it.
        assert (await client.get(f"{API}/payroll/payslips/{s0['id']}", headers=hr)).json()["gross"] == s0["gross"]
    finally:
        body = {"code": "TESTBONUS", "name": "Test bonus", "kind": "earning", "method": "fixed", "amount": 1000.5,
                "min_level": level, "max_level": level, "is_active": False}
        assert (await client.put(f"{API}/payroll/components/{comp['id']}", headers=hr, json=body)).status_code == 200
    run2 = await new_run(client, hr)
    s2 = await payslip_for(client, hr, run2["id"], "YAB-MED-042")
    assert s2["gross"] == s0["gross"]  # switched off again


async def test_changing_the_scale_leaves_existing_payslips_alone_until_recalculated(client, hr):
    run = await new_run(client, hr)
    s = await payslip_for(client, hr, run["id"], "YAB-MED-042")
    detail = (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hr)).json()
    level, step, basic = int(s["grade"].split()[-1]), s["step"], detail["basic"]
    try:
        put = await client.put(f"{API}/payroll/scales", headers=hr,
                               json={"entries": [{"level": level, "step": step, "basic": basic + 10_000}]})
        assert put.status_code == 200
        assert any(e["level"] == level and e["step"] == step and e["basic"] == basic + 10_000 for e in put.json())
        unchanged = (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hr)).json()
        assert unchanged["basic"] == basic                       # snapshot
        assert (await client.post(f"{API}/payroll/runs/{run['id']}/recalculate", headers=hr)).status_code == 200
        s2 = await payslip_for(client, hr, run["id"], "YAB-MED-042")
        assert (await client.get(f"{API}/payroll/payslips/{s2['id']}", headers=hr)).json()["basic"] == basic + 10_000
    finally:
        await client.put(f"{API}/payroll/scales", headers=hr, json={"entries": [{"level": level, "step": step, "basic": basic}]})


async def test_scale_update_validation(client, hr):
    for entries in ([{"level": 99, "step": 1, "basic": 5}], [{"level": 1, "step": 0, "basic": 5}],
                    [{"level": 1, "step": 1, "basic": 0}], [{"level": 1, "step": 1, "basic": 5}] * 2, []):
        assert (await client.put(f"{API}/payroll/scales", headers=hr, json={"entries": entries})).status_code == 422


# ------------------------------------------------------------------ individual adjustments
async def test_adjustments_apply_only_in_their_months_and_can_be_cancelled(client, hr):
    y, m = next_period()
    y2, m2 = next_period()
    p1, p2 = f"{y}-{m:02d}", f"{y2}-{m2:02d}"
    adj = await client.post(f"{API}/payroll/adjustments", headers=hr, json={
        "staff": "YAB-MED-042", "kind": "earning", "name": "Call duty", "amount": 7500, "start_period": p1,
        "end_period": p1, "reason": "Extra shifts"})
    assert adj.status_code == 201 and adj.json()["active"] and adj.json()["start_period"] == p1
    r1 = await client.post(f"{API}/payroll/runs", headers=hr, json={"year": y, "month": m})
    r2 = await client.post(f"{API}/payroll/runs", headers=hr, json={"year": y2, "month": m2})
    names = {}
    for key, r in (("in", r1), ("out", r2)):
        s = await payslip_for(client, hr, r.json()["id"], "YAB-MED-042")
        d = (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hr)).json()
        names[key] = [x["name"] for x in d["earnings"]]
    assert "Call duty" in names["in"] and "Call duty" not in names["out"]

    assert (await client.delete(f"{API}/payroll/adjustments/{adj.json()['id']}", headers=hr)).status_code == 204
    assert (await client.delete(f"{API}/payroll/adjustments/{adj.json()['id']}", headers=hr)).status_code == 404
    assert (await client.post(f"{API}/payroll/runs/{r1.json()['id']}/recalculate", headers=hr)).status_code == 200
    s = await payslip_for(client, hr, r1.json()["id"], "YAB-MED-042")
    d = (await client.get(f"{API}/payroll/payslips/{s['id']}", headers=hr)).json()
    assert "Call duty" not in [x["name"] for x in d["earnings"]]
    listed = (await client.get(f"{API}/payroll/adjustments", headers=hr, params={"staff": "YAB-MED-042"})).json()
    assert adj.json()["id"] not in [a["id"] for a in listed]
    allrows = (await client.get(f"{API}/payroll/adjustments", headers=hr,
                                params={"staff": "YAB-MED-042", "include_cancelled": True})).json()
    assert any(a["id"] == adj.json()["id"] and a["active"] is False for a in allrows)


async def test_deductions_larger_than_gross_are_flagged(client, hr):
    y, m = next_period()
    p = f"{y}-{m:02d}"
    a = await client.post(f"{API}/payroll/adjustments", headers=hr, json={
        "staff": "YAB-MED-042", "kind": "deduction", "name": "Huge recovery", "amount": 900_000_000,
        "start_period": p, "end_period": p})
    run = await client.post(f"{API}/payroll/runs", headers=hr, json={"year": y, "month": m})
    s = await payslip_for(client, hr, run.json()["id"], "YAB-MED-042")
    assert s["exception"] == "Deductions exceed gross pay" and s["net"] < 0
    await client.delete(f"{API}/payroll/adjustments/{a.json()['id']}", headers=hr)


async def test_adjustment_validation(client, hr):
    base = {"staff": "YAB-MED-042", "kind": "earning", "name": "x", "amount": 10, "start_period": "2001-01"}
    for bad in ({"amount": 0}, {"amount": -5}, {"start_period": "2001-13"}, {"start_period": "01-2001"},
                {"end_period": "2000-12"}, {"kind": "gift"}, {"name": ""}):
        assert (await client.post(f"{API}/payroll/adjustments", headers=hr, json={**base, **bad})).status_code == 422
    assert (await client.post(f"{API}/payroll/adjustments", headers=hr, json={**base, "staff": "NOBODY-1"})).status_code == 404


# ------------------------------------------------------------------ export and summary
async def test_export_is_csv_for_hr_and_director_only(client, hr, director, superadmin):
    run = await new_run(client, hr)
    r = await client.get(f"{API}/payroll/runs/{run['id']}/export", headers=hr)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert r.headers["content-disposition"].startswith("attachment;") and ".csv" in r.headers["content-disposition"]
    lines = r.text.strip().splitlines()
    assert lines[0].startswith("File number,Name,Department") and len(lines) == run["staff_count"] + 1
    assert (await client.get(f"{API}/payroll/runs/{run['id']}/export", headers=director)).status_code == 200
    assert (await client.get(f"{API}/payroll/runs/{run['id']}/export", headers=superadmin)).status_code == 403


def test_csv_cells_cannot_run_as_spreadsheet_formulas():
    for evil in ("=HYPERLINK(\"http://x\")", "+1+1", "-2", "@SUM(A1)", "\tcmd"):
        assert _safe(evil).startswith("'")
    assert _safe("Ngozi") == "Ngozi" and _safe(None) == "" and _safe(12.5) == "12.5"


async def test_summary_matches_the_latest_run(client, hr):
    run = await new_run(client, hr)  # periods only increase within the suite, but the seeded run is more recent
    s = (await client.get(f"{API}/payroll/summary", headers=hr)).json()
    latest = s["latest_run"]
    assert latest is not None and latest["staff_count"] == sum(d["staff"] for d in s["by_department"])
    assert round(sum(d["net"] for d in s["by_department"]), 2) == latest["total_net"]
    assert round(sum(d["gross"] for d in s["by_department"]), 2) == latest["total_gross"]
    assert run["id"]  # (the seeded last-month run is the latest; ours is an older period)
