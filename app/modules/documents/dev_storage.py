"""Signed upload/download endpoints that stand in for R2 during development and tests.
Only mounted when STORAGE_BACKEND=local and ENVIRONMENT is not production (see app/main.py)."""
from fastapi import APIRouter, HTTPException, Request, Response

from app.core.storage import LocalStorage, content_disposition, get_storage

router = APIRouter(prefix="/dev-storage", tags=["dev-storage"], include_in_schema=False)


def _store() -> LocalStorage:
    s = get_storage()
    assert isinstance(s, LocalStorage)
    return s


@router.put("/{token}", status_code=200)
async def put_object(token: str, request: Request):
    claims = LocalStorage.verify(token, "put")
    if claims is None:
        raise HTTPException(403, "Invalid or expired upload link")
    # Mirror what R2 enforces through the signature: exact content type and exact length.
    if request.headers.get("content-type") != claims["ct"]:
        raise HTTPException(403, "Content-Type does not match the signed upload")
    if int(request.headers.get("content-length", "-1")) != claims["sz"]:
        raise HTTPException(403, "Content-Length does not match the signed upload")
    body = await request.body()
    if len(body) != claims["sz"]:
        raise HTTPException(403, "Body length does not match the signed upload")
    _store().write(claims["k"], body)
    return Response(status_code=200)


@router.get("/{token}")
async def get_object(token: str):
    claims = LocalStorage.verify(token, "get")
    if claims is None:
        raise HTTPException(403, "Invalid or expired download link")
    p = _store().path(claims["k"])
    if not p.is_file():
        raise HTTPException(404, "File not found")
    return Response(content=p.read_bytes(), media_type=claims["ct"], headers={
        "Content-Disposition": content_disposition(claims["fn"]),
        "Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"})
