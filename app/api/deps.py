from typing import Annotated

from fastapi import Depends, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import ApiError
from app.core.pagination import PageParams
from app.core.security import get_current_user, require_roles
from app.db.session import get_db
from app.models import User
from app.services.embeddings import EmbeddingService, get_embeddings
from app.services.llm import LLMService, get_llm

DB = Annotated[AsyncSession, Depends(get_db)]
CurrentUser = Annotated[User, Depends(get_current_user)]
Reviewer = Annotated[User, Depends(require_roles("reviewer"))]
LLM = Annotated[LLMService, Depends(get_llm)]
Embeddings = Annotated[EmbeddingService, Depends(get_embeddings)]
Pagination = Annotated[PageParams, Depends()]


async def _hr_admin(user: User = Depends(require_roles("hr_admin"))) -> User:
    if user.org_id is None:
        raise ApiError(403, "no_organization", "Akun HR belum terhubung ke organisasi.")
    return user


HRAdmin = Annotated[User, Depends(_hr_admin)]


def cache_public(response: Response, seconds: int) -> None:
    response.headers["Cache-Control"] = f"public, max-age={seconds}"


def is_postgres(db: AsyncSession) -> bool:
    return db.get_bind().dialect.name == "postgresql"
