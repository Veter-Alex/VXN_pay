"""roles categories invites manager assignments

Revision ID: 004
Revises: 003
Create Date: 2026-07-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "004"
down_revision: Union[str, None] = "003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                CREATE TYPE tariffcategory AS ENUM ('preferential', 'commercial');
            EXCEPTION
                WHEN duplicate_object THEN null;
            END $$;
            """
        )
    )
    op.execute(sa.text("ALTER TYPE userrole ADD VALUE IF NOT EXISTS 'manager'"))

    op.add_column(
        "users",
        sa.Column(
            "tariff_category",
            postgresql.ENUM("preferential", "commercial", name="tariffcategory", create_type=False),
            nullable=False,
            server_default="commercial",
        ),
    )
    op.add_column("users", sa.Column("email_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "users",
        sa.Column("password_set", sa.Boolean(), nullable=False, server_default=sa.text("true")),
    )

    op.add_column("user_account_links", sa.Column("subscription_url_cache", sa.String(length=512), nullable=True))

    op.add_column(
        "tariffs",
        sa.Column(
            "category",
            postgresql.ENUM("preferential", "commercial", name="tariffcategory", create_type=False),
            nullable=False,
            server_default="commercial",
        ),
    )

    op.create_table(
        "invite_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_invite_tokens_user_id"), "invite_tokens", ["user_id"], unique=False)

    op.create_table(
        "email_verification_tokens",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("token_hash", sa.String(length=255), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_email_verification_tokens_user_id"),
        "email_verification_tokens",
        ["user_id"],
        unique=False,
    )

    op.create_table(
        "manager_assignments",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("manager_user_id", sa.UUID(), nullable=False),
        sa.Column("managed_user_id", sa.UUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["manager_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["managed_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("manager_user_id", "managed_user_id", name="uq_manager_managed"),
    )
    op.create_index(
        op.f("ix_manager_assignments_manager_user_id"),
        "manager_assignments",
        ["manager_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_manager_assignments_managed_user_id"),
        "manager_assignments",
        ["managed_user_id"],
        unique=False,
    )

    op.execute(sa.text("UPDATE tariffs SET is_active = false"))
    op.execute(
        sa.text(
            """
            INSERT INTO tariffs (name, period_days, price_rub, category, is_active) VALUES
            ('Льготный доступ — 1 месяц', 30, 75.00, 'preferential', true),
            ('Льготный доступ — 3 месяца', 90, 225.00, 'preferential', true),
            ('Коммерческий доступ — 1 месяц', 30, 500.00, 'commercial', true),
            ('Коммерческий доступ — 3 месяца', 90, 1500.00, 'commercial', true)
            """
        )
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_manager_assignments_managed_user_id"), table_name="manager_assignments")
    op.drop_index(op.f("ix_manager_assignments_manager_user_id"), table_name="manager_assignments")
    op.drop_table("manager_assignments")
    op.drop_index(op.f("ix_email_verification_tokens_user_id"), table_name="email_verification_tokens")
    op.drop_table("email_verification_tokens")
    op.drop_index(op.f("ix_invite_tokens_user_id"), table_name="invite_tokens")
    op.drop_table("invite_tokens")
    op.drop_column("tariffs", "category")
    op.drop_column("user_account_links", "subscription_url_cache")
    op.drop_column("users", "password_set")
    op.drop_column("users", "email_verified_at")
    op.drop_column("users", "tariff_category")
    op.execute(sa.text("DROP TYPE IF EXISTS tariffcategory"))
