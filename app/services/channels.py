"""Channel adapters: how an approved item actually reaches LinkedIn, WhatsApp or the website.

Each adapter returns a PublishResult. `mode="api"` means we posted it ourselves;
`mode="manual"` means a person must post it and then confirm in the console.

The reality we design for (see README):
  - LinkedIn: an admin connects an account with "Connect LinkedIn". Posts go to that
    person's profile, or to the company Page once LinkedIn approves company posting.
    No connection, or an expired one, falls back to "copy and post by hand".
  - The WhatsApp Cloud API cannot post to Status at all, so Status is always manual.
    Message templates need Meta's approval before the API will send them.
  - The website goes through your CMS's API (CMS_WEBHOOK_URL), never the live HTML.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import utcnow
from ..models import ChannelConnection, Content
from . import linkedin
from .secrets_box import CannotDecrypt, decrypt

log = logging.getLogger(__name__)


@dataclass
class PublishResult:
    mode: str              # "api" or "manual"
    ok: bool               # for "manual", True just means "instructions ready"
    message: str
    url: str = ""


class ChannelError(Exception):
    """The platform refused the post. The message is safe to show to the publisher."""


def publish(content: Content, settings: Settings, db: Session) -> PublishResult:
    if content.channel == "linkedin":
        return _linkedin(content, settings, db)
    if content.channel == "whatsapp":
        return _whatsapp(content)
    if content.channel == "website":
        return _website(content, settings)
    raise ChannelError(f"Unknown channel {content.channel!r}")


# ---------------------------------------------------------------------------
# Status for the Team page and /api/health
# ---------------------------------------------------------------------------
def _linkedin_connection(db: Session) -> ChannelConnection | None:
    return db.scalar(select(ChannelConnection).where(ChannelConnection.provider == "linkedin"))


def posts_as_company(conn: ChannelConnection, s: Settings) -> bool:
    return bool(s.linkedin_organization_urn) and conn.has_scope(linkedin.ORG_SCOPE)


def describe(conn: ChannelConnection) -> dict:
    """What the audit log records about a connection. Never the token."""
    return {"account": conn.account_name, "account_urn": conn.account_urn, "scopes": conn.scopes,
            "expires_at": conn.expires_at.isoformat() if conn.expires_at else None}


def connection_status(settings: Settings, db: Session | None) -> dict[str, dict]:
    """Which channels can publish on their own, and why not when they can't."""
    return {
        "linkedin": _linkedin_status(settings, db),
        "whatsapp": {
            "mode": "manual",
            "note": "Cloud API cannot post to Status. Download and post from the business phone.",
        },
        "website": {
            "mode": "api" if settings.cms_webhook_url else "manual",
            "note": "Publishes through your CMS API. Set CMS_WEBHOOK_URL to connect.",
        },
    }


def _linkedin_status(s: Settings, db: Session | None) -> dict:
    base = {"configured": bool(s.linkedin_client_id and s.linkedin_client_secret), "connected": False,
            "account_name": None, "expires_at": None, "days_left": None, "posting_as": None,
            "company_page_set": bool(s.linkedin_organization_urn)}
    if not base["configured"]:
        return {**base, "mode": "manual",
                "note": "Add your LinkedIn app's Client ID and Secret to backend/.env, then connect here."}
    conn = _linkedin_connection(db) if db is not None else None
    if conn is None:
        return {**base, "mode": "manual", "note": "Not connected. Posts are copied and posted by hand until an admin connects."}

    company = posts_as_company(conn, s)
    seconds_left = (conn.expires_at - utcnow()).total_seconds()
    info = {**base, "connected": True, "account_name": conn.account_name, "expires_at": conn.expires_at.isoformat(),
            "days_left": max(0, math.ceil(seconds_left / 86400)), "posting_as": "company" if company else "profile"}
    if conn.expires_at <= utcnow():
        return {**info, "mode": "manual", "note": "The connection expired. Reconnect to post automatically again."}
    where = "the company Page" if company else f"{conn.account_name}'s profile"
    note = f"Posts go to {where} automatically."
    if s.linkedin_organization_urn and not company:
        note += " Company posting is set up but not granted yet: LinkedIn must approve your app, then reconnect."
    return {**info, "mode": "api", "note": note}


# ---------------------------------------------------------------------------
# LinkedIn
# ---------------------------------------------------------------------------
def _linkedin(content: Content, s: Settings, db: Session) -> PublishResult:
    def by_hand(why: str) -> PublishResult:
        return PublishResult(mode="manual", ok=True,
                             message=f"{why} Copy the text, post it on LinkedIn, then paste the post link here.")

    conn = _linkedin_connection(db)
    if conn is None:
        return by_hand("LinkedIn is not connected.")
    if conn.expires_at <= utcnow():
        return by_hand(f"The LinkedIn connection expired on {conn.expires_at:%d %b %Y}; an admin needs to reconnect.")
    try:
        token = decrypt(conn.access_token_enc)
    except CannotDecrypt:
        return by_hand("The saved LinkedIn connection can't be read (the encryption key changed); reconnect it.")

    company = posts_as_company(conn, s)
    author = s.linkedin_organization_urn if company else conn.account_urn
    try:
        urn = linkedin.create_post(token, s.linkedin_api_version, author, content.body)
    except linkedin.LinkedInError as exc:
        raise ChannelError(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise ChannelError(f"Could not reach LinkedIn ({exc.__class__.__name__}). Try again shortly.") from exc

    where = "the company Page" if company else f"{conn.account_name}'s profile"
    note = " The image was not attached: image upload is not built yet." if content.media_url else ""
    return PublishResult(mode="api", ok=True, message=f"Posted to LinkedIn on {where}.{note}", url=linkedin.post_url(urn))


# ---------------------------------------------------------------------------
# WhatsApp and website
# ---------------------------------------------------------------------------
def _whatsapp(content: Content) -> PublishResult:
    if content.type == "template":
        return PublishResult(
            mode="manual", ok=True,
            message="Submit this template in WhatsApp Manager for Meta approval. "
                    "Once approved, it can be sent through the Cloud API.",
        )
    return PublishResult(
        mode="manual", ok=True,
        message="WhatsApp cannot post to Status through the API. Download the media and caption, "
                "post it from the business phone, then confirm here.",
    )


def _website(content: Content, s: Settings) -> PublishResult:
    if not s.cms_webhook_url:
        return PublishResult(
            mode="manual", ok=True,
            message="No CMS is connected. Paste the approved text into the matching CMS component, "
                    "then paste the page link here.",
        )
    # Only approved components with plain fields go to the CMS: no raw HTML, no layout.
    response = httpx.post(
        s.cms_webhook_url,
        json={"component": content.type, "title": content.title, "body": content.body,
              "media_url": content.media_url, "content_id": content.id, "version": content.version},
        timeout=20,
    )
    if response.status_code >= 300:
        raise ChannelError(f"CMS returned {response.status_code}: {response.text[:300]}")
    url = ""
    try:
        url = response.json().get("url", "")
    except ValueError:
        pass
    return PublishResult(mode="api", ok=True, message="Published through the CMS.", url=url)
