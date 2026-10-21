"""Content: the studio's CRUD, the workflow buttons, and the calendar feed.

Each endpoint is thin: load the row, call one workflow function, commit,
return the result. The rules live in services/workflow.py, not here.
"""

from datetime import UTC, date, datetime, time, timedelta

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .. import runtime
from ..database import get_db
from ..enums import Role
from ..models import Approval, AuditLog, Content, ContentVersion, LiveCheck, PublishJob, User
from ..schemas import (AcknowledgeIn, CommentIn, ConfirmIn, ContentEditIn, ContentIn, ScheduleIn, approval_out, audit_out, live_check_out,
                       content_card, content_out, job_out, version_out)
from ..security import current_user, require
from ..services import live_watch, workflow

router = APIRouter(prefix="/api", tags=["content"])


def load(db: Session, content_id: int) -> Content:
    content = db.get(Content, content_id)
    if content is None:
        raise HTTPException(404, "No such content.")
    return content


def detail(db: Session, content: Content) -> dict:
    """Everything the editor page needs in one response."""
    cid = content.id
    versions = db.scalars(select(ContentVersion).where(ContentVersion.content_id == cid)
                          .order_by(ContentVersion.version.desc())).all()
    approvals = db.scalars(select(Approval).where(Approval.content_id == cid).order_by(Approval.id.desc())).all()
    jobs = db.scalars(select(PublishJob).where(PublishJob.content_id == cid).order_by(PublishJob.id.desc())).all()
    audit = db.scalars(select(AuditLog).where(AuditLog.entity == "content", AuditLog.entity_id == cid)
                       .order_by(AuditLog.id.desc())).all()
    checks = db.scalars(select(LiveCheck).where(LiveCheck.content_id == cid)
                        .order_by(LiveCheck.id.desc()).limit(10)).all()
    return {"content": content_out(content), "versions": [version_out(v) for v in versions],
            "approvals": [approval_out(a) for a in approvals], "jobs": [job_out(j) for j in jobs],
            "audit": [audit_out(a) for a in audit], "live_checks": [live_check_out(c) for c in checks]}


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------
@router.get("/content")
def list_content(status: str | None = None, channel: str | None = None, campaign_id: int | None = None,
                 mine: bool = False, q: str | None = None,
                 db: Session = Depends(get_db), user: User = Depends(current_user)):
    query = select(Content).order_by(Content.updated_at.desc())
    if status:
        query = query.where(Content.status.in_(status.split(",")))
    if channel:
        query = query.where(Content.channel == channel)
    if campaign_id:
        query = query.where(Content.campaign_id == campaign_id)
    if mine:
        query = query.where(Content.author_id == user.id)
    if q:
        like = f"%{q}%"
        query = query.where(or_(Content.title.ilike(like), Content.body.ilike(like)))
    return [content_card(c) for c in db.scalars(query.limit(200)).all()]


@router.get("/calendar")
def calendar(start: date, end: date, db: Session = Depends(get_db), _: User = Depends(current_user)):
    """Everything scheduled or published between two dates (inclusive)."""
    if (end - start).days > 62:
        raise HTTPException(422, "Ask for at most two months at a time.")
    lo = datetime.combine(start, time.min, UTC)
    hi = datetime.combine(end + timedelta(days=1), time.min, UTC)
    rows = db.scalars(select(Content).where(or_(
        Content.scheduled_at.between(lo, hi), Content.published_at.between(lo, hi)))).all()
    return [content_card(c) for c in rows]


@router.get("/content/{content_id}")
def get_content(content_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return detail(db, load(db, content_id))


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------
@router.post("/content", status_code=201)
def create_content(data: ContentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    content = workflow.create(db, user, **data.model_dump())
    db.commit()
    return detail(db, content)


@router.put("/content/{content_id}")
def edit_content(content_id: int, data: ContentEditIn, db: Session = Depends(get_db),
                 user: User = Depends(current_user)):
    content = workflow.edit(db, user, load(db, content_id), **data.model_dump())
    db.commit()
    return detail(db, content)


@router.post("/content/{content_id}/restore/{version}")
def restore_version(content_id: int, version: int, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    content = workflow.restore(db, user, load(db, content_id), version)
    db.commit()
    return detail(db, content)


# ---------------------------------------------------------------------------
# Workflow buttons
# ---------------------------------------------------------------------------
@router.post("/content/{content_id}/submit")
def submit(content_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    try:
        content = workflow.submit(db, user, load(db, content_id))
    finally:
        db.commit()   # keep the "submit_blocked" audit row even when submit is refused
    return detail(db, content)


@router.post("/content/{content_id}/approve")
def approve(content_id: int, data: CommentIn, db: Session = Depends(get_db),
            user: User = Depends(current_user)):
    try:
        content = workflow.approve(db, user, load(db, content_id), data.comment)
    finally:
        db.commit()
    return detail(db, content)


@router.post("/content/{content_id}/reject")
def reject(content_id: int, data: CommentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    content = workflow.reject(db, user, load(db, content_id), data.comment)
    db.commit()
    return detail(db, content)


@router.post("/content/{content_id}/comment")
def comment(content_id: int, data: CommentIn, db: Session = Depends(get_db), user: User = Depends(current_user)):
    content = load(db, content_id)
    workflow.add_comment(db, user, content, data.comment)
    db.commit()
    return detail(db, content)


@router.post("/content/{content_id}/withdraw")
def withdraw(content_id: int, data: CommentIn, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    content = workflow.withdraw(db, user, load(db, content_id), data.comment)
    db.commit()
    return detail(db, content)


@router.post("/content/{content_id}/schedule")
def schedule(content_id: int, data: ScheduleIn, db: Session = Depends(get_db),
             user: User = Depends(current_user)):
    when = data.scheduled_at if data.scheduled_at.tzinfo else data.scheduled_at.replace(tzinfo=UTC)
    content = load(db, content_id)
    workflow.schedule(db, user, content, when)
    db.commit()
    return detail(db, content)


@router.post("/content/{content_id}/publish-now")
def publish_now(content_id: int, db: Session = Depends(get_db), user: User = Depends(current_user)):
    content = load(db, content_id)
    workflow.publish_now(db, user, content, runtime.settings)
    db.commit()
    return detail(db, content)


# ---------------------------------------------------------------------------
# Publish jobs
# ---------------------------------------------------------------------------
@router.get("/jobs")
def list_jobs(status: str | None = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    query = select(PublishJob).order_by(PublishJob.scheduled_at.desc())
    if status:
        query = query.where(PublishJob.status.in_(status.split(",")))
    jobs = db.scalars(query.limit(200)).all()
    return [{**job_out(j), "content": content_card(db.get(Content, j.content_id))} for j in jobs]


@router.post("/jobs/{job_id}/confirm")
def confirm_job(job_id: int, data: ConfirmIn, db: Session = Depends(get_db),
                user: User = Depends(require(Role.PUBLISHER))):
    job = db.get(PublishJob, job_id)
    if job is None:
        raise HTTPException(404, "No such publish job.")
    workflow.confirm_manual(db, user, job, data.url)
    db.commit()
    return detail(db, load(db, job.content_id))


# ---------------------------------------------------------------------------
# Live post watch
# ---------------------------------------------------------------------------
@router.post("/content/{content_id}/live/check")
def live_check_now(content_id: int, db: Session = Depends(get_db),
                   user: User = Depends(require(Role.COMPLIANCE, Role.PUBLISHER, Role.ADMIN))):
    """Look at the live post right now, instead of waiting for the scheduler."""
    content = load(db, content_id)
    if content.status != "published":
        raise HTTPException(409, "Only published content can be checked.")
    live_watch.check(db, content, runtime.settings, actor=user)
    db.commit()
    return detail(db, content)


@router.post("/content/{content_id}/live/acknowledge")
def live_acknowledge(content_id: int, data: AcknowledgeIn, db: Session = Depends(get_db),
                     officer: User = Depends(require(Role.COMPLIANCE))):
    """A Compliance Officer accepts a live change, with a reason that goes in the audit log."""
    content = load(db, content_id)
    if content.live_status != "changed":
        raise HTTPException(409, "There is no open live change to acknowledge.")
    live_watch.acknowledge(db, content, officer, data.comment)
    db.commit()
    return detail(db, content)
