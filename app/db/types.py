"""Tipe kolom yang jalan di Postgres (produksi, Supabase) dan SQLite (dev lokal & tes)."""

from datetime import UTC, datetime

from pgvector.sqlalchemy import VECTOR
from sqlalchemy import JSON, DateTime
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.types import TypeDecorator

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Vector(TypeDecorator):
    """pgvector di Postgres, JSON list di database lain."""

    impl = JSON
    cache_ok = True

    def __init__(self, dim: int) -> None:
        super().__init__()
        self.dim = dim

    def __repr__(self) -> str:
        return f"Vector(dim={self.dim})"

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(VECTOR(self.dim))
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        return [float(v) for v in value]

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if hasattr(value, "to_list"):
            value = value.to_list()
        return [float(v) for v in value]


class UTCDateTime(TypeDecorator):
    """Selalu mengembalikan datetime ber-zona UTC (SQLite menyimpannya tanpa zona)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def __repr__(self) -> str:
        return "UTCDateTime()"

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value
