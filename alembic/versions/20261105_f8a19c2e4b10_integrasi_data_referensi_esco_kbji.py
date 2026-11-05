"""integrasi data referensi esco kbji

Revision ID: f8a19c2e4b10
Revises: d0bcf19ea53b
Create Date: 2026-11-05 15:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "f8a19c2e4b10"
down_revision: Union[str, Sequence[str], None] = "d0bcf19ea53b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"
    has_trgm = False
    if is_pg:
        has_trgm = bool(
            bind.exec_driver_sql("SELECT 1 FROM pg_available_extensions WHERE name = 'pg_trgm'").scalar()
        )
        if has_trgm:
            op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    with op.batch_alter_table("occupations", schema=None) as batch_op:
        batch_op.add_column(sa.Column("esco_uri", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("label_id", sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column("search_text", sa.Text(), nullable=True))
        batch_op.create_unique_constraint(batch_op.f("uq_occupations_esco_uri"), ["esco_uri"])

    with op.batch_alter_table("skills", schema=None) as batch_op:
        batch_op.add_column(sa.Column("label_id", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "alt_labels",
                sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"),
                nullable=False,
                server_default=sa.text("'[]'::jsonb" if is_pg else "'[]'"),
            )
        )

    if is_pg:
        op.add_column("occupation_skills", sa.Column("occupation_code", sa.String(length=16), nullable=True))
        op.execute("UPDATE occupation_skills SET occupation_code = isco_code WHERE occupation_code IS NULL")
        op.alter_column("occupation_skills", "occupation_code", existing_type=sa.String(length=16), nullable=False)
        op.drop_constraint("pk_occupation_skills", "occupation_skills", type_="primary")
        op.create_primary_key("pk_occupation_skills", "occupation_skills", ["occupation_code", "skill_id"])
        op.create_index(op.f("ix_occupation_skills_isco_code"), "occupation_skills", ["isco_code"], unique=False)
        op.create_index(op.f("ix_occupation_skills_skill_id"), "occupation_skills", ["skill_id"], unique=False)
    else:
        op.create_table(
            "occupation_skills_new",
            sa.Column("occupation_code", sa.String(length=16), nullable=False),
            sa.Column("isco_code", sa.String(length=8), nullable=False),
            sa.Column("skill_id", sa.Integer(), nullable=False),
            sa.Column("relation", sa.String(length=16), nullable=False),
            sa.ForeignKeyConstraint(
                ["skill_id"], ["skills.id"], name=op.f("fk_occupation_skills_skill_id_skills"), ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("occupation_code", "skill_id", name=op.f("pk_occupation_skills")),
        )
        op.execute(
            "INSERT INTO occupation_skills_new (occupation_code, isco_code, skill_id, relation) "
            "SELECT isco_code, isco_code, skill_id, relation FROM occupation_skills"
        )
        op.drop_table("occupation_skills")
        op.rename_table("occupation_skills_new", "occupation_skills")
        op.create_index(op.f("ix_occupation_skills_isco_code"), "occupation_skills", ["isco_code"], unique=False)
        op.create_index(op.f("ix_occupation_skills_skill_id"), "occupation_skills", ["skill_id"], unique=False)

    op.execute(
        "UPDATE occupations SET search_text = COALESCE(title, '') || ' | ' || COALESCE(title_en, '') "
        "WHERE search_text IS NULL"
    )

    if is_pg and has_trgm:
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_occupations_title_en_trgm "
            "ON occupations USING gin (title_en gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_occupations_label_id_trgm "
            "ON occupations USING gin (label_id gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_occupations_search_text_trgm "
            "ON occupations USING gin (search_text gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_skills_label_trgm "
            "ON skills USING gin (label gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_skills_label_en_trgm "
            "ON skills USING gin (label_en gin_trgm_ops)"
        )
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_skills_label_id_trgm "
            "ON skills USING gin (label_id gin_trgm_ops)"
        )


def downgrade() -> None:
    """Downgrade schema."""
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    if is_pg:
        for idx in (
            "ix_skills_label_id_trgm",
            "ix_skills_label_en_trgm",
            "ix_skills_label_trgm",
            "ix_occupations_search_text_trgm",
            "ix_occupations_label_id_trgm",
            "ix_occupations_title_en_trgm",
        ):
            op.execute(f"DROP INDEX IF EXISTS {idx}")

        op.drop_index(op.f("ix_occupation_skills_skill_id"), table_name="occupation_skills")
        op.drop_index(op.f("ix_occupation_skills_isco_code"), table_name="occupation_skills")
        op.execute(
            "DELETE FROM occupation_skills a USING occupation_skills b "
            "WHERE a.ctid < b.ctid AND a.isco_code = b.isco_code AND a.skill_id = b.skill_id"
        )
        op.drop_constraint("pk_occupation_skills", "occupation_skills", type_="primary")
        op.create_primary_key("pk_occupation_skills", "occupation_skills", ["isco_code", "skill_id"])
        op.drop_column("occupation_skills", "occupation_code")
    else:
        op.create_table(
            "occupation_skills_old",
            sa.Column("isco_code", sa.String(length=8), nullable=False),
            sa.Column("skill_id", sa.Integer(), nullable=False),
            sa.Column("relation", sa.String(length=16), nullable=False),
            sa.ForeignKeyConstraint(
                ["skill_id"], ["skills.id"], name=op.f("fk_occupation_skills_skill_id_skills"), ondelete="CASCADE"
            ),
            sa.PrimaryKeyConstraint("isco_code", "skill_id", name=op.f("pk_occupation_skills")),
        )
        op.execute(
            "INSERT OR IGNORE INTO occupation_skills_old (isco_code, skill_id, relation) "
            "SELECT isco_code, skill_id, relation FROM occupation_skills"
        )
        op.drop_table("occupation_skills")
        op.rename_table("occupation_skills_old", "occupation_skills")

    with op.batch_alter_table("skills", schema=None) as batch_op:
        batch_op.drop_column("alt_labels")
        batch_op.drop_column("label_id")

    with op.batch_alter_table("occupations", schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f("uq_occupations_esco_uri"), type_="unique")
        batch_op.drop_column("search_text")
        batch_op.drop_column("label_id")
        batch_op.drop_column("esco_uri")
