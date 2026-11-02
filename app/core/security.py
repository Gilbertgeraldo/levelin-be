import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

import jwt
from fastapi import Depends
from fastapi.concurrency import run_in_threadpool
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import ApiError
from app.db.base import utcnow
from app.db.session import get_db
from app.models import ROLES, User
from app.services.invitations import accept_pending_invitation

logger = logging.getLogger("levelin.auth")

bearer_scheme = HTTPBearer(auto_error=False, description="JWT dari Supabase Auth")

_jwks_client: jwt.PyJWKClient | None = None


@dataclass
class TokenClaims:
    sub: uuid.UUID
    email: str | None
    full_name: str | None
    role: str | None  # dari app_metadata.role (hanya bisa diisi service role Supabase)


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        base = get_settings().supabase_url.rstrip("/")
        _jwks_client = jwt.PyJWKClient(f"{base}/auth/v1/.well-known/jwks.json", lifespan=3600)
    return _jwks_client


def _decode_sync(token: str) -> dict:
    settings = get_settings()
    alg = jwt.get_unverified_header(token).get("alg")
    if alg == "HS256" and settings.supabase_jwt_secret:
        key = settings.supabase_jwt_secret
    elif alg in ("RS256", "ES256") and settings.supabase_url:
        key = _jwks().get_signing_key_from_jwt(token).key
    else:
        raise jwt.InvalidAlgorithmError(f"Algoritma {alg} tidak dikonfigurasi")
    return jwt.decode(
        token,
        key,
        algorithms=[alg],
        audience=settings.supabase_jwt_audience,
        options={"require": ["exp", "sub"]},
    )


async def decode_token(token: str) -> TokenClaims:
    try:
        # JWKS mungkin perlu fetch jaringan (blocking), jadi jalankan di threadpool
        payload = await run_in_threadpool(_decode_sync, token)
        sub = uuid.UUID(str(payload["sub"]))
    except jwt.ExpiredSignatureError as exc:
        raise ApiError(401, "token_expired", "Sesi berakhir, silakan login ulang.") from exc
    except (jwt.PyJWTError, ValueError, KeyError) as exc:
        raise ApiError(401, "invalid_token", "Token tidak valid.") from exc
    user_meta = payload.get("user_metadata") or {}
    app_meta = payload.get("app_metadata") or {}
    role = app_meta.get("role")
    return TokenClaims(
        sub=sub,
        email=(payload.get("email") or None),
        full_name=user_meta.get("full_name") or user_meta.get("name"),
        role=role if role in ROLES else None,
    )


async def _provision_user(db: AsyncSession, claims: TokenClaims) -> User:
    user = User(
        id=claims.sub,
        email=claims.email.lower() if claims.email else None,
        full_name=claims.full_name,
        role=claims.role or "worker",
        last_active_at=utcnow(),
    )
    db.add(user)
    try:
        await db.flush()
        await accept_pending_invitation(db, user)
        await db.commit()
    except IntegrityError:
        # Dua request pertama datang bersamaan; pakai baris yang sudah dibuat
        await db.rollback()
        existing = await db.get(User, claims.sub)
        if existing is None:
            raise
        return existing
    return user


async def get_current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    if creds is None:
        raise ApiError(401, "unauthorized", "Header Authorization: Bearer <JWT> wajib diisi.")
    claims = await decode_token(creds.credentials)
    user = await db.get(User, claims.sub)
    if user is None:
        return await _provision_user(db, claims)

    changed = False
    if claims.role and user.role != claims.role:
        user.role = claims.role
        changed = True
    if claims.email and user.email != claims.email.lower():
        user.email = claims.email.lower()
        changed = True
    now = utcnow()
    if user.last_active_at is None or now - user.last_active_at > timedelta(minutes=30):
        user.last_active_at = now
        changed = True
    if changed:
        await db.commit()
    return user


def require_roles(*roles: str):
    async def checker(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            raise ApiError(403, "forbidden", "Peran kamu tidak punya akses ke fitur ini.")
        return user

    return checker
