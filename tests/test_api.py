"""The HTTP layer: auth, role checks, and error shapes the frontend relies on."""

import pytest
from fastapi.testclient import TestClient

from app.main import app

from .conftest import GOOD_BODY, PASSWORD

client = TestClient(app)   # no `with`: the scheduler and seeding in lifespan stay off


def login(email):
    r = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['token']}"}


@pytest.fixture
def as_(team):
    return {key: login(f"{key}@test.example") for key in team}


def create(headers, body=GOOD_BODY):
    r = client.post("/api/content", headers=headers,
                    json={"channel": "linkedin", "type": "post", "title": "Plan early", "body": body})
    assert r.status_code == 201, r.text
    return r.json()["content"]["id"]


def test_needs_a_token():
    assert client.get("/api/content").status_code == 401


def test_wrong_password_gives_one_generic_message(team):
    r = client.post("/api/auth/login", json={"email": "creator@test.example", "password": "nope"})
    assert r.status_code == 401 and r.json()["detail"] == "Email or password is wrong."


def test_public_registration_creates_creator_and_signs_in():
    r = client.post("/api/auth/register", json={
        "name": "New Creator",
        "email": "new-creator@test.example",
        "password": "long-secure-password",
    })
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "creator"
    headers = {"Authorization": "Bearer " + r.json()["token"]}
    assert client.get("/api/auth/me", headers=headers).status_code == 200


def test_public_registration_rejects_duplicate_email(team):
    r = client.post("/api/auth/register", json={
        "name": "Another Creator",
        "email": "creator@test.example",
        "password": PASSWORD,
    })
    assert r.status_code == 409


def test_blocked_submit_returns_the_report(as_):
    cid = create(as_["creator"], body="Risk-free returns.")
    r = client.post(f"/api/content/{cid}/submit", headers=as_["creator"])
    assert r.status_code == 422
    assert r.json()["report"]["blocking"] >= 2   # the phrase and the missing disclaimer


def test_happy_path_over_http(as_):
    cid = create(as_["creator"])
    assert client.post(f"/api/content/{cid}/submit", headers=as_["creator"]).status_code == 200
    assert client.post(f"/api/content/{cid}/approve", headers=as_["creator"], json={}).status_code == 403
    r = client.post(f"/api/content/{cid}/approve", headers=as_["compliance"], json={"comment": "Fine."})
    assert r.json()["content"]["approved_by"]["name"] == "Compliance"

    r = client.post(f"/api/content/{cid}/publish-now", headers=as_["publisher"])
    job = r.json()["jobs"][0]
    assert job["status"] == "awaiting_manual"
    r = client.post(f"/api/jobs/{job['id']}/confirm", headers=as_["publisher"], json={"url": "https://x.example"})
    assert r.json()["content"]["status"] == "published"


def test_scan_endpoint(as_):
    r = client.post("/api/compliance/scan", headers=as_["creator"],
                    json={"title": "t", "body": "Double your money", "channel": "whatsapp", "type": "status"})
    assert r.status_code == 200 and not r.json()["passed"]


def test_ai_draft_falls_back_to_template_when_ai_is_off(as_):
    r = client.post("/api/ai/draft", headers=as_["creator"],
                    json={"brief": "Invite people to our retirement webinar", "channel": "linkedin", "type": "post"})
    data = r.json()
    assert r.status_code == 200 and data["by_ai"] is False
    assert "Investments are subject to market risks" in data["body"]


def test_only_compliance_or_admin_edit_rules(as_):
    rule = {"kind": "prohibited", "text": "sure shot", "severity": "block"}
    assert client.post("/api/compliance/rules", headers=as_["creator"], json=rule).status_code == 403
    assert client.post("/api/compliance/rules", headers=as_["compliance"], json=rule).status_code == 201


def test_audit_log_is_for_auditors(as_):
    create(as_["creator"])
    assert client.get("/api/audit", headers=as_["creator"]).status_code == 403
    rows = client.get("/api/audit", headers=as_["compliance"]).json()
    assert any(r["action"] == "content.created" for r in rows)
    csv = client.get("/api/audit/export.csv", headers=as_["admin"])
    assert csv.status_code == 200 and csv.text.startswith("id,time_utc")


def test_dashboard_queue_never_shows_your_own_work_for_review(as_, team):
    cid = create(as_["creator"])
    client.post(f"/api/content/{cid}/submit", headers=as_["creator"])
    queue = client.get("/api/dashboard", headers=as_["compliance"]).json()["queue"]
    assert [c["id"] for c in queue] == [cid]
