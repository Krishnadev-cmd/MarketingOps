"""Live post watch: published posts are re-read and compared with what was approved.

LinkedIn and websites are faked; the safety checks on fetching pages run for real
(they refuse before any network call).
"""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app import runtime
from app.database import utcnow
from app.main import app
from app.models import AuditLog, LiveCheck
from app.seed import MAIN_DISCLAIMER
from app.services import linkedin, live_watch, workflow

from .conftest import PASSWORD
from .test_linkedin import connect
from .test_workflow import approved

client = TestClient(app)
HERO_TITLE = "Plan the retirement you actually want"
HERO_BODY = f"Talk to a certified adviser about income and healthcare.\n\n{MAIN_DISCLAIMER}"
PAGE_URL = "https://www.meridian.example/retire"


def bearer(email):
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {r.json()['token']}"}


def alerts(db, content_id, action):
    return db.scalar(select(func.count(AuditLog.id)).where(AuditLog.entity_id == content_id,
                                                           AuditLog.action == action))


@pytest.fixture
def website_post(db, team):
    """A website hero, approved and published by hand with its page link."""
    c = workflow.create(db, team["creator"], channel="website", type="hero", title=HERO_TITLE, body=HERO_BODY)
    workflow.submit(db, team["creator"], c)
    workflow.approve(db, team["compliance"], c)
    job = workflow.publish_now(db, team["publisher"], c, runtime.settings)
    workflow.confirm_manual(db, team["publisher"], job, PAGE_URL)
    db.commit()
    return c


@pytest.fixture
def page(monkeypatch):
    """What the 'website' currently serves. Tests change it to simulate edits."""
    served = {"status": 200, "text": f"Home  Products  {HERO_TITLE}  {HERO_BODY}  Contact us"}
    monkeypatch.setattr(live_watch, "fetch_page_text", lambda url, s: (served["status"], served["text"]))
    return served


@pytest.fixture
def linkedin_post(db, team, monkeypatch):
    """A LinkedIn post published through the API, plus a fake LinkedIn to read it back."""
    monkeypatch.setattr(runtime.settings, "linkedin_client_id", "id")
    monkeypatch.setattr(runtime.settings, "linkedin_client_secret", "secret")
    monkeypatch.setattr(linkedin, "create_post", lambda *a: "urn:li:share:555")
    connect(db, team)
    c = approved(db, team)
    workflow.publish_now(db, team["publisher"], c, runtime.settings)
    db.commit()
    live = {"text": c.body}

    def read_post(token, version, urn):
        assert urn == "urn:li:share:555"
        if isinstance(live["text"], Exception):
            raise live["text"]
        return live["text"]

    monkeypatch.setattr(linkedin, "read_post", read_post)
    return c, live


# --- website ---------------------------------------------------------------------
def test_page_still_showing_the_approved_text_matches(db, website_post, page):
    check = live_watch.check(db, website_post, runtime.settings)
    assert check.outcome == "match" and website_post.live_status == "match"


def test_edited_page_raises_one_alert_not_one_per_check(db, website_post, page):
    page["text"] = f"Home  {HERO_TITLE}  Talk to us today about guaranteed income.  Contact us"
    live_watch.check(db, website_post, runtime.settings)
    live_watch.check(db, website_post, runtime.settings)
    db.commit()
    assert website_post.live_status == "changed"
    assert alerts(db, website_post.id, "live.change_detected") == 1


def test_deleted_page_is_reported_removed(db, website_post, page):
    page["status"] = 404
    live_watch.check(db, website_post, runtime.settings)
    db.commit()
    assert website_post.live_status == "removed" and alerts(db, website_post.id, "live.removed") == 1


def test_fixing_the_page_closes_the_alert(db, website_post, page):
    original = page["text"]
    page["text"] = "Home  Contact us"
    live_watch.check(db, website_post, runtime.settings)
    page["text"] = original
    live_watch.check(db, website_post, runtime.settings)
    db.commit()
    assert website_post.live_status == "match" and alerts(db, website_post.id, "live.restored") == 1


# --- fetching safely ----------------------------------------------------------------
def test_pages_are_not_fetched_until_hosts_are_allowed(monkeypatch):
    monkeypatch.setattr(runtime.settings, "watch_allowed_hosts", [])
    with pytest.raises(live_watch.Unverifiable, match="WATCH_ALLOWED_HOSTS"):
        live_watch.fetch_page_text(PAGE_URL, runtime.settings)


def test_other_hosts_and_internal_addresses_are_refused(monkeypatch):
    monkeypatch.setattr(runtime.settings, "watch_allowed_hosts", ["meridian.example"])
    for url in ["http://169.254.169.254/latest/meta-data", "http://localhost:8787/api/health",
                "https://meridian.example.evil.test/x", "file:///etc/passwd"]:
        with pytest.raises(live_watch.Unverifiable):
            live_watch.fetch_page_text(url, runtime.settings)


def test_allowed_host_includes_subdomains_only():
    assert live_watch.host_allowed("www.meridian.example", ["meridian.example"])
    assert not live_watch.host_allowed("evilmeridian.example", ["meridian.example"])


def test_only_visible_text_is_compared():
    html = "<html><head><title>x</title><script>guaranteed()</script></head><body><h1>Hello</h1></body></html>"
    assert live_watch.visible_text(html).strip() == "Hello"


# --- LinkedIn ---------------------------------------------------------------------------
def test_unchanged_linkedin_post_matches(db, linkedin_post):
    c, _ = linkedin_post
    assert live_watch.check(db, c, runtime.settings).outcome == "match"


def test_edited_linkedin_post_is_scanned_for_compliance(db, linkedin_post):
    c, live = linkedin_post
    live["text"] = "Now with guaranteed returns! " + c.body
    check = live_watch.check(db, c, runtime.settings)
    assert check.outcome == "changed" and check.findings["blocking"] >= 1
    assert "blocking compliance issue" in check.detail and "guaranteed" in check.live_text


def test_no_read_permission_is_reported_honestly(db, linkedin_post):
    c, live = linkedin_post
    live["text"] = linkedin.LinkedInError("LinkedIn doesn't let this connection read posts back.")
    check = live_watch.check(db, c, runtime.settings)
    assert check.outcome == "unverifiable" and c.live_status == "unverifiable"


def test_cant_look_never_hides_an_open_alert(db, linkedin_post):
    c, live = linkedin_post
    live["text"] = "Edited on LinkedIn."
    live_watch.check(db, c, runtime.settings)
    live["text"] = linkedin.LinkedInError("connection expired")
    live_watch.check(db, c, runtime.settings)
    assert c.live_status == "changed"


# --- acknowledging -----------------------------------------------------------------------
def test_acknowledged_change_stays_quiet_until_it_changes_again(db, team, linkedin_post):
    c, live = linkedin_post
    live["text"] = "A small typo fix on LinkedIn. " + c.body
    live_watch.check(db, c, runtime.settings)
    live_watch.acknowledge(db, c, team["compliance"], "Typo fix only, wording unchanged.")
    live_watch.check(db, c, runtime.settings)
    db.commit()
    assert c.live_status == "acknowledged" and alerts(db, c.id, "live.change_detected") == 1

    live["text"] = "A different edit entirely."
    live_watch.check(db, c, runtime.settings)
    db.commit()
    assert c.live_status == "changed" and alerts(db, c.id, "live.change_detected") == 2


def test_only_compliance_officers_acknowledge_and_must_give_a_reason(db, team, linkedin_post):
    c, live = linkedin_post
    live["text"] = "Edited."
    live_watch.check(db, c, runtime.settings)
    db.commit()
    url = f"/api/content/{c.id}/live/acknowledge"
    assert client.post(url, headers=bearer("publisher@test.example"), json={"comment": "fine"}).status_code == 403
    assert client.post(url, headers=bearer("compliance@test.example"), json={"comment": ""}).status_code == 422
    r = client.post(url, headers=bearer("compliance@test.example"), json={"comment": "Reviewed, acceptable."})
    assert r.json()["content"]["live_status"] == "acknowledged"


# --- scheduling and API ---------------------------------------------------------------------
def test_whatsapp_status_is_not_watchable_and_then_skipped(db, team):
    c = workflow.create(db, team["creator"], channel="whatsapp", type="status", title="S",
                        body=f"Hello. {MAIN_DISCLAIMER}")
    workflow.submit(db, team["creator"], c)
    workflow.approve(db, team["compliance"], c)
    job = workflow.publish_now(db, team["publisher"], c, runtime.settings)
    workflow.confirm_manual(db, team["publisher"], job, "")
    db.commit()
    assert live_watch.run_due_checks(db, runtime.settings) == 1 and c.live_status == "not_watchable"
    db.commit()
    assert live_watch.run_due_checks(db, runtime.settings) == 0


def test_scheduler_respects_interval_and_watch_window(db, website_post, page):
    assert live_watch.run_due_checks(db, runtime.settings) == 1      # never checked: due
    db.commit()
    assert live_watch.run_due_checks(db, runtime.settings) == 0      # checked just now: not due
    website_post.live_checked_at = utcnow() - timedelta(hours=runtime.settings.watch_interval_hours + 1)
    website_post.published_at = utcnow() - timedelta(days=runtime.settings.watch_days + 1)
    db.commit()
    assert live_watch.run_due_checks(db, runtime.settings) == 0      # outside the watch window


def test_check_now_permissions_and_dashboard_alert(db, team, website_post, page):
    url = f"/api/content/{website_post.id}/live/check"
    assert client.post(url, headers=bearer("creator@test.example")).status_code == 403
    page["text"] = "Something else entirely"
    r = client.post(url, headers=bearer("publisher@test.example"))
    assert r.json()["content"]["live_status"] == "changed"
    assert r.json()["live_checks"][0]["outcome"] == "changed"
    dash = client.get("/api/dashboard", headers=bearer("compliance@test.example")).json()
    assert [a["id"] for a in dash["live_alerts"]] == [website_post.id]


def test_unpublished_content_cannot_be_checked(db, team):
    c = approved(db, team)
    r = client.post(f"/api/content/{c.id}/live/check", headers=bearer("publisher@test.example"))
    assert r.status_code == 409
