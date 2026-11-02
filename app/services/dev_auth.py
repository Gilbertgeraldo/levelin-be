"""Token akun demo untuk pengembangan lokal (tanpa Supabase Auth).

Token ditandatangani SUPABASE_JWT_SECRET (HS256) sehingga diverifikasi persis seperti token Supabase.
Login tanpa kata sandi hanya boleh untuk email domain demo (.test, tidak pernah ada di dunia nyata).
"""

import time
import uuid

import jwt

from app.core.config import get_settings

DEMO_EMAIL_DOMAIN = "sinar-distribusi.test"


def make_token(email: str, name: str | None, role: str | None = None, hours: int = 24, sub: str | None = None) -> str:
    settings = get_settings()
    if not settings.supabase_jwt_secret:
        raise RuntimeError("SUPABASE_JWT_SECRET belum diisi di .env")
    now = int(time.time())
    payload = {
        "sub": sub or str(uuid.uuid5(uuid.NAMESPACE_URL, email)),
        "email": email,
        "aud": settings.supabase_jwt_audience,
        "role": "authenticated",
        "iat": now,
        "exp": now + hours * 3600,
        "user_metadata": {"full_name": name} if name else {},
        "app_metadata": {"role": role} if role else {},
    }
    return jwt.encode(payload, settings.supabase_jwt_secret, algorithm="HS256")


def demo_login_enabled() -> bool:
    settings = get_settings()
    return (
        settings.dev_login_enabled
        and settings.app_env != "production"
        and bool(settings.supabase_jwt_secret)
    )


def is_demo_email(email: str) -> bool:
    return email.lower().endswith(f"@{DEMO_EMAIL_DOMAIN}")
