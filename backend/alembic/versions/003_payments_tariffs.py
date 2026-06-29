"""payments and tariffs

Revision ID: 003
Revises: 002
Create Date: 2026-06-10

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "003"
down_revision: Union[str, None] = "002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

paymentstatus = postgresql.ENUM(
    "pending", "succeeded", "failed", "canceled", name="paymentstatus", create_type=False
)


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            DO $$ BEGIN
                CREATE TYPE paymentstatus AS ENUM ('pending', 'succeeded', 'failed', 'canceled');
            EXCEPTION
                WHEN duplicate_object THEN null;
            END $$;
            """
        )
    )

    op.create_table(
        "tariffs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("period_days", sa.Integer(), nullable=False),
        sa.Column("price_rub", sa.Numeric(10, 2), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "payments",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("yukassa_payment_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("tariff_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),
        sa.Column("status", paymentstatus, nullable=False),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("marzban_extended", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tariff_id"], ["tariffs.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("yukassa_payment_id"),
    )
    op.create_index(op.f("ix_payments_user_id"), "payments", ["user_id"], unique=False)
    op.create_index(op.f("ix_payments_yukassa_payment_id"), "payments", ["yukassa_payment_id"], unique=True)

    op.create_foreign_key(
        "fk_marzban_jobs_payment_id", "marzban_jobs", "payments", ["payment_id"], ["id"], ondelete="SET NULL"
    )

    op.execute(
        sa.text(
            """
            INSERT INTO tariffs (name, period_days, price_rub, is_active) VALUES
            ('Доступ к материалам — 1 месяц', 30, 300.00, true),
            ('Доступ к материалам — 3 месяца', 90, 800.00, true),
            ('Доступ к материалам — 12 месяцев', 365, 2500.00, true)
            """
        )
    )


def downgrade() -> None:
    op.drop_constraint("fk_marzban_jobs_payment_id", "marzban_jobs", type_="foreignkey")
    op.drop_index(op.f("ix_payments_yukassa_payment_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_user_id"), table_name="payments")
    op.drop_table("payments")
    op.drop_table("tariffs")
    op.execute(sa.text("DROP TYPE IF EXISTS paymentstatus"))
