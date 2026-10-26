# PMS API: Federal Neuro-Psychiatric Hospital, Yaba

FastAPI + PostgreSQL (Neon) backend for the Personnel Management System. Modular monolith: one deployable, one database, separate modules.

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
python -m app.seed --reset        # SQLite file dev.db + mock data
uvicorn app.main:app --reload     # http://127.0.0.1:8000/docs
pytest                            # 88 tests
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
app/modules/   auth, users, org, staff, leave, documents, reports   (router.py [+ schemas.py, service.py])
alembic/       migrations      tests/   pytest suite      app/seed.py   mock data
```

## API (all under `/api/v1`; interactive docs at `/docs` outside production)

| Area | Endpoints |
|---|---|
| Auth | `POST /auth/login`, `/auth/refresh`, `/auth/logout`, `/auth/logout-all`, `/auth/change-password`; `GET /auth/me` |
| Users (HR, super admin) | `POST/GET /users`, `PATCH /users/{id}`, `POST /users/{id}/reset-password`, `GET /users/summary` |
| Org | `GET/POST /departments`, `GET /grades` |
| Staff | `GET /staff` (search, filters, paging), `POST /staff` (also creates the login), `POST /staff/{id}/account`, `GET/PATCH/DELETE /staff/{id or file number}`, `GET/PATCH /staff/me`, `GET /staff/lookup`, `GET /staff/{id}/audit`, education / dependants / emergency-contact / license / bank sub-resources |
| Leave | `GET /leave/types`, `/leave/balance`; `POST/GET /leave/requests` (`?awaiting_me=true`), `GET /leave/requests/{id}`, `POST …/decision`, `POST …/cancel` |
| Documents | `POST /staff/{id}/documents` (start an upload), `POST /documents/{id}/complete`, `GET /staff/{id}/documents`, `GET /documents/{id}`, `GET /documents/{id}/download`, `POST /documents/{id}/verify`, `DELETE /documents/{id}`, `GET /documents/expiring` |
| Reports | `GET /reports/dashboard` (role-aware KPIs and chart data) |

## Documents and Cloudflare R2

Files live in a **private** R2 bucket; the database holds only metadata. The browser uploads and downloads directly through
short-lived signed links, so file bytes never pass through this server.

1. `POST /staff/{id}/documents` with `doc_type`, `filename`, `content_type`, `size` returns the document and a signed `upload` target (`url`, `method`, `headers`, valid 5 minutes, bound to that exact type and size).
2. The browser `PUT`s the file to `upload.url` with exactly `upload.headers`. The file lands in a random quarantine key.
3. `POST /documents/{id}/complete` copies it to its final key and validates the copy: size matches, first bytes match PDF/JPEG/PNG. Failures are deleted and recorded. Only then does it become `Pending`.
4. `GET /documents/{id}/download` returns a signed link valid 60 seconds, always served as an attachment. Each link issued is audited.

**Rules (defaults, confirm with the hospital):** PDF, JPEG, PNG only, up to 10 MB. Owner and HR may download; HOD, director and super admin see details but not files. HR verifies or rejects, never their own or self-uploaded files. The owner may delete only Pending or Rejected documents; deletion is soft and the stored file is kept.

**Setup:** set `STORAGE_BACKEND=r2` and `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` (the bucket name). The bucket needs a CORS policy allowing `PUT` with the `Content-Type` header from your frontend's origin. With the default `STORAGE_BACKEND=local`, files go to `./.storage` and the same signed-link flow is served by the app itself (development only; disabled in production), so no R2 account is needed to develop.

Not yet done: virus scanning (recommended before wide rollout; a ClamAV service is the usual route), backups of the bucket, and an automatic cleanup job for abandoned uploads (stale ones are currently swept when the same person starts a new upload).

## Accounts and first sign-in

When HR creates a staff record (`POST /staff`) the staff member's login is created in the same transaction, so either both
exist or neither does. The login name is the staff member's **email** and the role is `staff`. Every account gets its **own random
one-time password** (for example `k7mq-h2rx-9pwd`: lowercase letters and digits, no look-alike characters, easy to read out or
write down). It is stored hashed with Argon2 and returned **once**, in `account.temporary_password`, so HR can pass it to the
new user. HR cannot choose passwords. The response includes an `account_note` instead when no login was made (no email, or
`create_account: false`); add the email later and call `POST /staff/{id}/account`.

The one-time password is never shown again and never written to the audit log. API responses carry `Cache-Control: no-store`
so browsers and proxies do not keep it. If HR loses it, `POST /users/{id}/reset-password` issues a new one the same way.

First sign-in: `POST /auth/login` returns `must_change_password: true`. Until the person has chosen their own password, **every
other endpoint answers 403** with `{"detail": {"code": "password_change_required", ...}}`; only `/auth/me`, `/auth/logout`,
`/auth/logout-all` and `/auth/change-password` work. The frontend should send the user to a change-password screen when it sees
that flag or code. `POST /auth/change-password` signs out every other device and returns fresh tokens, so no second sign-in is needed.
The new password must differ from the current one and meet the minimum length.

An unused one-time password stops working after `INITIAL_PASSWORD_DAYS` (default 14). `GET /users?awaiting_first_login=true`
lists the people who have not signed in yet, so HR can chase them or reset their password.

| Setting | Meaning |
|---|---|
| `INITIAL_PASSWORD_DAYS` | how long an unused one-time password works (default 14) |
| `AUTO_CREATE_ACCOUNTS` | turn automatic account creation off entirely |

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

Payroll and payslips, promotions, queries, performance appraisals, notifications (email/SMS), exports (PDF/Excel/CSV), TOTP two-factor, superadmin system tools (backup, config), and the frontend API client.
