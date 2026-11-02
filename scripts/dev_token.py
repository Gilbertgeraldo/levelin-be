"""Buat JWT HS256 untuk pengujian lokal, ditandatangani SUPABASE_JWT_SECRET dari .env.

    uv run python -m scripts.dev_token --email budi@contoh-sejahtera.test
    uv run python -m scripts.dev_token --email hr@contoh-sejahtera.test --role hr_admin

Hanya untuk dev. Di produksi token dibuat Supabase Auth saat pengguna login.
"""

import argparse

from app.services.dev_auth import make_token

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name")
    parser.add_argument("--role", choices=["worker", "hr_admin", "reviewer"])
    parser.add_argument("--sub", help="UUID pengguna; default diturunkan dari email")
    parser.add_argument("--hours", type=int, default=24)
    args = parser.parse_args()
    try:
        print(make_token(args.email, args.name, args.role, args.hours, args.sub))
    except RuntimeError as exc:
        raise SystemExit(str(exc)) from exc
