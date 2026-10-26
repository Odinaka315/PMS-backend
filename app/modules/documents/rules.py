"""What may be uploaded. Deliberately a short allow-list: PDF, JPEG and PNG only.
SVG, HTML and Office files are refused because they can carry scripts or macros."""
import os
import re

DOC_TYPES = ("License", "NYSC", "Promotion", "Appointment", "Certificate", "Passport", "NIN", "CV", "Other")

# content type -> (allowed extensions, magic bytes the file must start with)
ALLOWED: dict[str, tuple[tuple[str, ...], bytes]] = {
    "application/pdf": ((".pdf",), b"%PDF-"),
    "image/jpeg": ((".jpg", ".jpeg"), b"\xff\xd8\xff"),
    "image/png": ((".png",), b"\x89PNG\r\n\x1a\n"),
}
MAGIC_BYTES_NEEDED = 8


def clean_filename(filename: str) -> str:
    """Strip any path and control characters; keep it short."""
    name = os.path.basename(filename.replace("\\", "/"))
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).strip()
    return name[:200] or "document"


def check_declared(filename: str, content_type: str, size: int, max_bytes: int) -> str | None:
    """Returns an error message, or None if the declared upload is acceptable."""
    rule = ALLOWED.get(content_type)
    if rule is None:
        return "Only PDF, JPEG and PNG files are accepted"
    if not filename.lower().endswith(rule[0]):
        return f"The file extension does not match its type ({content_type})"
    if size <= 0:
        return "The file is empty"
    if size > max_bytes:
        return f"The file is too large (limit {max_bytes // (1024 * 1024)} MB)"
    return None


def matches_magic(content_type: str, head: bytes) -> bool:
    rule = ALLOWED.get(content_type)
    return rule is not None and head.startswith(rule[1])
