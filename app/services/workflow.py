"""The approval workflow. The only code allowed to change a content item's status.

    draft ──submit──▶ in_review ──approve──▶ approved ──schedule──▶ scheduled ──▶ published
      ▲                  │                      │                       │
      │               reject                    └────── withdraw ───────┘
      └── changes_requested ◀──┘                          (back to draft)

The rules that make it safe:

  1. Only the author edits, and only in draft or changes_requested.
  2. Submitting runs the compliance checker; blocking findings stop it.
  3. Only a Compliance Officer approves, and never their own content.
  4. An approval belongs to one version. Any edit means a new review.
  5. Publishing re-checks everything (status, named approver, version, compliance
     against today's rules). If anything fails, nothing goes out and the reason
     is written to the audit log.

Every function here records an audit row. The API layer commits.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import utcnow
from ..enums import CHANNEL_TYPES, Channel, ContentType, Decision, JobStatus, Role, Status
from ..models import Approval, ComplianceRule, Content, ContentVersion, PublishJob, User
from . import channels, compliance
from .audit import record, snapshot

EDITABLE = {Status.DRAFT, Status.CHANGES_REQUESTED}


class WorkflowError(Exception):
    """A rule said no. `status_code` tells the API which HTTP error to return."""

    def __init__(self, message: str, status_code: int = 409, report: dict | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.report = report


# ---------------------------------------------------------------------------
# Compliance
# ---------------------------------------------------------------------------
def active_rules(db: Session) -> list[compliance.Rule]:
    rows = db.scalars(select(ComplianceRule).where(ComplianceRule.active.is_(True))).all()
    return [compliance.Rule(id=r.id, kind=r.kind, text=r.text, severity=r.severity,
                            channels=r.channels or [], reason=r.reason) for r in rows]


def check(db: Session, content: Content) -> dict:
    """Scan with today's rules and remember the report on the content row."""
    report = compliance.scan(content.title, content.body, content.channel, content.type, active_rules(db))
    content.compliance = report
    return report


def approval_is_valid(content: Content) -> bool:
    """A named approver who is not the author, for exactly the current version."""
    return (
        content.approved_by_id is not None
        and content.approved_by_id != content.author_id
        and content.approved_version == content.version
    )


def _clear_approval(content: Content) -> None:
    content.approved_version = None
    content.approved_by_id = None
    content.approved_at = None


def _cancel_pending_jobs(db: Session, content: Content, why: str) -> None:
    jobs = db.scalars(select(PublishJob).where(
        PublishJob.content_id == content.id,
        PublishJob.status.in_([JobStatus.PENDING, JobStatus.AWAITING_MANUAL]))).all()
    for job in jobs:
        job.status = JobStatus.CANCELLED
        job.result = why
        job.finished_at = utcnow()


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------
def create(db: Session, user: User, *, channel: str, type: str, title: str, body: str,
           media_url: str = "", campaign_id: int | None = None, note: str = "") -> Content:
    if user.role != Role.CREATOR:
        raise WorkflowError("Only creators draft content.", 403)
    if ContentType(type) not in CHANNEL_TYPES[Channel(channel)]:
        raise WorkflowError(f"A {type} cannot go on {channel}.", 422)

    content = Content(channel=channel, type=type, title=title, body=body, media_url=media_url,
                      campaign_id=campaign_id, status=Status.DRAFT, author_id=user.id, version=1)
    db.add(content)
    check(db, content)
    db.flush()   # gives content.id
    db.add(ContentVersion(content_id=content.id, version=1, title=title, body=body,
                          media_url=media_url, editor_id=user.id, note=note or "Created"))
    record(db, user, "content.created", "content", content.id, after=snapshot(content))
    return content


def edit(db: Session, user: User, content: Content, *, title: str, body: str,
         media_url: str = "", campaign_id: int | None = None, note: str = "") -> Content:
    if content.author_id != user.id:
        raise WorkflowError("Only the author can edit this.", 403)
    if content.status not in EDITABLE:
        raise WorkflowError("Approved or scheduled content is locked. Withdraw it to edit.")
    if (title, body, media_url, campaign_id) == (content.title, content.body, content.media_url, content.campaign_id):
        return content   # nothing changed: no new version, no audit noise

    before = snapshot(content)
    content.title, content.body, content.media_url, content.campaign_id = title, body, media_url, campaign_id
    content.version += 1
    check(db, content)
    db.add(ContentVersion(content_id=content.id, version=content.version, title=title, body=body,
                          media_url=media_url, editor_id=user.id, note=note))
    record(db, user, "content.edited", "content", content.id, before=before, after=snapshot(content))
    return content


def restore(db: Session, user: User, content: Content, version: int) -> Content:
    old = db.scalar(select(ContentVersion).where(ContentVersion.content_id == content.id,
                                                 ContentVersion.version == version))
    if old is None:
        raise WorkflowError(f"Version {version} does not exist.", 404)
    return edit(db, user, content, title=old.title, body=old.body, media_url=old.media_url,
                campaign_id=content.campaign_id, note=f"Restored v{version}")


# ---------------------------------------------------------------------------
# Review
# ---------------------------------------------------------------------------
def submit(db: Session, user: User, content: Content) -> Content:
    if content.author_id != user.id:
        raise WorkflowError("Only the author can submit this for review.", 403)
    if content.status not in EDITABLE:
        raise WorkflowError(f"Content in {content.status} cannot be submitted.")

    report = check(db, content)
    if not report["passed"]:
        reason = compliance.summary(report)
        record(db, user, "content.submit_blocked", "content", content.id, reason=reason)
        raise WorkflowError("Fix the compliance issues before submitting.", 422, report)

    before = snapshot(content)
    content.status = Status.IN_REVIEW
    record(db, user, "content.submitted", "content", content.id, before=before, after=snapshot(content))
    return content


def approve(db: Session, user: User, content: Content, comment: str = "") -> Content:
    if user.role != Role.COMPLIANCE:
        raise WorkflowError("Only a Compliance Officer can approve.", 403)
    if user.id == content.author_id:
        raise WorkflowError("You cannot approve your own content.", 403)
    if content.status != Status.IN_REVIEW:
        raise WorkflowError("Only content in compliance review can be approved.")

    # Rules may have changed since it was submitted.
    report = check(db, content)
    if not report["passed"]:
        record(db, user, "content.approve_blocked", "content", content.id, reason=compliance.summary(report))
        raise WorkflowError("The checker now finds blocking issues. Reject it with a reason instead.", 422, report)

    before = snapshot(content)
    content.status = Status.APPROVED
    content.approved_version = content.version
    content.approved_by_id = user.id
    content.approved_at = utcnow()
    db.add(Approval(content_id=content.id, version=content.version, reviewer_id=user.id,
                    decision=Decision.APPROVED, comment=comment))
    record(db, user, "content.approved", "content", content.id, before=before, after=snapshot(content),
           reason=comment)
    return content


def reject(db: Session, user: User, content: Content, comment: str) -> Content:
    if user.role not in (Role.COMPLIANCE, Role.REVIEWER):
        raise WorkflowError("Only reviewers and Compliance Officers can request changes.", 403)
    if user.id == content.author_id:
        raise WorkflowError("You cannot review your own content.", 403)
    if content.status != Status.IN_REVIEW:
        raise WorkflowError("Only content in compliance review can be rejected.")
    if not comment.strip():
        raise WorkflowError("Say what needs to change.", 422)

    before = snapshot(content)
    content.status = Status.CHANGES_REQUESTED
    db.add(Approval(content_id=content.id, version=content.version, reviewer_id=user.id,
                    decision=Decision.REJECTED, comment=comment))
    record(db, user, "content.rejected", "content", content.id, before=before, after=snapshot(content),
           reason=comment)
    return content


def add_comment(db: Session, user: User, content: Content, comment: str) -> Approval:
    if not comment.strip():
        raise WorkflowError("Write a comment first.", 422)
    note = Approval(content_id=content.id, version=content.version, reviewer_id=user.id,
                    decision=Decision.COMMENT, comment=comment)
    db.add(note)
    record(db, user, "content.commented", "content", content.id, reason=comment)
    return note


def withdraw(db: Session, user: User, content: Content, reason: str = "") -> Content:
    """Pull content back to draft. Voids the approval and cancels any scheduled publish."""
    if user.id != content.author_id and user.role != Role.PUBLISHER:
        raise WorkflowError("Only the author or a publisher can withdraw this.", 403)
    if content.status not in (Status.IN_REVIEW, Status.APPROVED, Status.SCHEDULED):
        raise WorkflowError(f"Content in {content.status} cannot be withdrawn.")

    before = snapshot(content)
    content.status = Status.DRAFT
    content.scheduled_at = None
    _clear_approval(content)
    _cancel_pending_jobs(db, content, "Withdrawn to draft.")
    record(db, user, "content.withdrawn", "content", content.id, before=before, after=snapshot(content),
           reason=reason)
    return content


# ---------------------------------------------------------------------------
# Publishing
# ---------------------------------------------------------------------------
def schedule(db: Session, user: User, content: Content, when: datetime) -> PublishJob:
    if user.role != Role.PUBLISHER:
        raise WorkflowError("Only publishers can schedule.", 403)
    if content.status not in (Status.APPROVED, Status.SCHEDULED):
        raise WorkflowError("Only approved content can be scheduled.")
    if not approval_is_valid(content):
        raise WorkflowError("This version has no valid approval. Send it back for review.")
    if when < utcnow() - timedelta(minutes=1):
        raise WorkflowError("Pick a time in the future.", 422)

    _cancel_pending_jobs(db, content, "Rescheduled.")
    if db.scalar(select(PublishJob.id).where(PublishJob.content_id == content.id,
                                             PublishJob.status == JobStatus.PUBLISHING)):
        raise WorkflowError("This is being published right now. Try again in a moment.")
    before = snapshot(content)
    content.status = Status.SCHEDULED
    content.scheduled_at = when
    job = PublishJob(content_id=content.id, channel=content.channel, scheduled_at=when,
                     status=JobStatus.PENDING, created_by_id=user.id)
    db.add(job)
    db.flush()
    record(db, user, "content.scheduled", "content", content.id, before=before, after=snapshot(content))
    return job


def run_job(db: Session, job: PublishJob, settings: Settings, actor: User | None = None) -> PublishJob:
    """Publish one job. Called by the scheduler when it is due, or by "Publish now"."""
    if not claim(db, job):
        return job   # another process (or click) got there first: never publish twice
    content = db.get(Content, job.content_id)

    # The last gate. Everything is checked again, at the moment of publishing.
    problems = []
    if content.status != Status.SCHEDULED:
        problems.append(f"Content is {content.status}, not scheduled.")
    if not approval_is_valid(content):
        problems.append("No valid approval for this version.")
    report = check(db, content)
    if not report["passed"]:
        problems.append(compliance.summary(report))

    if problems:
        reason = " ".join(problems)
        before = snapshot(content)
        job.status, job.result, job.finished_at = JobStatus.BLOCKED, reason, utcnow()
        # Approved text that fails today's rules has to be reworked and reviewed again.
        content.status = Status.CHANGES_REQUESTED
        content.scheduled_at = None
        _clear_approval(content)
        record(db, actor, "publish.blocked", "content", content.id, before=before,
               after=snapshot(content), reason=reason)
        return job

    try:
        result = channels.publish(content, settings, db)
    except Exception as exc:   # ChannelError or a network error: record it, never kill the scheduler
        job.status, job.result, job.finished_at = JobStatus.FAILED, str(exc)[:1000], utcnow()
        record(db, actor, "publish.failed", "content", content.id, reason=job.result)
        return job

    job.mode = result.mode
    job.result = result.message
    if result.mode == "manual":
        job.status = JobStatus.AWAITING_MANUAL
        record(db, actor, "publish.awaiting_manual", "content", content.id, reason=result.message)
    else:
        _mark_published(db, actor, content, job, result.url)
    return job


def claim(db: Session, job: PublishJob) -> bool:
    """Atomically move a job from pending to publishing. True = it is ours to run.

    Several API processes run the scheduler, and a publisher can click "Publish
    now" at the same moment. A plain "if job.status == pending" would let two of
    them through. One conditional UPDATE cannot: MySQL row-locks the job, and
    only the first statement finds it still pending (rowcount 1); everyone else
    finds 0 rows and walks away.
    """
    result = db.execute(
        update(PublishJob)
        .where(PublishJob.id == job.id, PublishJob.status == JobStatus.PENDING)
        .values(status=JobStatus.PUBLISHING)
        .execution_options(synchronize_session=False)
    )
    if result.rowcount != 1:
        return False
    db.refresh(job)
    return True


def confirm_manual(db: Session, user: User, job: PublishJob, url: str) -> PublishJob:
    """The publisher posted it by hand and tells us where."""
    if user.role != Role.PUBLISHER:
        raise WorkflowError("Only publishers can confirm a publish.", 403)
    if job.status != JobStatus.AWAITING_MANUAL:
        raise WorkflowError("This job is not waiting for a manual post.")
    content = db.get(Content, job.content_id)
    if not approval_is_valid(content):
        raise WorkflowError("The approval is no longer valid. Do not post this.")
    _mark_published(db, user, content, job, url.strip())
    return job


def publish_now(db: Session, user: User, content: Content, settings: Settings) -> PublishJob:
    job = schedule(db, user, content, utcnow())
    return run_job(db, job, settings, actor=user)


def run_due_jobs(db: Session, settings: Settings) -> int:
    """The scheduler's tick: run every pending job whose time has come."""
    # A process that died mid-publish leaves its job in "publishing". After 10
    # minutes, say so plainly instead of leaving it stuck. We do not retry by
    # ourselves: the post may already be live, so a person must check first.
    stuck = db.scalars(select(PublishJob).where(PublishJob.status == JobStatus.PUBLISHING,
                                                PublishJob.scheduled_at <= utcnow() - timedelta(minutes=10))).all()
    for job in stuck:
        job.status, job.finished_at = JobStatus.FAILED, utcnow()
        job.result = "Interrupted while publishing. Check the channel before scheduling it again."
        record(db, None, "publish.failed", "content", job.content_id, reason=job.result)

    due = db.scalars(select(PublishJob).where(PublishJob.status == JobStatus.PENDING,
                                              PublishJob.scheduled_at <= utcnow())).all()
    for job in due:
        run_job(db, job, settings)
    return len(due)


def _mark_published(db: Session, actor: User | None, content: Content, job: PublishJob, url: str) -> None:
    before = snapshot(content)
    now = utcnow()
    job.status, job.finished_at, job.external_url = JobStatus.SUCCEEDED, now, url
    job.finished_by_id = actor.id if actor else None
    content.status = Status.PUBLISHED
    content.published_at = now
    content.published_url = url
    content.live_status, content.live_checked_at, content.live_acked_hash = None, None, ""  # watch starts afresh
    record(db, actor, "content.published", "content", content.id, before=before, after=snapshot(content))
