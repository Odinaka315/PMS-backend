"""Object storage for staff documents.

Two interchangeable backends behind one interface:
  * R2Storage    - Cloudflare R2 over the S3 API. The browser talks to R2 directly through short-lived signed links;
                   file bytes never pass through this server.
  * LocalStorage - files in a folder, with the same signed-link flow served by this app (development and tests only).
"""
import asyncio
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from urllib.parse import quote

import jwt

from .config import settings


@dataclass
class PresignedUpload:
    url: str
    method: str = "PUT"
    headers: dict[str, str] = field(default_factory=dict)  # the client must send exactly these


@dataclass
class ObjectInfo:
    size: int
    content_type: str | None


class Storage(Protocol):
    async def presign_upload(self, key: str, content_type: str, size: int, expires: int) -> PresignedUpload: ...
    async def presign_download(self, key: str, filename: str, content_type: str, expires: int) -> str: ...
    async def head(self, key: str) -> ObjectInfo | None: ...
    async def read_start(self, key: str, n: int) -> bytes: ...
    async def copy(self, src: str, dst: str) -> None: ...
    async def delete(self, key: str) -> None: ...


def content_disposition(filename: str) -> str:
    """Always 'attachment', so a browser downloads the file instead of rendering it."""
    ascii_name = re.sub(r'[^A-Za-z0-9._ -]', "_", filename)[:100] or "document"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename[:150])}"


class R2Storage:
    def __init__(self) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.r2_bucket
        self._c = boto3.client(
            "s3",
            endpoint_url=settings.r2_endpoint_url or f"https://{settings.r2_account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=settings.r2_access_key_id,
            aws_secret_access_key=settings.r2_secret_access_key,
            region_name="auto",
            config=Config(signature_version="s3v4", s3={"addressing_style": "path"},
                          retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=20),
        )

    async def presign_upload(self, key, content_type, size, expires):
        # Content-Type and Content-Length are part of the signature, so the upload must match what was authorised.
        url = self._c.generate_presigned_url(
            "put_object",
            Params={"Bucket": self.bucket, "Key": key, "ContentType": content_type, "ContentLength": size},
            ExpiresIn=expires, HttpMethod="PUT",
        )
        return PresignedUpload(url=url, method="PUT", headers={"Content-Type": content_type})

    async def presign_download(self, key, filename, content_type, expires):
        return self._c.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key, "ResponseContentDisposition": content_disposition(filename),
                    "ResponseContentType": content_type, "ResponseCacheControl": "private, no-store"},
            ExpiresIn=expires,
        )

    async def head(self, key):
        from botocore.exceptions import ClientError

        try:
            r = await asyncio.to_thread(self._c.head_object, Bucket=self.bucket, Key=key)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return None
            raise
        return ObjectInfo(size=r["ContentLength"], content_type=r.get("ContentType"))

    async def read_start(self, key, n):
        def _get():
            r = self._c.get_object(Bucket=self.bucket, Key=key, Range=f"bytes=0-{n - 1}")
            return r["Body"].read()

        return await asyncio.to_thread(_get)

    async def copy(self, src, dst):
        await asyncio.to_thread(self._c.copy_object, Bucket=self.bucket, Key=dst,
                                CopySource={"Bucket": self.bucket, "Key": src})

    async def delete(self, key):
        await asyncio.to_thread(self._c.delete_object, Bucket=self.bucket, Key=key)


class LocalStorage:
    """Development/test stand-in. Signed links point at this app's /dev-storage endpoints."""

    def __init__(self, root: str | None = None) -> None:
        self.root = Path(root or settings.local_storage_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        p = (self.root / key).resolve()
        if self.root not in p.parents:
            raise ValueError("invalid storage key")
        return p

    def _token(self, op: str, key: str, expires: int, **extra) -> str:
        claims = {"type": "storage", "op": op, "k": key, "exp": int(time.time()) + expires, **extra}
        return jwt.encode(claims, settings.secret_key, algorithm="HS256")

    @staticmethod
    def verify(token: str, op: str) -> dict | None:
        try:
            claims = jwt.decode(token, settings.secret_key, algorithms=["HS256"])
        except jwt.PyJWTError:
            return None
        return claims if claims.get("type") == "storage" and claims.get("op") == op else None

    def _url(self, token: str) -> str:
        return f"{settings.api_public_url.rstrip('/')}/api/v1/dev-storage/{token}"

    async def presign_upload(self, key, content_type, size, expires):
        token = self._token("put", key, expires, ct=content_type, sz=size)
        return PresignedUpload(url=self._url(token), method="PUT", headers={"Content-Type": content_type})

    async def presign_download(self, key, filename, content_type, expires):
        return self._url(self._token("get", key, expires, fn=filename, ct=content_type))

    async def head(self, key):
        p = self.path(key)
        return ObjectInfo(size=p.stat().st_size, content_type=None) if p.is_file() else None

    async def read_start(self, key, n):
        with self.path(key).open("rb") as f:
            return f.read(n)

    async def copy(self, src, dst):
        d = self.path(dst)
        d.parent.mkdir(parents=True, exist_ok=True)
        d.write_bytes(self.path(src).read_bytes())

    async def delete(self, key):
        self.path(key).unlink(missing_ok=True)

    def write(self, key: str, data: bytes) -> None:
        p = self.path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


_storage: Storage | None = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = R2Storage() if settings.storage_backend == "r2" else LocalStorage()
    return _storage
