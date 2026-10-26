from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.core.config import settings
from app.core.db import engine
from app.modules.auth.router import router as auth_router
from app.modules.documents.dev_storage import router as dev_storage_router
from app.modules.documents.router import router as documents_router
from app.modules.leave.router import router as leave_router
from app.modules.org.router import router as org_router
from app.modules.reports.router import router as reports_router
from app.modules.staff.router import router as staff_router
from app.modules.users.router import router as users_router

app = FastAPI(
    title="Federal Neuro-Psychiatric Hospital, Yaba: Personnel Management System API",
    version="0.1.0",
    docs_url="/docs" if settings.environment != "production" else None,
    redoc_url=None,
    openapi_url="/openapi.json" if settings.environment != "production" else None,
)

app.add_middleware(
    CORSMiddleware, allow_origins=settings.cors_origins, allow_credentials=False,
    allow_methods=["*"], allow_headers=["*"],
)

API = "/api/v1"
for r in (auth_router, users_router, org_router, staff_router, leave_router, documents_router, reports_router):
    app.include_router(r, prefix=API)
if settings.storage_backend == "local" and settings.environment != "production":
    app.include_router(dev_storage_router, prefix=API)  # stands in for R2 during development


@app.get("/health", tags=["system"])
async def health():
    """Liveness + database reachability (used by Render's health check)."""
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    return {"status": "ok"}
