"""Add ingredient identities and local multilingual mappings.

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ingredient",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("family_id", sa.String(64), sa.ForeignKey("ingredient.id")),
        sa.Column("family_evidence", sa.Text()),
    )
    op.create_index("ix_ingredient_family_id", "ingredient", ["family_id"])
    op.create_table(
        "ingredient_identifier",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ingredient_id", sa.String(64), sa.ForeignKey("ingredient.id"), nullable=False),
        sa.Column("namespace", sa.String(64), nullable=False),
        sa.Column("value", sa.String(128), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.UniqueConstraint("namespace", "value"),
    )
    op.create_index("ix_ingredient_identifier_ingredient_id", "ingredient_identifier", ["ingredient_id"])
    op.create_table(
        "ingredient_alias",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ingredient_id", sa.String(64), sa.ForeignKey("ingredient.id"), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("normalized", sa.String(1024), nullable=False),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("evidence", sa.String(512), nullable=False),
        sa.UniqueConstraint("ingredient_id", "normalized", "language", "evidence"),
    )
    op.create_index("ix_ingredient_alias_ingredient_id", "ingredient_alias", ["ingredient_id"])
    op.create_index("ix_ingredient_alias_normalized", "ingredient_alias", ["normalized"])
    op.create_table(
        "ingredient_dictionary",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cache_path", sa.Text(), nullable=False),
        sa.Column("summary", sa.JSON(), nullable=False),
    )
    op.create_table(
        "product_ingredient",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("product.id"), nullable=False),
        sa.Column("ingredient_id", sa.String(64), sa.ForeignKey("ingredient.id"), nullable=False),
        sa.Column("source_text", sa.Text()),
        sa.Column("method", sa.String(32), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.Column("dictionary_version", sa.String(64), nullable=False),
        sa.UniqueConstraint("product_id", "ingredient_id"),
    )
    op.create_index("ix_product_ingredient_product_id", "product_ingredient", ["product_id"])
    op.create_index("ix_product_ingredient_ingredient_id", "product_ingredient", ["ingredient_id"])
    op.create_table(
        "product_ingredient_resolution",
        sa.Column("product_id", sa.Integer(), sa.ForeignKey("product.id"), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("components", sa.JSON(), nullable=False),
        sa.Column("dictionary_version", sa.String(64), nullable=False),
    )
    op.create_index("ix_product_ingredient_resolution_status", "product_ingredient_resolution", ["status"])
    op.create_table(
        "ingredient_override",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("source_id", sa.String(64), nullable=False),
        sa.Column("source_text", sa.Text(), nullable=False),
        sa.Column("ingredient_ids", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.Text(), nullable=False),
        sa.UniqueConstraint("source_id", "source_text"),
    )


def downgrade() -> None:
    for table in (
        "ingredient_override", "product_ingredient_resolution", "product_ingredient",
        "ingredient_dictionary", "ingredient_alias", "ingredient_identifier", "ingredient",
    ):
        op.drop_table(table)
