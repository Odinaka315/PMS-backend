"""Staff documents.

Who can do what (defaults; confirm with the hospital):
  * Upload:           the staff member (own) and HR (anyone)
  * See details:      anyone who can see that staff record (self, HOD of the department, HR, director, super admin)
  * Download the file: ONLY the owner and HR
  * Verify / reject:  HR only, never for a file they uploaded or that belongs to them
  * Delete:           HR any; the owner only while it is Pending or Rejected (a Verified file stays on record)
Deletion is a soft delete: the record and the stored object are kept.
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.config import settings
from app.core.db import get_session
from app.core.deps import can_access_staff, get_accessible_staff, get_current_user, get_staff_or_404, require_roles
from app.core.roles import Role
from app.core.security import as_utc, utcnow
from app.core.storage import Storage, get_storage
from app.models import Document, Staff, User

from . import rules
from .schemas import DocumentInit, DocumentOut, DownloadOut, ExpiringOut, UploadInitOut, UploadTarget, VerifyIn

router = APIRouter(tags=["documents"])

VISIBLE = ("Pending", "Verified", "Rejected")  # 'Awaiting Upload' and 'Failed' never appear in lists
STALE_UPLOAD = timedelta(hours=1)
MAX_OPEN_UPLOADS = 10


# ---------------------------------------------------------------- permissions
def _is_owner(user: User, doc: Document) -> bool:
    return user.staff_id is not None and user.staff_id == doc.staff_id


def _can_download(user: User, doc: Document) -> bool:
    return user.role == Role.HR or _is_owner(user, doc)


def _can_verify(user: User, doc: Document) -> bool:
    return (user.role == Role.HR and doc.status == "Pending" and not _is_owner(user, doc)
            and doc.uploaded_by != user.id)


def _can_delete(user: User, doc: Document) -> bool:
    return user.role == Role.HR or (_is_owner(user, doc) and doc.status in ("Pending", "Rejected"))


def to_out(user: User, d: Document) -> DocumentOut:
    return DocumentOut(
        id=d.id, staff_id=d.staff_id, name=d.name, type=d.doc_type, status=d.status, expiry=d.expiry_date,
        original_filename=d.original_filename, content_type=d.content_type, size_bytes=d.size_bytes,
        uploaded_at=d.created_at, verified_by=d.verified_by_email, verified_at=d.verified_at,
        rejection_reason=d.rejection_reason,
        can_download=_can_download(user, d) and d.status in VISIBLE,
        can_verify=_can_verify(user, d), can_delete=_can_delete(user, d) and d.status in VISIBLE,
    )


async def _load(session: AsyncSession, doc_id: uuid.UUID) -> Document:
    doc = await session.scalar(select(Document).where(Document.id == doc_id, Document.deleted_at.is_(None)))
    if doc is None:
        raise HTTPException(404, "Document not found")
    return doc


async def _load_visible(session: AsyncSession, user: User, doc_id: uuid.UUID) -> Document:
    """A document the caller may at least see the details of. Out-of-scope documents look like they don't exist."""
    doc = await _load(session, doc_id)
    staff = await session.get(Staff, doc.staff_id)
    if doc.status not in VISIBLE or staff is None or staff.deleted_at or not can_access_staff(user, staff):
        raise HTTPException(404, "Document not found")
    return doc


# ---------------------------------------------------------------- upload (3 steps: init -> browser PUT -> complete)
@router.post("/staff/{ident}/documents", response_model=UploadInitOut, status_code=201)
async def init_upload(ident: str, body: DocumentInit, request: Request, user: User = Depends(get_current_user),
                      session: AsyncSession = Depends(get_session), storage: Storage = Depends(get_storage)):
    """Step 1: register the upload and get a short-lived signed link. The file then goes straight to storage."""
    staff = await get_staff_or_404(session, ident)
    if not (user.role == Role.HR or user.staff_id == staff.id):
        raise HTTPException(403, "You can only upload documents to your own record")

    filename = rules.clean_filename(body.filename)
    problem = rules.check_declared(filename, body.content_type, body.size, settings.max_upload_bytes)
    if problem:
        raise HTTPException(422, problem)

    # Housekeeping: abandon uploads that never completed, then cap what one person can have open or stored.
    stale = (await session.scalars(select(Document).where(
        Document.staff_id == staff.id, Document.status == "Awaiting Upload",
        Document.created_at < utcnow() - STALE_UPLOAD))).all()
    for d in stale:
        try:
            await storage.delete(d.object_key)
        except Exception:  # best effort; an orphaned quarantine object is harmless
            pass
        await session.delete(d)
    open_uploads = await session.scalar(select(func.count()).select_from(Document).where(
        Document.staff_id == staff.id, Document.status == "Awaiting Upload")) or 0
    if open_uploads >= MAX_OPEN_UPLOADS:
        raise HTTPException(429, "Too many unfinished uploads; finish or wait for them to expire")
    stored = await session.scalar(select(func.count()).select_from(Document).where(
        Document.staff_id == staff.id, Document.deleted_at.is_(None), Document.status.in_(VISIBLE))) or 0
    if stored >= settings.max_documents_per_staff:
        raise HTTPException(409, "Document limit reached for this staff member")

    doc_id = uuid.uuid4()
    doc = Document(
        id=doc_id, staff_id=staff.id, doc_type=body.doc_type, name=(body.name or filename)[:200],
        original_filename=filename, content_type=body.content_type, declared_size=body.size,
        object_key=f"quarantine/{staff.id}/{doc_id}",  # random; the real filename is never part of the key
        status="Awaiting Upload", expiry_date=body.expiry_date, uploaded_by=user.id,
    )
    session.add(doc)
    presigned = await storage.presign_upload(doc.object_key, doc.content_type, doc.declared_size,
                                             settings.upload_url_seconds)
    await session.flush()
    await session.commit()
    return UploadInitOut(
        document=to_out(user, doc),
        upload=UploadTarget(url=presigned.url, method=presigned.method, headers=presigned.headers,
                            expires_in=settings.upload_url_seconds),
    )


@router.post("/documents/{doc_id}/complete", response_model=DocumentOut)
async def complete_upload(doc_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session), storage: Storage = Depends(get_storage)):
    """Step 3: after the browser has uploaded the file, check it really is what was declared."""
    doc = await session.scalar(select(Document).where(Document.id == doc_id, Document.deleted_at.is_(None)))
    if doc is None or doc.uploaded_by != user.id:
        raise HTTPException(404, "Document not found")
    if doc.status != "Awaiting Upload":
        raise HTTPException(409, "This upload has already been completed")

    quarantine = doc.object_key
    if await storage.head(quarantine) is None:
        raise HTTPException(409, "No file has been received yet; upload it first")

    # Copy to its final home FIRST and validate the copy. The browser's signed link only reaches the quarantine key,
    # so it cannot swap the file between validation and use.
    final_key = f"documents/{doc.staff_id}/{doc.id}"
    await storage.copy(quarantine, final_key)
    info = await storage.head(final_key)
    problem = None
    if info is None:
        problem = "The file could not be read"
    elif info.size != doc.declared_size:
        problem = "The uploaded file's size does not match what was declared"
    elif info.size > settings.max_upload_bytes:
        problem = "The file is too large"
    elif not rules.matches_magic(doc.content_type, await storage.read_start(final_key, rules.MAGIC_BYTES_NEEDED)):
        problem = "The file's contents do not match its declared type"

    await storage.delete(quarantine)
    if problem:
        await storage.delete(final_key)
        doc.status = "Failed"
        audit.record(session, user=user, action="document_upload_rejected", entity="document", entity_id=doc.id,
                     subject_staff_id=doc.staff_id, after={"reason": problem, "type": doc.doc_type}, request=request)
        await session.commit()  # keep the record of the failure, then report it
        raise HTTPException(422, problem)

    doc.object_key, doc.size_bytes, doc.status = final_key, info.size, "Pending"
    audit.record(session, user=user, action="document_uploaded", entity="document", entity_id=doc.id,
                 subject_staff_id=doc.staff_id,
                 after={"type": doc.doc_type, "name": doc.name, "size_bytes": info.size}, request=request)
    await session.commit()
    return to_out(user, doc)


# ---------------------------------------------------------------- read
@router.get("/staff/{ident}/documents", response_model=list[DocumentOut])
async def list_documents(ident: str, status: str | None = Query(None, pattern=r"^(Pending|Verified|Rejected)$"),
                         type: str | None = None, q: str | None = None, user: User = Depends(get_current_user),
                         session: AsyncSession = Depends(get_session)):
    staff = await get_accessible_staff(session, user, ident)
    stmt = select(Document).where(Document.staff_id == staff.id, Document.deleted_at.is_(None),
                                  Document.status.in_(VISIBLE))
    if status:
        stmt = stmt.where(Document.status == status)
    if type:
        stmt = stmt.where(Document.doc_type == type)
    if q:
        stmt = stmt.where(func.lower(Document.name).like(f"%{q.lower()}%"))
    rows = (await session.scalars(stmt.order_by(Document.created_at.desc()))).all()
    return [to_out(user, d) for d in rows]


@router.get("/documents/expiring", response_model=list[ExpiringOut])
async def expiring_documents(days: int = Query(90, ge=1, le=730),
                             _: User = Depends(require_roles(Role.HR, Role.DIRECTOR)),
                             session: AsyncSession = Depends(get_session)):
    """Licences and certificates expiring soon (or already expired), soonest first."""
    cutoff = date.today() + timedelta(days=days)
    rows = (await session.execute(
        select(Document, Staff).join(Staff, Staff.id == Document.staff_id)
        .where(Document.deleted_at.is_(None), Staff.deleted_at.is_(None), Document.status.in_(("Pending", "Verified")),
               Document.expiry_date.is_not(None), Document.expiry_date <= cutoff)
        .order_by(Document.expiry_date))).unique().all()
    return [ExpiringOut(id=d.id, staff_id=s.id, file_number=s.file_number, staff_name=s.full_name, name=d.name,
                        type=d.doc_type, status=d.status, expiry=d.expiry_date,
                        days_left=(d.expiry_date - date.today()).days) for d, s in rows]


@router.get("/documents/{doc_id}", response_model=DocumentOut)
async def get_document(doc_id: uuid.UUID, user: User = Depends(get_current_user),
                       session: AsyncSession = Depends(get_session)):
    return to_out(user, await _load_visible(session, user, doc_id))


@router.get("/documents/{doc_id}/download", response_model=DownloadOut)
async def download_document(doc_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                            session: AsyncSession = Depends(get_session), storage: Storage = Depends(get_storage)):
    """Returns a signed link that works for about a minute. Issuing it is audited."""
    doc = await _load_visible(session, user, doc_id)
    if not _can_download(user, doc):
        raise HTTPException(403, "Only the document's owner and HR can download the file")
    url = await storage.presign_download(doc.object_key, doc.original_filename, doc.content_type,
                                         settings.download_url_seconds)
    audit.record(session, user=user, action="document_downloaded", entity="document", entity_id=doc.id,
                 subject_staff_id=doc.staff_id, after={"type": doc.doc_type, "name": doc.name}, request=request)
    await session.commit()
    return DownloadOut(url=url, expires_in=settings.download_url_seconds, filename=doc.original_filename)


# ---------------------------------------------------------------- HR actions
@router.post("/documents/{doc_id}/verify", response_model=DocumentOut)
async def verify_document(doc_id: uuid.UUID, body: VerifyIn, request: Request,
                          user: User = Depends(require_roles(Role.HR)), session: AsyncSession = Depends(get_session)):
    doc = await _load_visible(session, user, doc_id)
    if doc.status != "Pending":
        raise HTTPException(409, f"This document is already {doc.status.lower()}")
    if _is_owner(user, doc) or doc.uploaded_by == user.id:
        raise HTTPException(403, "You cannot verify a document you uploaded or that belongs to you")
    if body.decision == "reject" and not (body.reason and body.reason.strip()):
        raise HTTPException(422, "A reason is required when rejecting a document")
    doc.status = "Verified" if body.decision == "verify" else "Rejected"
    doc.rejection_reason = body.reason.strip() if body.decision == "reject" else None
    doc.verified_by, doc.verified_by_email, doc.verified_at = user.id, user.email, utcnow()
    audit.record(session, user=user, action=f"document_{doc.status.lower()}", entity="document", entity_id=doc.id,
                 subject_staff_id=doc.staff_id, after={"status": doc.status, "reason": doc.rejection_reason},
                 request=request)
    await session.commit()
    return to_out(user, doc)


@router.delete("/documents/{doc_id}", status_code=204)
async def delete_document(doc_id: uuid.UUID, request: Request, user: User = Depends(get_current_user),
                          session: AsyncSession = Depends(get_session)):
    doc = await _load_visible(session, user, doc_id)
    if not _can_delete(user, doc):
        raise HTTPException(403, "You cannot delete this document")
    doc.deleted_at = utcnow()  # soft delete: record and stored file are retained
    audit.record(session, user=user, action="document_deleted", entity="document", entity_id=doc.id,
                 subject_staff_id=doc.staff_id, before={"status": doc.status, "type": doc.doc_type, "name": doc.name},
                 request=request)
    await session.commit()
    return Response(status_code=204)
