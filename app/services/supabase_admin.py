"""Panggilan admin ke Supabase (butuh service role key). Semua best-effort: gagal tidak memutus alur."""

import logging
import uuid

import httpx

from app.core.config import get_settings

logger = logging.getLogger("levelin.supabase")


def _config() -> tuple[str, str] | None:
    s = get_settings()
    if not s.supabase_url or not s.supabase_service_role_key:
        return None
    return s.supabase_url.rstrip("/"), s.supabase_service_role_key


def configured() -> bool:
    return _config() is not None


def _headers(key: str) -> dict:
    return {"apikey": key, "Authorization": f"Bearer {key}"}


async def invite_user_by_email(email: str, data: dict | None = None, redirect_to: str | None = None) -> bool:
    cfg = _config()
    if cfg is None:
        return False
    base, key = cfg
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                f"{base}/auth/v1/invite",
                headers=_headers(key),
                params={"redirect_to": redirect_to} if redirect_to else None,
                json={"email": email, "data": data or {}},
            )
        # 422 = email sudah terdaftar; undangan tetap tercatat dan akan diterima saat login
        return resp.status_code < 300
    except httpx.HTTPError as exc:
        logger.warning("Supabase invite failed: %s", exc)
        return False


async def delete_auth_user(user_id: uuid.UUID) -> bool:
    cfg = _config()
    if cfg is None:
        return False
    base, key = cfg
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.delete(f"{base}/auth/v1/admin/users/{user_id}", headers=_headers(key))
        return resp.status_code < 300 or resp.status_code == 404
    except httpx.HTTPError as exc:
        logger.warning("Supabase delete user failed: %s", exc)
        return False


async def upload_object(bucket: str, path: str, content: bytes, content_type: str) -> bool:
    cfg = _config()
    if cfg is None:
        return False
    base, key = cfg
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.post(
            f"{base}/storage/v1/object/{bucket}/{path}",
            headers={**_headers(key), "Content-Type": content_type, "x-upsert": "true"},
            content=content,
        )
    resp.raise_for_status()
    return True
