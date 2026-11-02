import re
import uuid
from pathlib import Path

from app.core.config import get_settings
from app.core.errors import ApiError
from app.services import supabase_admin

ALLOWED_DOCUMENT_TYPES = {
    "application/pdf": ".pdf",
    "application/msword": ".doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
}
# Tanda tangan berkas (magic bytes) supaya jenis berkas tidak hanya dipercaya dari header
_SIGNATURES = {".pdf": b"%PDF", ".docx": b"PK\x03\x04", ".doc": b"\xd0\xcf\x11\xe0"}


def check_document(filename: str | None, content_type: str | None, content: bytes) -> str:
    settings = get_settings()
    if len(content) > settings.max_upload_mb * 1024 * 1024:
        raise ApiError(413, "payload_too_large", f"Ukuran berkas maksimal {settings.max_upload_mb} MB.")
    ext = ALLOWED_DOCUMENT_TYPES.get(content_type or "")
    if ext is None and filename:
        ext = Path(filename).suffix.lower()
    if ext not in _SIGNATURES or not content.startswith(_SIGNATURES[ext]):
        raise ApiError(415, "unsupported_media_type", "Unggah dokumen PDF atau Word (.doc/.docx).")
    return ext


async def save_document(org_id: uuid.UUID, filename: str | None, content: bytes, content_type: str, ext: str) -> str:
    settings = get_settings()
    stem = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(filename or "dokumen").stem)[:60] or "dokumen"
    key = f"{org_id}/{uuid.uuid4().hex}-{stem}{ext}"
    if settings.storage_backend == "supabase":
        await supabase_admin.upload_object(settings.supabase_storage_bucket, key, content, content_type)
        return f"supabase://{settings.supabase_storage_bucket}/{key}"
    path = Path(settings.upload_dir) / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return f"local://{key}"
