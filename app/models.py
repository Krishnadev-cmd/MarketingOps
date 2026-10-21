"""The tables. One class per table, plain columns, no clever inheritance.

    User ─┬─ Content ──┬── ContentVersion   every saved edit, numbered 1, 2, 3...
          │            ├── Approval          every review decision, tied to a version
          │            └── PublishJob        one row per scheduled publish attempt
          │
    Campaign ── Content
    ComplianceRule                            prohibited phrases + required disclaimers
    LiveCheck                                 each look at a published post vs. what was approved
    ChannelConnection                         one signed-in account per channel (tokens encrypted)
    AuditLog                                  who changed what, before and after
"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base, UTCDateTime, utcnow


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    role: Mapped[str] = mapped_column(String(32))
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Brute-force protection: wrong passwords in a row, and when the lock lifts.
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    password_changed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    goal: Mapped[str] = mapped_column(Text, default="")
    start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    channels: Mapped[list] = mapped_column(JSON, default=list)
    color: Mapped[str] = mapped_column(String(16), default="teal")  # calendar tag colour
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Content(Base):
    __tablename__ = "contents"

    id: Mapped[int] = mapped_column(primary_key=True)
    channel: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(32))
    title: Mapped[str] = mapped_column(String(200))   # headline, or an internal name for posts
    body: Mapped[str] = mapped_column(Text, default="")
    media_url: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(32), index=True)
    author_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)

    # Bumped on every saved edit. An approval is only valid for the version it approved.
    version: Mapped[int] = mapped_column(Integer, default=1)
    approved_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    approved_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)

    scheduled_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True, index=True)
    published_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    published_url: Mapped[str] = mapped_column(String(500), default="")

    # The latest compliance report, so lists can show a badge without re-scanning.
    compliance: Mapped[dict] = mapped_column(JSON, default=dict)

    # Live post watch: is what's on the platform still what was approved?
    # match | changed | removed | acknowledged | unverifiable | not_watchable (None = not checked yet)
    live_status: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    live_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    # Fingerprint of a changed live text a Compliance Officer reviewed and accepted.
    live_acked_hash: Mapped[str] = mapped_column(String(64), default="")

    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    author: Mapped[User] = relationship(foreign_keys=[author_id])
    approved_by: Mapped[User | None] = relationship(foreign_keys=[approved_by_id])
    campaign: Mapped[Campaign | None] = relationship()


class ContentVersion(Base):
    __tablename__ = "content_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("contents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    media_url: Mapped[str] = mapped_column(String(500), default="")
    editor_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    note: Mapped[str] = mapped_column(String(255), default="")  # "AI draft", "Restored v2"...
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    editor: Mapped[User] = relationship()


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("contents.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)

    reviewer: Mapped[User] = relationship()


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("contents.id"), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    scheduled_at: Mapped[datetime] = mapped_column(UTCDateTime, index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="manual")  # "api" or "manual"
    result: Mapped[str] = mapped_column(Text, default="")            # what happened, in words
    external_url: Mapped[str] = mapped_column(String(500), default="")
    created_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    finished_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class ComplianceRule(Base):
    __tablename__ = "compliance_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    # "prohibited" = phrase must not appear.  "disclaimer" = text must appear.
    kind: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(String(500))
    severity: Mapped[str] = mapped_column(String(8), default="block")  # "block" or "warn"
    channels: Mapped[list] = mapped_column(JSON, default=list)         # empty = every channel
    reason: Mapped[str] = mapped_column(String(255), default="")       # shown to the writer
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class LiveCheck(Base):
    """One look at the live post: what the platform showed, and how it compares."""

    __tablename__ = "live_checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    content_id: Mapped[int] = mapped_column(ForeignKey("contents.id"), index=True)
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    outcome: Mapped[str] = mapped_column(String(16))        # match | changed | removed | unverifiable
    detail: Mapped[str] = mapped_column(String(500), default="")
    live_text: Mapped[str] = mapped_column(Text, default="")  # what the platform showed (posts) or "" (pages)
    live_hash: Mapped[str] = mapped_column(String(64), default="")
    findings: Mapped[dict | None] = mapped_column(JSON, nullable=True)  # compliance scan of the live text
    checked_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # None = scheduler


class ChannelConnection(Base):
    """The account the console posts through, e.g. the LinkedIn member who clicked Connect.

    One row per provider: reconnecting replaces it. Tokens are encrypted with
    services/secrets_box.py before they are stored, and are never sent to the browser.
    """

    __tablename__ = "channel_connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(32), unique=True, index=True)   # "linkedin"
    account_urn: Mapped[str] = mapped_column(String(100))     # urn:li:person:abc123
    account_name: Mapped[str] = mapped_column(String(200))    # shown as "Connected as ..."
    access_token_enc: Mapped[str] = mapped_column(Text)
    refresh_token_enc: Mapped[str] = mapped_column(Text, default="")
    scopes: Mapped[str] = mapped_column(String(500), default="")  # space separated, as granted
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    connected_by_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)

    connected_by: Mapped[User] = relationship()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes.split()


class AuditLog(Base):
    """Append only. Nothing in the code updates or deletes these rows."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    actor_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)  # None = system
    action: Mapped[str] = mapped_column(String(64), index=True)     # "content.approved"
    entity: Mapped[str] = mapped_column(String(32))                 # "content"
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    before: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    after: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)

    actor: Mapped[User | None] = relationship()
