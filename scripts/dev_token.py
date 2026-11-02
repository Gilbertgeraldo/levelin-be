"""Buat JWT HS256 untuk pengujian lokal, ditandatangani SUPABASE_JWT_SECRET dari .env.

    uv run python -m scripts.dev_token --email budi@contoh-sejahtera.test
    uv run python -m scripts.dev_token --email hr@contoh-sejahtera.test --role hr_admin

Hanya untuk dev. Di produksi token dibuat Supabase Auth saat pengguna login.
"""

import argparse
import time
import uuid

import jwt

from app.core.config import get_settings


def make_token(email: str, name: str | None, role: str | None, hours: int, sub: str | None = None) -> str:
    settings = get_settings()
    if not settings.supabase_jwt_secret:
        raise SystemExit("SUPABASE_JWT_SECRET belum diisi di .env")
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


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name")
    parser.add_argument("--role", choices=["worker", "hr_admin", "reviewer"])
    parser.add_argument("--sub", help="UUID pengguna; default diturunkan dari email")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    print(make_token(args.email, args.name, args.role, args.hours, args.sub))
