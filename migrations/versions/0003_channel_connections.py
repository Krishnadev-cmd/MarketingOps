"""Channel connections: the signed-in account each channel posts through.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-18

Holds the LinkedIn connection made with "Connect LinkedIn". Tokens are stored
encrypted by the app (services/secrets_box.py); this table only ever sees ciphertext.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import mysql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

MYSQL = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def utc_datetime() -> sa.types.TypeEngine:
    return sa.DateTime().with_variant(mysql.DATETIME(fsp=6), "mysql")


def upgrade() -> None:
    op.create_table(
        "channel_connections",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("account_urn", sa.String(100), nullable=False),
        sa.Column("account_name", sa.String(200), nullable=False),
        sa.Column("access_token_enc", sa.Text(), nullable=False),
        sa.Column("refresh_token_enc", sa.Text(), nullable=False),
        sa.Column("scopes", sa.String(500), nullable=False),
        sa.Column("expires_at", utc_datetime(), nullable=False),
        sa.Column("connected_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", name="fk_channel_connections_connected_by_id_users"), nullable=False),
        sa.Column("created_at", utc_datetime(), nullable=False),
        sa.Column("updated_at", utc_datetime(), nullable=False),
        **MYSQL,
    )
    # One connection per provider: reconnecting replaces it.
    op.create_index("ix_channel_connections_provider", "channel_connections", ["provider"], unique=True)


def downgrade() -> None:
    op.drop_table("channel_connections")
