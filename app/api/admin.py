"""Campaigns, the audit log, users, and the health/dashboard endpoints."""

import csv
import io
import json
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import runtime
from ..database import get_db, ping, utcnow
from ..enums import JobStatus, Role, Status
from ..models import AuditLog, Campaign, Content, PublishJob, User
from ..schemas import CampaignIn, UserCreateIn, UserRoleIn, audit_out, campaign_out, content_card, user_out
from ..security import current_user, hash_password, password_problem, require
from ..services import channels
from ..services.audit import record

router = APIRouter(prefix="/api", tags=["admin"])

AUDITORS = (Role.COMPLIANCE, Role.ADMIN)


# ---------------------------------------------------------------------------
# Health and dashboard
# ---------------------------------------------------------------------------
@router.get("/health")
def health(db: Session = Depends(get_db)):
    """For load balancers and uptime checks: 503 when the database is unreachable."""
    db_ok = ping()
    body = {"ok": db_ok, "database": "up" if db_ok else "down", "ai_enabled": runtime.drafter.enabled,
            "ai_model": runtime.settings.groq_model,
            "channels": channels.connection_status(runtime.settings, db if db_ok else None)}
    return body if db_ok else JSONResponse(status_code=503, content=body)


@router.get("/dashboard")
def dashboard(db: Session = Depends(get_db), user: User = Depends(current_user)):
    """Counts per status, and the items waiting on *this* person's role."""
    statuses = Counter(db.scalars(select(Content.status)).all())
    queue_status = {
        Role.CREATOR: [Status.DRAFT, Status.CHANGES_REQUESTED],
        Role.REVIEWER: [Status.IN_REVIEW],
        Role.COMPLIANCE: [Status.IN_REVIEW],
        Role.PUBLISHER: [Status.APPROVED],
        Role.ADMIN: [],
    }[Role(user.role)]
    query = select(Content).where(Content.status.in_(queue_status)).order_by(Content.updated_at.desc())
    if user.role == Role.CREATOR:
        query = query.where(Content.author_id == user.id)
    if user.role in (Role.REVIEWER, Role.COMPLIANCE):
        query = query.where(Content.author_id != user.id)   # you never review your own work
    awaiting = db.scalars(select(PublishJob).where(PublishJob.status == JobStatus.AWAITING_MANUAL)).all()
    blocked = db.scalars(select(AuditLog).where(AuditLog.action.in_(
        ["publish.blocked", "content.submit_blocked", "content.approve_blocked"]))
        .order_by(AuditLog.id.desc()).limit(5)).all()
    live_alerts = db.scalars(select(Content).where(Content.live_status.in_(["changed", "removed"]))
                             .order_by(Content.live_checked_at.desc()).limit(10)).all()
    return {
        "counts": {s.value: statuses.get(s.value, 0) for s in Status},
        "live_alerts": [content_card(c) for c in live_alerts],
        "queue": [content_card(c) for c in db.scalars(query.limit(20)).all()],
        "awaiting_manual": len(awaiting),
        "recent_blocks": [audit_out(a) for a in blocked],
    }


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------
@router.get("/campaigns")
def list_campaigns(db: Session = Depends(get_db), _: User = Depends(current_user)):
    rows = db.scalars(select(Campaign).order_by(Campaign.start_date.desc())).all()
    return [campaign_out(c) for c in rows]


@router.post("/campaigns", status_code=201)
def create_campaign(data: CampaignIn, db: Session = Depends(get_db),
                    user: User = Depends(require(Role.CREATOR, Role.PUBLISHER, Role.ADMIN))):
    if data.start_date and data.end_date and data.end_date < data.start_date:
        raise HTTPException(422, "The campaign ends before it starts.")
    campaign = Campaign(**data.model_dump())
    db.add(campaign)
    db.flush()
    record(db, user, "campaign.created", "campaign", campaign.id, after=campaign_out(campaign))
    db.commit()
    return campaign_out(campaign)


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------
def _audit_query(entity: str | None, entity_id: int | None, actor_id: int | None, action: str | None):
    query = select(AuditLog).order_by(AuditLog.id.desc())
    if entity:
        query = query.where(AuditLog.entity == entity)
    if entity_id:
        query = query.where(AuditLog.entity_id == entity_id)
    if actor_id:
        query = query.where(AuditLog.actor_id == actor_id)
    if action:
        query = query.where(AuditLog.action.startswith(action))
    return query


@router.get("/audit")
def audit(entity: str | None = None, entity_id: int | None = None, actor_id: int | None = None,
          action: str | None = None, limit: int = 200,
          db: Session = Depends(get_db), _: User = Depends(require(*AUDITORS))):
    rows = db.scalars(_audit_query(entity, entity_id, actor_id, action).limit(min(limit, 1000))).all()
    # Human labels for the "Item" column: the post's title, or the person's name.
    content_ids = {a.entity_id for a in rows if a.entity == "content" and a.entity_id}
    user_ids = {a.entity_id for a in rows if a.entity == "user" and a.entity_id}
    labels = {
        "content": dict(db.execute(select(Content.id, Content.title).where(Content.id.in_(content_ids))).all()) if content_ids else {},
        "user": dict(db.execute(select(User.id, User.name).where(User.id.in_(user_ids))).all()) if user_ids else {},
    }
    return [{**audit_out(a), "entity_label": labels.get(a.entity, {}).get(a.entity_id)} for a in rows]


@router.get("/audit/export.csv")
def audit_csv(entity: str | None = None, entity_id: int | None = None, actor_id: int | None = None,
              action: str | None = None, db: Session = Depends(get_db), user: User = Depends(require(*AUDITORS))):
    """The whole trail as CSV, for regulators and internal auditors."""
    rows = db.scalars(_audit_query(entity, entity_id, actor_id, action)).all()
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(["id", "time_utc", "actor", "role", "action", "entity", "entity_id", "reason", "before", "after"])
    for a in rows:
        writer.writerow([a.id, a.created_at.isoformat(), a.actor.name if a.actor else "system",
                         a.actor.role if a.actor else "", a.action, a.entity, a.entity_id, a.reason,
                         json.dumps(a.before, ensure_ascii=False) if a.before else "",
                         json.dumps(a.after, ensure_ascii=False) if a.after else ""])
    record(db, user, "audit.exported", "audit", None, reason=f"{len(rows)} rows")
    db.commit()
    return StreamingResponse(iter([out.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=audit-log.csv"})


# ---------------------------------------------------------------------------
# Users (admin)
# ---------------------------------------------------------------------------
@router.get("/users")
def list_users(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return [user_out(u) for u in db.scalars(select(User).order_by(User.name)).all()]


@router.post("/users", status_code=201)
def create_user(data: UserCreateIn, db: Session = Depends(get_db), admin: User = Depends(require(Role.ADMIN))):
    email = data.email.lower()
    if db.scalar(select(User.id).where(User.email == email)):
        raise HTTPException(409, "Someone already has that email.")
    problem = password_problem(data.password, email)
    if problem:
        raise HTTPException(422, f"Temporary password: {problem}")
    user = User(email=email, name=data.name.strip(), role=data.role, password_hash=hash_password(data.password),
                active=True, failed_logins=0)
    db.add(user)
    db.flush()
    record(db, admin, "user.created", "user", user.id, after=user_out(user))   # never the password
    db.commit()
    return user_out(user)


@router.patch("/users/{user_id}")
def update_user(user_id: int, data: UserRoleIn, db: Session = Depends(get_db),
                admin: User = Depends(require(Role.ADMIN))):
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "No such user.")
    if target.id == admin.id:
        raise HTTPException(409, "Ask another admin to change your own account.")
    before = user_out(target)
    if data.role is not None:
        target.role = data.role
    if data.active is not None:
        target.active = data.active
    if data.password is not None:
        problem = password_problem(data.password, target.email)
        if problem:
            raise HTTPException(422, f"Temporary password: {problem}")
        target.password_hash = hash_password(data.password)
        target.password_changed_at = utcnow()   # ends their existing sessions
    if data.unlock or data.password is not None:
        target.failed_logins = 0
        target.locked_until = None
    reason = "password reset" if data.password is not None else ""
    record(db, admin, "user.updated", "user", target.id, before=before, after=user_out(target), reason=reason)
    db.commit()
    return user_out(target)
