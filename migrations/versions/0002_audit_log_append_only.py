"""Make the audit log append-only inside MySQL itself.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-18

The app never updates or deletes audit rows, but "the code doesn't" is a weak
answer for a regulator. These triggers make the database refuse UPDATE and
DELETE on audit_logs for every client, including someone with a SQL prompt.
Only a user allowed to drop triggers can get round them, and that is itself
visible in the MySQL general/binary log.

MySQL only: SQLite (tests) skips this migration. The migrating user needs the
TRIGGER privilege on the database.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER audit_logs_no_{event.lower()} BEFORE {event} ON audit_logs "
            "FOR EACH ROW SIGNAL SQLSTATE '45000' "
            "SET MESSAGE_TEXT = 'audit_logs is append-only'"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "mysql":
        return
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_update")
    op.execute("DROP TRIGGER IF EXISTS audit_logs_no_delete")
