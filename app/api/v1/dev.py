"""Login akun demo tanpa kata sandi, untuk mencoba web dashboard sebelum Supabase Auth disiapkan.

Mati kecuali DEV_LOGIN_ENABLED=true (dan APP_ENV bukan production); hanya untuk email domain demo.
Tidak tampil di OpenAPI.
"""

from fastapi import APIRouter
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.api.deps import DB
from app.core.errors import ApiError, not_found
from app.models import Invitation, User
from app.services import dev_auth

router = APIRouter(prefix="/dev", tags=["Dev"], include_in_schema=False)

TOKEN_HOURS = 12


class DemoLoginIn(BaseModel):
    # Bukan EmailStr: validator menolak domain .test yang justru dipakai akun demo
    email: str = Field(min_length=3, max_length=320)


class DemoLoginOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


@router.post("/demo-login", response_model=DemoLoginOut)
async def demo_login(body: DemoLoginIn, db: DB):
    if not dev_auth.demo_login_enabled():
        raise not_found("Endpoint")
    email = body.email.strip().lower()
    if not dev_auth.is_demo_email(email):
        raise ApiError(403, "forbidden", "Login demo hanya untuk akun demo.")
    user = await db.scalar(select(User).where(func.lower(User.email) == email))
    name = user.full_name if user else None
    if name is None:
        name = await db.scalar(
            select(Invitation.full_name).where(func.lower(Invitation.email) == email).order_by(Invitation.created_at)
        )
    token = dev_auth.make_token(email, name, hours=TOKEN_HOURS, sub=str(user.id) if user else None)
    return {"access_token": token, "expires_in": TOKEN_HOURS * 3600}
