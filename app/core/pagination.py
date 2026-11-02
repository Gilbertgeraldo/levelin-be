import math
from typing import Annotated

from fastapi import Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession


class PageParams:
    def __init__(
        self,
        page: Annotated[int, Query(ge=1, description="Nomor halaman, mulai dari 1")] = 1,
        limit: Annotated[int, Query(ge=1, le=100, description="Jumlah item per halaman")] = 10,
    ) -> None:
        self.page = page
        self.limit = limit

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.limit


class PageMeta(BaseModel):
    page: int
    limit: int
    total: int
    total_pages: int
    has_next: bool

    @classmethod
    def build(cls, params: PageParams, total: int) -> "PageMeta":
        pages = math.ceil(total / params.limit) if total else 0
        return cls(
            page=params.page,
            limit=params.limit,
            total=total,
            total_pages=pages,
            has_next=params.page < pages,
        )


class Page[T](BaseModel):
    data: list[T]
    meta: PageMeta


async def paginate_scalars(db: AsyncSession, stmt: Select, params: PageParams) -> tuple[list, PageMeta]:
    total = await db.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
    rows = (await db.scalars(stmt.limit(params.limit).offset(params.offset))).all()
    return list(rows), PageMeta.build(params, total or 0)


def paginate_list(items: list, params: PageParams) -> tuple[list, PageMeta]:
    """Paginasi untuk daftar yang diurutkan di Python (mis. hasil pemeringkatan)."""
    return items[params.offset : params.offset + params.limit], PageMeta.build(params, len(items))
