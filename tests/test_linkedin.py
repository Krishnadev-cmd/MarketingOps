"""LinkedIn: connecting an account, and posting through it. LinkedIn itself is faked."""

from datetime import timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import runtime
from app.database import utcnow
from app.enums import JobStatus, Status
from app.main import app
from app.models import AuditLog, ChannelConnection
from app.services import linkedin, workflow
from app.services.secrets_box import decrypt, encrypt

from .conftest import PASSWORD
from .test_workflow import approved

client = TestClient(app)
TOKEN = "AQX-real-looking-linkedin-token-123"
MEMBER = "urn:li:person:abc123"
COMPANY = "urn:li:organization:777"


def bearer(email):
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def fake_linkedin(monkeypatch):
    """LinkedIn is configured, and its HTTP calls are replaced by fakes that record what we sent."""
    monkeypatch.setattr(runtime.settings, "linkedin_client_id", "client-id-1")
    monkeypatch.setattr(runtime.settings, "linkedin_client_secret", "client-secret-1")
    monkeypatch.setattr(runtime.settings, "linkedin_organization_urn", "")
    sent = []
    monkeypatch.setattr(linkedin, "exchange_code", lambda s, code: {
        "access_token": TOKEN, "expires_in": 5184000, "scope": "openid,profile,w_member_social"})
    monkeypatch.setattr(linkedin, "fetch_member", lambda token: (MEMBER, "Sofia Lindqvist"))

    def create_post(token, version, author, text):
        sent.append({"token": token, "author": author, "text": text, "version": version})
        return "urn:li:share:9001"

    monkeypatch.setattr(linkedin, "create_post", create_post)
    return sent


def connect(db, team, scopes="openid profile w_member_social", expires_in_days=60):
    db.add(ChannelConnection(provider="linkedin", account_urn=MEMBER, account_name="Sofia Lindqvist",
                             access_token_enc=encrypt(TOKEN), refresh_token_enc="", scopes=scopes,
                             expires_at=utcnow() + timedelta(days=expires_in_days),
                             connected_by_id=team["admin"].id))
    db.commit()


# --- connecting ------------------------------------------------------------------
def test_admins_and_publishers_can_start_connecting(team, fake_linkedin):
    publisher = client.get("/api/channels/linkedin/connect", headers=bearer("publisher@test.example"))
    assert publisher.status_code == 200
    r = client.get("/api/channels/linkedin/connect", headers=bearer("admin@test.example"))
    q = parse_qs(urlparse(r.json()["url"]).query)
    assert q["client_id"] == ["client-id-1"]
    assert q["scope"] == ["openid profile w_member_social"]       # no company scope unless a Page is set


def test_company_scope_is_requested_only_when_a_page_is_set(team, fake_linkedin, monkeypatch):
    monkeypatch.setattr(runtime.settings, "linkedin_organization_urn", COMPANY)
    r = client.get("/api/channels/linkedin/connect", headers=bearer("admin@test.example"))
    assert "w_organization_social" in parse_qs(urlparse(r.json()["url"]).query)["scope"][0]


def test_connect_needs_app_credentials(team):
    r = client.get("/api/channels/linkedin/connect", headers=bearer("admin@test.example"))
    assert r.status_code == 409 and "LINKEDIN_CLIENT_ID" in r.json()["detail"]


def test_full_round_trip_stores_the_token_encrypted(db, team, fake_linkedin):
    url = client.get("/api/channels/linkedin/connect", headers=bearer("admin@test.example")).json()["url"]
    state = parse_qs(urlparse(url).query)["state"][0]

    r = client.get(f"/api/channels/linkedin/callback?code=one-time-code&state={state}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/team?linkedin=connected")

    conn = db.scalar(select(ChannelConnection))
    assert conn.account_name == "Sofia Lindqvist" and conn.account_urn == MEMBER
    assert TOKEN not in conn.access_token_enc and decrypt(conn.access_token_enc) == TOKEN
    audit = db.scalar(select(AuditLog).where(AuditLog.action == "channel.connected"))
    assert audit and TOKEN not in str(audit.after)


def test_a_forged_callback_is_refused(db, team, fake_linkedin):
    r = client.get("/api/channels/linkedin/callback?code=x&state=not-a-real-state", follow_redirects=False)
    assert "linkedin=error" in r.headers["location"]
    assert db.scalar(select(ChannelConnection)) is None


def test_cancelling_on_linkedin_comes_back_with_a_reason(team, fake_linkedin):
    r = client.get("/api/channels/linkedin/callback?error=user_cancelled_authorize"
                   "&error_description=The+user+cancelled", follow_redirects=False)
    assert "linkedin=error" in r.headers["location"] and "cancelled" in r.headers["location"]


def test_status_never_contains_the_token(db, team, fake_linkedin):
    connect(db, team)
    body = client.get("/api/channels", headers=bearer("creator@test.example")).text
    assert TOKEN not in body and "Sofia Lindqvist" in body


def test_disconnect(db, team, fake_linkedin):
    connect(db, team)
    assert client.delete("/api/channels/linkedin", headers=bearer("publisher@test.example")).status_code == 403
    r = client.delete("/api/channels/linkedin", headers=bearer("admin@test.example"))
    assert r.json()["linkedin"]["connected"] is False


# --- posting -----------------------------------------------------------------------
def test_publishing_posts_to_the_connected_profile(db, team, fake_linkedin):
    connect(db, team)
    c = approved(db, team)
    job = workflow.publish_now(db, team["publisher"], c, runtime.settings)
    assert job.status == JobStatus.SUCCEEDED and c.status == Status.PUBLISHED
    assert job.external_url == "https://www.linkedin.com/feed/update/urn:li:share:9001"
    assert fake_linkedin[0]["author"] == MEMBER and fake_linkedin[0]["token"] == TOKEN


def test_company_page_once_linkedin_grants_it(db, team, fake_linkedin, monkeypatch):
    monkeypatch.setattr(runtime.settings, "linkedin_organization_urn", COMPANY)
    connect(db, team, scopes="openid profile w_member_social w_organization_social")
    workflow.publish_now(db, team["publisher"], approved(db, team), runtime.settings)
    assert fake_linkedin[0]["author"] == COMPANY


def test_page_set_but_not_granted_posts_to_the_profile_and_says_why(db, team, fake_linkedin, monkeypatch):
    monkeypatch.setattr(runtime.settings, "linkedin_organization_urn", COMPANY)
    connect(db, team)   # connected before approval: no company scope
    workflow.publish_now(db, team["publisher"], approved(db, team), runtime.settings)
    assert fake_linkedin[0]["author"] == MEMBER
    note = client.get("/api/channels", headers=bearer("admin@test.example")).json()["linkedin"]["note"]
    assert "not granted yet" in note


def test_no_connection_falls_back_to_manual(db, team, fake_linkedin):
    job = workflow.publish_now(db, team["publisher"], approved(db, team), runtime.settings)
    assert job.status == JobStatus.AWAITING_MANUAL and "not connected" in job.result
    assert fake_linkedin == []


def test_expired_connection_falls_back_to_manual(db, team, fake_linkedin):
    connect(db, team, expires_in_days=-1)
    job = workflow.publish_now(db, team["publisher"], approved(db, team), runtime.settings)
    assert job.status == JobStatus.AWAITING_MANUAL and "expired" in job.result


def test_linkedin_refusal_fails_the_job_with_a_readable_reason(db, team, fake_linkedin, monkeypatch):
    connect(db, team)

    def refuse(*args):
        raise linkedin.LinkedInError("LinkedIn says the connection is no longer valid (expired or revoked).")

    monkeypatch.setattr(linkedin, "create_post", refuse)
    c = approved(db, team)
    job = workflow.publish_now(db, team["publisher"], c, runtime.settings)
    assert job.status == JobStatus.FAILED and "no longer valid" in job.result
    assert c.status == Status.SCHEDULED          # nothing pretends it went out


# --- text format -----------------------------------------------------------------------
def test_reserved_characters_are_escaped_but_hashtags_survive():
    escaped = linkedin.escape_commentary("Retire (well) [2027] @team #Retirement")
    assert escaped == "Retire \\(well\\) \\[2027\\] \\@team #Retirement"
