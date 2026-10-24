import uuid
from typing import Any

from fastapi import Request
from fastapi.encoders import jsonable_encoder
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AuditLog, User

from .security import utcnow

# Never written to the audit trail, even as "before/after" values.
_REDACT = {"password", "password_hash", "token", "totp_secret", "account_number", "account_number_enc", "bvn", "bvn_enc"}


def _clean(data: dict[str, Any] | None):
    if data is None:
        return None
    safe = jsonable_encoder(data)
    return {k: ("[redacted]" if k in _REDACT else v) for k, v in safe.items()}


def diff(before: dict[str, Any], after: dict[str, Any]) -> tuple[dict, dict]:
    """Keep only the fields that actually changed."""
    keys = [k for k in after if before.get(k) != after.get(k)]
    return {k: before.get(k) for k in keys}, {k: after.get(k) for k in keys}


def record(
    session: AsyncSession,
    *,
    user: User | None,
    action: str,
    entity: str,
    entity_id: uuid.UUID | None = None,
    subject_staff_id: uuid.UUID | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
    request: Request | None = None,
    user_email: str | None = None,
) -> None:
    """Adds an audit row to the caller's transaction, so it commits or rolls back with the change itself."""
    session.add(
        AuditLog(
            at=utcnow(),
            user_id=user.id if user else None,
            user_email=user.email if user else user_email,
            action=action,
            entity=entity,
            entity_id=entity_id,
            subject_staff_id=subject_staff_id,
            before=_clean(before),
            after=_clean(after),
            ip=(request.client.host if request and request.client else None),
        )
    )
