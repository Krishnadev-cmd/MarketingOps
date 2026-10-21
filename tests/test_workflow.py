"""The approval workflow's safety rules, called directly (no HTTP)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import runtime
from app.database import utcnow
from app.enums import JobStatus, Status
from app.models import AuditLog, ComplianceRule, ContentVersion
from app.services import workflow
from app.services.workflow import WorkflowError

from .conftest import GOOD_BODY


def new(db, author, body=GOOD_BODY, channel="linkedin", type_="post"):
    c = workflow.create(db, author, channel=channel, type=type_, title="Plan early", body=body)
    db.commit()
    return c


def approved(db, team):
    c = new(db, team["creator"])
    workflow.submit(db, team["creator"], c)
    workflow.approve(db, team["compliance"], c)
    db.commit()
    return c


def actions(db, content_id):
    db.flush()
    return [a.action for a in db.scalars(select(AuditLog).where(AuditLog.entity_id == content_id)
                                         .order_by(AuditLog.id)).all()]


# --- drafting ---------------------------------------------------------------
def test_only_creators_draft(db, team):
    with pytest.raises(WorkflowError):
        new(db, team["publisher"])


def test_channel_and_type_must_match(db, team):
    with pytest.raises(WorkflowError):
        workflow.create(db, team["creator"], channel="linkedin", type="banner", title="x", body="y")


def test_every_edit_is_a_new_version(db, team):
    c = new(db, team["creator"])
    workflow.edit(db, team["creator"], c, title="Plan early", body=GOOD_BODY + " More.")
    workflow.edit(db, team["creator"], c, title="Plan early", body=GOOD_BODY + " More.")  # no change
    db.commit()
    versions = db.scalars(select(ContentVersion).where(ContentVersion.content_id == c.id)).all()
    assert c.version == 2 and len(versions) == 2


def test_only_the_author_edits(db, team):
    c = new(db, team["creator"])
    with pytest.raises(WorkflowError):
        workflow.edit(db, team["creator2"], c, title="Mine now", body=GOOD_BODY)


def test_restore_makes_a_new_version(db, team):
    c = new(db, team["creator"])
    workflow.edit(db, team["creator"], c, title="Changed", body=GOOD_BODY)
    workflow.restore(db, team["creator"], c, 1)
    assert c.version == 3 and c.title == "Plan early"


# --- review -----------------------------------------------------------------
def test_submit_is_blocked_by_violations_and_the_reason_is_logged(db, team):
    c = new(db, team["creator"], body=f"Guaranteed growth. {GOOD_BODY}")
    with pytest.raises(WorkflowError) as err:
        workflow.submit(db, team["creator"], c)
    db.commit()
    assert err.value.report["blocking"] == 1
    assert c.status == Status.DRAFT
    log = db.scalar(select(AuditLog).where(AuditLog.action == "content.submit_blocked"))
    assert "Guaranteed" in log.reason


def test_creator_cannot_approve(db, team):
    c = new(db, team["creator"])
    workflow.submit(db, team["creator"], c)
    with pytest.raises(WorkflowError):
        workflow.approve(db, team["creator"], c)


def test_reviewer_cannot_approve_but_can_request_changes(db, team):
    c = new(db, team["creator"])
    workflow.submit(db, team["creator"], c)
    with pytest.raises(WorkflowError):
        workflow.approve(db, team["reviewer"], c)
    workflow.reject(db, team["reviewer"], c, "Tone is off.")
    assert c.status == Status.CHANGES_REQUESTED


def test_compliance_officer_cannot_approve_own_content(db, team):
    # Even if someone's role later changes, an approver can never be the author.
    c = new(db, team["creator"])
    workflow.submit(db, team["creator"], c)
    team["creator"].role = "compliance_officer"
    with pytest.raises(WorkflowError, match="your own"):
        workflow.approve(db, team["creator"], c)


def test_reject_needs_a_reason(db, team):
    c = new(db, team["creator"])
    workflow.submit(db, team["creator"], c)
    with pytest.raises(WorkflowError):
        workflow.reject(db, team["compliance"], c, "  ")


def test_approval_names_the_approver_and_version(db, team):
    c = approved(db, team)
    assert c.status == Status.APPROVED
    assert c.approved_by_id == team["compliance"].id and c.approved_version == c.version


def test_approved_content_is_locked(db, team):
    c = approved(db, team)
    with pytest.raises(WorkflowError, match="locked"):
        workflow.edit(db, team["creator"], c, title="Sneaky", body=GOOD_BODY)


def test_withdraw_voids_the_approval(db, team):
    c = approved(db, team)
    workflow.withdraw(db, team["creator"], c)
    assert c.status == Status.DRAFT and c.approved_by_id is None


# --- publishing -------------------------------------------------------------
def test_only_publishers_schedule_and_only_approved_content(db, team):
    c = new(db, team["creator"])
    with pytest.raises(WorkflowError):
        workflow.schedule(db, team["publisher"], c, utcnow() + timedelta(days=1))
    c = approved(db, team)
    with pytest.raises(WorkflowError):
        workflow.schedule(db, team["creator"], c, utcnow() + timedelta(days=1))


def test_manual_channel_waits_for_a_person_then_publishes(db, team):
    c = approved(db, team)
    job = workflow.publish_now(db, team["publisher"], c, runtime.settings)
    assert job.status == JobStatus.AWAITING_MANUAL and c.status == Status.SCHEDULED
    workflow.confirm_manual(db, team["publisher"], job, "https://linkedin.example/post/1")
    assert c.status == Status.PUBLISHED and c.published_url.endswith("/1")
    assert actions(db, c.id)[-1] == "content.published"


def test_scheduler_runs_only_due_jobs(db, team):
    c = approved(db, team)
    workflow.schedule(db, team["publisher"], c, utcnow() + timedelta(hours=2))
    db.commit()
    assert workflow.run_due_jobs(db, runtime.settings) == 0


def test_new_rule_blocks_publish_of_already_approved_content(db, team):
    """The last gate: today's rules apply at publish time, and the reason is logged."""
    c = approved(db, team)
    job = workflow.schedule(db, team["publisher"], c, utcnow())
    db.add(ComplianceRule(kind="prohibited", text="plan early", severity="block", reason="New guidance."))
    db.commit()

    workflow.run_job(db, job, runtime.settings)
    db.commit()
    assert job.status == JobStatus.BLOCKED and "plan early" in job.result.lower()
    assert c.status == Status.CHANGES_REQUESTED and c.approved_by_id is None
    assert "publish.blocked" in actions(db, c.id)


def test_tampered_version_cannot_publish(db, team):
    c = approved(db, team)
    job = workflow.schedule(db, team["publisher"], c, utcnow())
    c.version += 1          # an edit that bypassed the workflow
    workflow.run_job(db, job, runtime.settings)
    assert job.status == JobStatus.BLOCKED


def test_withdraw_cancels_the_scheduled_job(db, team):
    c = approved(db, team)
    job = workflow.schedule(db, team["publisher"], c, utcnow() + timedelta(days=1))
    workflow.withdraw(db, team["publisher"], c, "Campaign paused.")
    assert job.status == JobStatus.CANCELLED


def test_full_trail_is_audited(db, team):
    c = approved(db, team)
    job = workflow.publish_now(db, team["publisher"], c, runtime.settings)
    workflow.confirm_manual(db, team["publisher"], job, "")
    db.commit()
    assert actions(db, c.id) == ["content.created", "content.submitted", "content.approved",
                                 "content.scheduled", "publish.awaiting_manual", "content.published"]
