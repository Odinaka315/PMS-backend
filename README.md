# PMS API: Federal Neuro-Psychiatric Hospital, Yaba

FastAPI + PostgreSQL (Neon) backend for the Personnel Management System. Modular monolith: one deployable, one database, separate modules.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m app.seed --reset        # SQLite file dev.db + mock data
uvicorn app.main:app --reload     # http://127.0.0.1:8000/docs
pytest                            # 32 tests
```

Demo logins (same as the frontend): `staff|hod|hr|director|superadmin@hospital.test`, password `hospital123`.
**All data is mock.** The seeder refuses to run when `ENVIRONMENT=production`.

## Using Neon

1. Create the project and copy both connection strings from the Neon dashboard.
2. `.env`: `DATABASE_URL=<pooled string>` and `MIGRATION_DATABASE_URL=<direct string>`. The `postgresql://…?sslmode=require` format works as-is.
3. `alembic upgrade head`, then optionally `python -m app.seed` for mock data.
4. Free-tier scale-to-zero means a slow first request; use a paid plan before go-live.

Tested against PostgreSQL 16 as well as SQLite.

## Deploy to Render

`render.yaml` is included. Set `DATABASE_URL`, `MIGRATION_DATABASE_URL`, `CORS_ORIGINS` and `FIELD_ENCRYPTION_KEY` in the dashboard (`SECRET_KEY` is generated). Migrations run on start. The app refuses to boot in production without strong secrets.

**Back up `FIELD_ENCRYPTION_KEY`.** Bank account numbers and BVNs are encrypted with it; losing it makes them unrecoverable.

## Layout

```
app/core/      config, db, security (argon2, JWT, field encryption), deps (RBAC + data scope), audit
app/models/    SQLAlchemy models
app/modules/   auth, users, org, staff, leave, reports   (router.py [+ schemas.py, service.py])
alembic/       migrations      tests/   pytest suite      app/seed.py   mock data
```

## API (all under `/api/v1`; interactive docs at `/docs` outside production)

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/logout-all`, `/auth/change-password`; `GET /auth/me` |
| Users (HR, super admin) | `POST/GET /users`, `PATCH /users/{id}`, `POST /users/{id}/reset-password`, `GET /users/summary` |
| Org | `GET/POST /departments`, `GET /grades` |
| Staff | `GET /staff` (search, filters, paging), `POST /staff`, `GET/PATCH/DELETE /staff/{id or file number}`, `GET/PATCH /staff/me`, `GET /staff/lookup`, `GET /staff/{id}/audit`, education / dependants / emergency-contact / license / bank sub-resources |
| Leave | `GET /leave/types`, `/leave/balance`; `POST/GET /leave/requests` (`?awaiting_me=true`), `GET /leave/requests/{id}`, `POST …/decision`, `POST …/cancel` |
| Reports | `GET /reports/dashboard` (role-aware KPIs and chart data) |

## Security model

- **Roles:** staff, hod, hr, director, superadmin. **Data scope** is enforced in the query layer: staff see themselves, an HOD sees their department, HR/director/superadmin see everyone. Out-of-scope records return 404, not 403.
- Accounts are created only by HR or super admin. HR can create staff/HOD accounts; director, HR and super admin accounts need a super admin.
- Argon2 passwords, 15-minute access tokens, rotating single-use refresh tokens (replay revokes all sessions), lockout after 5 failed logins.
- Append-only audit log for logins, changes and views of other people's files. Secrets (bank, BVN, passwords) are never written to it.
- Bank/BVN encrypted at rest; shown only to the owner and HR; BVN always masked.

## Assumptions to confirm with the hospital (all easy to change)

- **Leave:** entitlements (annual 15, sick 10, compassionate 5) are placeholders from the frontend mock; days are counted as calendar days; the approval chain HOD → HR → Director is configurable per leave type in `leave_workflow_steps`; an approver's own request skips their level.
- **Retirement:** 60 years of age or 35 years of service, whichever is first (`RETIREMENT_AGE`, `RETIREMENT_SERVICE_YEARS`).
- **Departments, grade levels (CONHESS 1–15), designations** are mock; replace via the API or seed.
- **Promotion rules, payroll/CONHESS salary table:** not built yet, waiting on real rules.

## Not built yet

Documents (upload and verification), payroll and payslips, promotions, queries, performance appraisals, notifications (email/SMS), exports (PDF/Excel/CSV), TOTP two-factor, superadmin system tools (backup, config), and the frontend API client.
