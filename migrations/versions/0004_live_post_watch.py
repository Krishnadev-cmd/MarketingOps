"""Live post watch: check published posts still say what was approved.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19

Adds a history of checks (live_checks) and three summary columns on contents so
lists can show "Changed live" without reading the history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MYSQL = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    # server_default fills the new NOT NULL column for rows that already exist.
    with op.batch_alter_table("contents") as batch:
        batch.add_column(sa.Column("live_status", sa.String(16), nullable=True))
        batch.add_column(sa.Column("live_checked_at", utc_datetime(), nullable=True))
        batch.add_column(sa.Column("live_acked_hash", sa.String(64), nullable=False, server_default=""))
        batch.create_index("ix_contents_live_status", ["live_status"])

    op.create_table(
        "live_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("content_id", sa.Integer(),
                  sa.ForeignKey("contents.id", name="fk_live_checks_content_id_contents"), nullable=False),
        sa.Column("checked_at", utc_datetime(), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("detail", sa.String(500), nullable=False),
        sa.Column("live_text", sa.Text(), nullable=False),
        sa.Column("live_hash", sa.String(64), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=True),
        sa.Column("checked_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_live_checks_checked_by_id_users"), nullable=True),
        **MYSQL,
    )
    op.create_index("ix_live_checks_content_id", "live_checks", ["content_id"])
    op.create_index("ix_live_checks_checked_at", "live_checks", ["checked_at"])


def downgrade() -> None:
    op.drop_table("live_checks")
    with op.batch_alter_table("contents") as batch:
        batch.drop_index("ix_contents_live_status")
        batch.drop_column("live_acked_hash")
        batch.drop_column("live_checked_at")
        batch.drop_column("live_status")
