"""Request bodies (what the API accepts) and plain dict serializers (what it returns).

Pydantic validates what comes in. Going out, small functions turn rows into
dicts: easy to read, and the frontend's types in src/lib/api.ts mirror them.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, EmailStr, Field

from .database import utcnow
from .enums import Channel, ContentType, Role
from .models import (Approval, AuditLog, Campaign, ComplianceRule, Content, ContentVersion, LiveCheck,
                     PublishJob, User)


# ---------------------------------------------------------------------------
# In
# ---------------------------------------------------------------------------
class LoginIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)


class RegisterIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: EmailStr
    password: str = Field(min_length=1)


class ContentIn(BaseModel):
    channel: Channel
    type: ContentType
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=5000)
    media_url: str = Field(default="", max_length=500)
    campaign_id: int | None = None
    note: str = Field(default="", max_length=255)


class ContentEditIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=5000)
    media_url: str = Field(default="", max_length=500)
    campaign_id: int | None = None
    note: str = Field(default="", max_length=255)


class CommentIn(BaseModel):
    comment: str = Field(default="", max_length=2000)


class ScheduleIn(BaseModel):
    scheduled_at: datetime


class ConfirmIn(BaseModel):
    url: str = Field(default="", max_length=500)


class ScanIn(BaseModel):
    title: str = ""
    body: str = ""
    channel: Channel
    type: ContentType


class DraftIn(BaseModel):
    brief: str = Field(min_length=10, max_length=2000)
    channel: Channel
    type: ContentType


class FixIn(BaseModel):
    title: str
    body: str
    channel: Channel
    type: ContentType


class CampaignIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    goal: str = Field(default="", max_length=1000)
    start_date: date | None = None
    end_date: date | None = None
    channels: list[Channel] = []
    color: str = Field(default="teal", pattern="^(teal|amber|rose|sky|violet|lime)$")


class RuleIn(BaseModel):
    kind: str = Field(pattern="^(prohibited|disclaimer)$")
    text: str = Field(min_length=2, max_length=500)
    severity: str = Field(default="block", pattern="^(block|warn)$")
    channels: list[Channel] = []
    reason: str = Field(default="", max_length=255)
    active: bool = True


class UserRoleIn(BaseModel):
    role: Role | None = None
    active: bool | None = None
    password: str | None = Field(default=None, max_length=128)  # admin reset; also unlocks
    unlock: bool = False


class UserCreateIn(BaseModel):
    email: EmailStr
    name: str = Field(min_length=2, max_length=120)
    role: Role
    password: str = Field(max_length=128)   # temporary; they change it after first sign in


class PasswordChangeIn(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(max_length=128)


# ---------------------------------------------------------------------------
# Out
# ---------------------------------------------------------------------------
def iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value else None


def person(user: User | None) -> dict | None:
    if user is None:
        return None
    return {"id": user.id, "name": user.name, "role": user.role}


def user_out(user: User) -> dict:
    locked = bool(user.locked_until and user.locked_until > utcnow())
    return {**person(user), "email": user.email, "active": user.active, "locked": locked}


def campaign_out(c: Campaign) -> dict:
    return {"id": c.id, "name": c.name, "goal": c.goal, "start_date": iso(c.start_date),
            "end_date": iso(c.end_date), "channels": c.channels or [], "color": c.color}


def content_card(c: Content) -> dict:
    """The short form, for lists and the calendar."""
    report = c.compliance or {}
    return {
        "id": c.id, "channel": c.channel, "type": c.type, "title": c.title, "status": c.status,
        "version": c.version, "author": person(c.author),
        "campaign": campaign_out(c.campaign) if c.campaign else None,
        "scheduled_at": iso(c.scheduled_at), "published_at": iso(c.published_at),
        "compliance": {"passed": report.get("passed", True), "blocking": report.get("blocking", 0),
                       "warnings": report.get("warnings", 0)},
        "updated_at": iso(c.updated_at),
        "live_status": c.live_status, "live_checked_at": iso(c.live_checked_at),
    }


def content_out(c: Content) -> dict:
    return {
        **content_card(c),
        "body": c.body, "media_url": c.media_url,
        "approved_version": c.approved_version, "approved_by": person(c.approved_by),
        "approved_at": iso(c.approved_at), "published_url": c.published_url,
        "compliance": c.compliance or {},
        "created_at": iso(c.created_at),
    }


def version_out(v: ContentVersion) -> dict:
    return {"version": v.version, "title": v.title, "body": v.body, "media_url": v.media_url,
            "editor": person(v.editor), "note": v.note, "created_at": iso(v.created_at)}


def approval_out(a: Approval) -> dict:
    return {"id": a.id, "version": a.version, "reviewer": person(a.reviewer), "decision": a.decision,
            "comment": a.comment, "created_at": iso(a.created_at)}


class AcknowledgeIn(BaseModel):
    comment: str = Field(min_length=3, max_length=1000)


def live_check_out(c: LiveCheck) -> dict:
    return {"id": c.id, "checked_at": iso(c.checked_at), "outcome": c.outcome, "detail": c.detail,
            "live_text": c.live_text, "findings": c.findings, "by_scheduler": c.checked_by_id is None}


def job_out(j: PublishJob) -> dict:
    return {"id": j.id, "content_id": j.content_id, "channel": j.channel, "status": j.status, "mode": j.mode,
            "scheduled_at": iso(j.scheduled_at), "result": j.result, "external_url": j.external_url,
            "finished_at": iso(j.finished_at)}


def rule_out(r: ComplianceRule) -> dict:
    return {"id": r.id, "kind": r.kind, "text": r.text, "severity": r.severity, "channels": r.channels or [],
            "reason": r.reason, "active": r.active}


def audit_out(a: AuditLog) -> dict:
    return {"id": a.id, "actor": person(a.actor), "action": a.action, "entity": a.entity,
            "entity_id": a.entity_id, "before": a.before, "after": a.after, "reason": a.reason,
            "created_at": iso(a.created_at)}
