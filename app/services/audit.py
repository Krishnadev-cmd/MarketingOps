"""The audit trail. Every state change in the console goes through `record()`.

Rows are only ever inserted. The caller commits, so the audit row and the change
it describes land in the same transaction: you never get one without the other.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ..models import AuditLog, Content, User


def record(
    db: Session,
    actor: User | None,
    action: str,
    entity: str,
    entity_id: int | None,
    before: dict | None = None,
    after: dict | None = None,
    reason: str = "",
) -> AuditLog:
    row = AuditLog(
        actor_id=actor.id if actor else None,
        action=action,
        entity=entity,
        entity_id=entity_id,
        before=before,
        after=after,
        reason=reason,
    )
    db.add(row)
    return row


def snapshot(content: Content) -> dict:
    """The fields of a content item an auditor cares about, as plain JSON."""
    return {
        "status": content.status,
        "version": content.version,
        "title": content.title,
        "body": content.body,
        "media_url": content.media_url,
        "campaign_id": content.campaign_id,
        "approved_version": content.approved_version,
        "approved_by_id": content.approved_by_id,
        "scheduled_at": content.scheduled_at.isoformat() if content.scheduled_at else None,
    }
