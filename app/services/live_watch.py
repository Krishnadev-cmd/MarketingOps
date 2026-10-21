"""Live post watch: is what's on the platform still what Compliance approved?

After something is published, the scheduler looks at it again every few hours
(WATCH_INTERVAL_HOURS) for a while (WATCH_DAYS). Each look is a LiveCheck row:

    match         the live text is the approved text
    changed       someone edited it on the platform (or the page no longer shows it)
    removed       the post or page is gone
    unverifiable  we could not look (no permission, host not allowed, network...)

What can be checked:
    LinkedIn  read back through the API with the stored connection. Needs read
              permission, which LinkedIn grants with company-page approval.
    Website   the public page is fetched and must still contain the approved text.
              Only hosts listed in WATCH_ALLOWED_HOSTS are ever fetched.
    WhatsApp  Status has no link and disappears after 24 hours: not watchable.

An alert (audit row + Overview card) fires once per change, not on every check.
A Compliance Officer can acknowledge a change; it stays quiet until the live
text changes again.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..config import Settings
from ..database import utcnow
from ..enums import Status
from ..models import ChannelConnection, Content, LiveCheck, User
from . import compliance, linkedin
from .audit import record
from .secrets_box import CannotDecrypt, decrypt

log = logging.getLogger(__name__)

ALERTING = ("changed", "removed")
MAX_PAGE_BYTES = 2_000_000


class Unverifiable(Exception):
    """We could not look. The message says why, in plain words."""


# ---------------------------------------------------------------------------
# The entry points
# ---------------------------------------------------------------------------
def check(db: Session, content: Content, settings: Settings, actor: User | None = None) -> LiveCheck:
    """Look at one published item now, record what we saw, and raise an alert if needed."""
    outcome, detail, live_text, findings = _look(db, content, settings)
    live_hash = _fingerprint(content, outcome, live_text)
    row = LiveCheck(content_id=content.id, checked_at=utcnow(), outcome=outcome, detail=detail[:500],
                    live_text=live_text, live_hash=live_hash, findings=findings,
                    checked_by_id=actor.id if actor else None)
    db.add(row)
    db.flush()   # give the row its id now, so "the previous check" below means the one before it
    _apply(db, content, row, actor)
    return row


def run_due_checks(db: Session, settings: Settings, limit: int = 20) -> int:
    """The scheduler's share of the work: check whatever is due, oldest first."""
    now = utcnow()
    due = db.scalars(
        select(Content)
        .where(Content.status == Status.PUBLISHED,
               Content.published_at >= now - timedelta(days=settings.watch_days),
               or_(Content.live_status.is_(None), Content.live_status != "not_watchable"),
               or_(Content.live_checked_at.is_(None),
                   Content.live_checked_at <= now - timedelta(hours=settings.watch_interval_hours)))
        .order_by(Content.live_checked_at.is_(None).desc(), Content.live_checked_at)
        .limit(limit)
    ).all()
    for content in due:
        try:
            check(db, content, settings)
        except Exception:   # one bad item must never stop the others
            log.exception("Live check failed for content %s", content.id)
    return len(due)


def acknowledge(db: Session, content: Content, officer: User, comment: str) -> None:
    """A Compliance Officer reviewed the live change and accepts it (with a reason, for the record)."""
    db.flush()
    last = db.scalar(select(LiveCheck).where(LiveCheck.content_id == content.id).order_by(LiveCheck.id.desc()))
    before = {"live_status": content.live_status}
    content.live_acked_hash = last.live_hash if last else ""
    content.live_status = "acknowledged"
    record(db, officer, "live.acknowledged", "content", content.id, before=before,
           after={"live_status": "acknowledged"}, reason=comment)


# ---------------------------------------------------------------------------
# Looking
# ---------------------------------------------------------------------------
def _look(db: Session, content: Content, s: Settings) -> tuple[str, str, str, dict | None]:
    """(outcome, detail, live_text, compliance findings on the live text)"""
    try:
        if content.channel == "linkedin":
            return _look_linkedin(db, content, s)
        if content.channel == "website":
            return _look_website(content, s)
        return "not_watchable", "WhatsApp Status has no link to check, and it disappears after 24 hours.", "", None
    except Unverifiable as exc:
        return "unverifiable", str(exc), "", None


def _look_linkedin(db: Session, content: Content, s: Settings) -> tuple[str, str, str, dict | None]:
    urn = linkedin.urn_from_url(content.published_url)
    if not urn:
        raise Unverifiable("The saved link doesn't identify a LinkedIn post (posted by hand without a post link?).")
    conn = db.scalar(select(ChannelConnection).where(ChannelConnection.provider == "linkedin"))
    if conn is None or conn.expires_at <= utcnow():
        raise Unverifiable("LinkedIn is not connected (or the connection expired), so the post can't be read.")
    try:
        token = decrypt(conn.access_token_enc)
        live = linkedin.read_post(token, s.linkedin_api_version, urn)
    except CannotDecrypt as exc:
        raise Unverifiable(str(exc)) from exc
    except linkedin.LinkedInError as exc:
        raise Unverifiable(str(exc)) from exc
    except httpx.HTTPError as exc:
        raise Unverifiable(f"Could not reach LinkedIn ({exc.__class__.__name__}).") from exc

    if live is None:
        return "removed", "The post no longer exists on LinkedIn.", "", None
    if compliance.words_of(live) == compliance.words_of(content.body):
        return "match", "The live post matches the approved text.", live, None
    report = compliance.scan("", live, content.channel, content.type, _rules(db))
    detail = "The live post differs from the approved text."
    if not report["passed"]:
        detail += f" It now has {report['blocking']} blocking compliance issue(s)."
    return "changed", detail, live, report


def _look_website(content: Content, s: Settings) -> tuple[str, str, str, dict | None]:
    if not content.published_url:
        raise Unverifiable("No page link was saved when this was published.")
    status, page_text = fetch_page_text(content.published_url, s)
    if status in (404, 410):
        return "removed", f"The page returns {status} (not found).", "", None
    if status >= 400:
        raise Unverifiable(f"The page answered with HTTP {status}.")
    page_words = compliance.words_of(page_text)
    missing = [part for part, text in (("headline", content.title), ("text", content.body))
               if text.strip() and compliance.words_of(text) not in page_words]
    if not missing:
        return "match", "The page shows the approved headline and text.", "", None
    return "changed", f"The page no longer shows the approved {' and '.join(missing)}.", "", None


def _rules(db: Session):
    from .workflow import active_rules   # local import: workflow imports this module's neighbours
    return active_rules(db)


# ---------------------------------------------------------------------------
# Fetching web pages safely
# ---------------------------------------------------------------------------
def host_allowed(host: str | None, allowed: list[str]) -> bool:
    host = (host or "").lower().rstrip(".")
    return any(host == a.lower() or host.endswith("." + a.lower()) for a in allowed if a)


def fetch_page_text(url: str, s: Settings) -> tuple[int, str]:
    """Fetch a page and return (status, visible text).

    Only http(s), only hosts in WATCH_ALLOWED_HOSTS, redirects followed by hand
    (at most 3, each re-checked against the allow-list), at most 2 MB read.
    Without these limits, a "live link" typed into the console could make the
    server fetch internal addresses on your network.
    """
    if not s.watch_allowed_hosts:
        raise Unverifiable("Set WATCH_ALLOWED_HOSTS to your website's domain so the console may read its pages.")
    current = url
    with httpx.Client(follow_redirects=False, timeout=15,
                      headers={"User-Agent": "MarketingOpsConsole-LiveWatch/1.0"}) as client:
        for _hop in range(4):
            parsed = urlparse(current)
            if parsed.scheme not in ("http", "https"):
                raise Unverifiable("Only http and https links can be checked.")
            if not host_allowed(parsed.hostname, s.watch_allowed_hosts):
                raise Unverifiable(f"{parsed.hostname} is not in WATCH_ALLOWED_HOSTS, so it is not fetched.")
            try:
                response = client.get(current)
            except httpx.HTTPError as exc:
                raise Unverifiable(f"Could not reach the page ({exc.__class__.__name__}).") from exc
            if response.is_redirect and response.headers.get("location"):
                current = urljoin(current, response.headers["location"])
                continue
            body = response.content[:MAX_PAGE_BYTES].decode(response.encoding or "utf-8", errors="replace")
            return response.status_code, visible_text(body)
    raise Unverifiable("The page redirects too many times.")


class _TextOnly(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "head"}

    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipping += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skipping:
            self.skipping -= 1

    def handle_data(self, data):
        if not self.skipping:
            self.parts.append(data)


def visible_text(html: str) -> str:
    parser = _TextOnly()
    parser.feed(html)
    return " ".join(parser.parts)


# ---------------------------------------------------------------------------
# Recording and alerting
# ---------------------------------------------------------------------------
def _fingerprint(content: Content, outcome: str, live_text: str) -> str:
    """Identifies *this* live state, so an acknowledged change stays acknowledged until it changes again."""
    basis = compliance.words_of(live_text) if live_text else f"{outcome}:{content.id}"
    return hashlib.sha256(basis.encode()).hexdigest()


def _apply(db: Session, content: Content, row: LiveCheck, actor: User | None) -> None:
    previous = content.live_status
    previous_hash = db.scalar(select(LiveCheck.live_hash).where(LiveCheck.content_id == content.id,
                                                                LiveCheck.id < row.id)
                              .order_by(LiveCheck.id.desc())) if content.id else None
    content.live_checked_at = row.checked_at

    if row.outcome == "match":
        content.live_status = "match"
        content.live_acked_hash = ""
        if previous in (*ALERTING, "acknowledged"):
            record(db, actor, "live.restored", "content", content.id, before={"live_status": previous},
                   after={"live_status": "match"}, reason=row.detail)
        return

    if row.outcome in ALERTING:
        if row.outcome == "changed" and row.live_hash == content.live_acked_hash:
            content.live_status = "acknowledged"   # the same change a Compliance Officer already accepted
            return
        content.live_status = row.outcome
        if previous != row.outcome or previous_hash != row.live_hash:
            action = "live.removed" if row.outcome == "removed" else "live.change_detected"
            record(db, actor, action, "content", content.id, before={"live_status": previous},
                   after={"live_status": row.outcome, "live_text": row.live_text[:1000] or None},
                   reason=row.detail)
        return

    # unverifiable / not_watchable: never hide an open alert behind "couldn't look this time".
    if previous not in ALERTING:
        content.live_status = row.outcome
