"""marzban bridge

Revision ID: 002
Revises: 001
Create Date: 2026-06-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

marzbanjobstatus = postgresql.ENUM("pending", "done", "failed", name="marzbanjobstatus", create_type=False)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                CREATE TYPE marzbanjobstatus AS ENUM ('pending', 'done', 'failed');
            EXCEPTION
                WHEN duplicate_object THEN null;
            END $$;
            """
        )
    )

    op.add_column("user_account_links", sa.Column("status_cache", sa.String(length=32), nullable=True))
    op.add_column("user_account_links", sa.Column("expires_at_cache", sa.DateTime(timezone=True), nullable=True))
    op.add_column("user_account_links", sa.Column("data_limit_cache", sa.BigInteger(), nullable=True))
    op.add_column("user_account_links", sa.Column("data_used_cache", sa.BigInteger(), nullable=True))
    op.add_column("user_account_links", sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "marzban_jobs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("payment_id", sa.UUID(), nullable=True),
        sa.Column("account_username", sa.String(length=64), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", marzbanjobstatus, nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_marzban_jobs_account_username"), "marzban_jobs", ["account_username"], unique=False)
    op.create_index(op.f("ix_marzban_jobs_payment_id"), "marzban_jobs", ["payment_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_marzban_jobs_payment_id"), table_name="marzban_jobs")
    op.drop_index(op.f("ix_marzban_jobs_account_username"), table_name="marzban_jobs")
    op.drop_table("marzban_jobs")
    op.drop_column("user_account_links", "last_synced_at")
    op.drop_column("user_account_links", "data_used_cache")
    op.drop_column("user_account_links", "data_limit_cache")
    op.drop_column("user_account_links", "expires_at_cache")
    op.drop_column("user_account_links", "status_cache")
    op.execute(sa.text("DROP TYPE IF EXISTS marzbanjobstatus"))
