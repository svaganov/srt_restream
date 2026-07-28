"""sessions, desired state, and encrypted endpoint secrets

Revision ID: 0001
Revises:
Create Date: 2026-07-28

Adds:
- user_sessions table (opaque session auth)
- passphrase_encrypted + desired_active on input/output streams
"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def _column_exists(table: str, column: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return column in {c["name"] for c in inspector.get_columns(table)}


def _table_exists(table: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return table in inspector.get_table_names()


def upgrade() -> None:
    if not _table_exists("user_sessions"):
        op.create_table(
            "user_sessions",
            sa.Column("id", sa.Integer(), primary_key=True, index=True),
            sa.Column(
                "user_id",
                sa.Integer(),
                sa.ForeignKey("users.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("token_hash", sa.String(length=64), unique=True, index=True, nullable=False),
            sa.Column("csrf_token", sa.String(length=64), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=True),
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.Column("last_used_at", sa.DateTime(), nullable=True),
            sa.Column("revoked", sa.Boolean(), default=False),
        )

    for table in ("input_streams", "output_streams"):
        if _table_exists(table) and not _column_exists(table, "passphrase_encrypted"):
            op.add_column(table, sa.Column("passphrase_encrypted", sa.String(), server_default=""))
        if _table_exists(table) and not _column_exists(table, "desired_active"):
            op.add_column(table, sa.Column("desired_active", sa.Boolean(), server_default=sa.false()))

    if _table_exists("input_streams") and not _column_exists("input_streams", "mode"):
        op.add_column(
            "input_streams",
            sa.Column("mode", sa.String(), server_default="listener"),
        )


def downgrade() -> None:
    for table in ("input_streams", "output_streams"):
        if _table_exists(table) and _column_exists(table, "desired_active"):
            op.drop_column(table, "desired_active")
        if _table_exists(table) and _column_exists(table, "passphrase_encrypted"):
            op.drop_column(table, "passphrase_encrypted")
    if _table_exists("input_streams") and _column_exists("input_streams", "mode"):
        op.drop_column("input_streams", "mode")
    if _table_exists("user_sessions"):
        op.drop_table("user_sessions")
