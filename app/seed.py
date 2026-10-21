"""First-run data: the starter compliance rules, plus an optional demo team.

Rules are always seeded on an empty database, because a console with no rules
would happily approve "guaranteed returns". The demo team and sample content
only appear when SEED_DEMO_DATA=true. Everything goes through the workflow
functions, so the demo has real versions, approvals and audit rows.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import Settings
from .database import utcnow
from .enums import Role
from .models import Campaign, ComplianceRule, User
from .security import hash_password
from .services import workflow

log = logging.getLogger(__name__)

MAIN_DISCLAIMER = "Investments are subject to market risks. Read all related documents carefully before investing."

STARTER_RULES = [
    # kind, text, severity, channels, reason
    ("prohibited", "guarantee*", "block", [], "Returns can never be promised."),
    ("prohibited", "risk-free", "block", [], "No investment is free of risk."),
    ("prohibited", "assured return*", "block", [], "Returns can never be assured."),
    ("prohibited", "double your money", "block", [], "Implies a promised outcome."),
    ("prohibited", "zero risk", "block", [], "No investment is free of risk."),
    ("prohibited", "no risk", "block", [], "No investment is free of risk."),
    ("prohibited", "best returns", "warn", [], "Superlatives need a cited, dated source."),
    ("prohibited", "safe investment", "warn", [], "Avoid implying safety of capital."),
    ("prohibited", "beat the market", "warn", [], "Performance claims need evidence."),
    ("prohibited", "limited time only", "warn", [], "Urgency language draws regulator attention."),
    ("disclaimer", MAIN_DISCLAIMER, "block", [], "Required on every investment promotion."),
    ("disclaimer", "Past performance is not indicative of future results.", "warn",
     ["linkedin", "website"], "Recommended whenever performance is mentioned."),
]

DEMO_TEAM = [
    ("priya.raman@meridianwealth.example", "Priya Raman", Role.CREATOR),
    ("tomas.becker@meridianwealth.example", "Tomás Becker", Role.CREATOR),
    ("daniel.okafor@meridianwealth.example", "Daniel Okafor", Role.REVIEWER),
    ("meera.iyer@meridianwealth.example", "Meera Iyer", Role.COMPLIANCE),
    ("arjun.mehta@meridianwealth.example", "Arjun Mehta", Role.PUBLISHER),
    ("sofia.lindqvist@meridianwealth.example", "Sofia Lindqvist", Role.ADMIN),
]


def seed(db: Session, settings: Settings) -> None:
    if db.scalar(select(func.count(ComplianceRule.id))) == 0:
        for kind, text, severity, channels, reason in STARTER_RULES:
            db.add(ComplianceRule(kind=kind, text=text, severity=severity, channels=channels, reason=reason))
        db.commit()
        log.info("Seeded %d starter compliance rules", len(STARTER_RULES))

    if settings.seed_demo_data and db.scalar(select(func.count(User.id))) == 0:
        if not settings.demo_password:
            log.warning("SEED_DEMO_DATA=true but DEMO_PASSWORD is empty: skipping the demo team")
            return
        _seed_demo(db, settings)
        db.commit()
        log.info("Seeded the demo team and sample content")


def _seed_demo(db: Session, settings: Settings) -> None:
    users = {}
    for email, name, role in DEMO_TEAM:
        user = User(email=email, name=name, role=role, password_hash=hash_password(settings.demo_password))
        db.add(user)
        users[name.split()[0].lower()] = user
    db.flush()
    priya, tomas, daniel = users["priya"], users["tomás"], users["daniel"]
    meera, arjun = users["meera"], users["arjun"]

    today = date.today()
    retire = Campaign(name="Retire Ready 2027", goal="Book 400 retirement planning calls before March.",
                      start_date=today - timedelta(days=10), end_date=today + timedelta(days=50),
                      channels=["linkedin", "whatsapp", "website"], color="teal")
    sip = Campaign(name="SIP Awareness November", goal="Explain monthly investing to first time investors.",
                   start_date=today, end_date=today + timedelta(days=30),
                   channels=["linkedin", "whatsapp"], color="amber")
    db.add_all([retire, sip])
    db.flush()

    # 1. A draft that the checker blocks: the writer sees exactly why.
    workflow.create(db, priya, channel="linkedin", type="post", campaign_id=sip.id,
                    title="SIP explainer for first time investors",
                    body="Start a SIP with just ₹500 a month and enjoy guaranteed growth. "
                         "It is the risk-free way to build wealth for your family.")

    # 2. Waiting for compliance review.
    c2 = workflow.create(db, tomas, channel="linkedin", type="post", campaign_id=retire.id,
                         title="Retirement gap: what a 35 year old should know",
                         body="Most people underestimate how long retirement lasts. A plan that starts at 35 "
                              "has 25 years of compounding on its side.\n\nBook a free planning call with our "
                              f"advisers this week.\n\n{MAIN_DISCLAIMER}")
    workflow.submit(db, tomas, c2)
    workflow.add_comment(db, daniel, c2, "Reads well. Could we name the call booking page?")

    # 3. Rejected with a reason, back with the author.
    c3 = workflow.create(db, priya, channel="whatsapp", type="status", campaign_id=sip.id,
                         title="SIP day reminder (Status)",
                         body="Your SIP builds discipline. Our clients beat the market by staying invested.\n\n"
                              f"{MAIN_DISCLAIMER}")
    workflow.submit(db, priya, c3)
    workflow.reject(db, meera, c3, "Remove “beat the market”: we cannot evidence it for all clients.")

    # 4. Approved, ready for the publisher.
    c4 = workflow.create(db, tomas, channel="whatsapp", type="status", campaign_id=retire.id,
                         title="Retirement calculator launch (Status)",
                         body="How much will you need at 60? Try our retirement calculator in under two minutes. "
                              f"Link in our profile.\n\n{MAIN_DISCLAIMER}")
    workflow.submit(db, tomas, c4)
    workflow.approve(db, meera, c4, "Clear and balanced.")

    # 5. Scheduled on the website in three days.
    c5 = workflow.create(db, priya, channel="website", type="hero", campaign_id=retire.id,
                         title="Plan the retirement you actually want",
                         body="Talk to a certified adviser about income, healthcare and the years in between. "
                              f"The first call is free.\n\n{MAIN_DISCLAIMER}")
    workflow.submit(db, priya, c5)
    workflow.approve(db, meera, c5)
    when = utcnow().replace(hour=4, minute=30, second=0, microsecond=0) + timedelta(days=3)
    workflow.schedule(db, arjun, c5, when)

    # 6. Published last week, posted by hand on LinkedIn.
    c6 = workflow.create(db, tomas, channel="linkedin", type="post", campaign_id=retire.id,
                         title="Retire Ready launch post",
                         body="Retire Ready 2027 starts today: free planning calls, a new calculator and a "
                              f"weekly myth buster series.\n\n{MAIN_DISCLAIMER}")
    workflow.submit(db, tomas, c6)
    workflow.approve(db, meera, c6)
    job = workflow.publish_now(db, arjun, c6, settings)
    workflow.confirm_manual(db, arjun, job, "https://www.linkedin.com/company/meridian-wealth/posts/")
    week_ago = utcnow() - timedelta(days=6)
    c6.scheduled_at = c6.published_at = job.scheduled_at = job.finished_at = week_ago
