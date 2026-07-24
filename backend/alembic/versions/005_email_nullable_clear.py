"""email nullable and clear all emails

Revision ID: 005
Revises: 004
Create Date: 2026-07-24

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "005"
down_revision: Union[str, None] = "004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Почта больше не обязательна и не подставляется автоматически
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=True)
    op.execute(sa.text("UPDATE users SET email = NULL, email_verified_at = NULL"))


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE users SET email = login || '@localhost' WHERE email IS NULL"
        )
    )
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=False)
