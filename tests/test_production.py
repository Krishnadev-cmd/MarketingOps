"""The production blockers stay fixed: double publishing, brute force, sessions,
user management, health, and unsafe production settings."""

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import runtime
from app.config import Settings
from app.database import utcnow
from app.enums import JobStatus
from app.main import app
from app.services import workflow

from .conftest import GOOD_BODY, PASSWORD
from .test_workflow import approved

client = TestClient(app)


def login(email, password=PASSWORD):
    return client.post("/api/auth/login", json={"email": email, "password": password})


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# --- publishing exactly once ----------------------------------------------------
def test_a_job_is_published_at_most_once(db, team):
    """Scheduler and "Publish now" racing for the same job: only one wins the claim."""
    c = approved(db, team)
    job = workflow.schedule(db, team["publisher"], c, utcnow())
    assert workflow.claim(db, job) is True
    assert workflow.claim(db, job) is False          # the second claimant walks away
    assert job.status == JobStatus.PUBLISHING


def test_run_job_skips_a_job_someone_else_claimed(db, team):
    c = approved(db, team)
    job = workflow.schedule(db, team["publisher"], c, utcnow())
    workflow.claim(db, job)                           # another process got it first
    workflow.run_job(db, job, runtime.settings)
    assert job.status == JobStatus.PUBLISHING          # untouched by us


# --- login protection -----------------------------------------------------------
def test_five_wrong_passwords_lock_the_account(team):
    for _ in range(5):
        assert login("creator@test.example", "wrong-password-123").status_code == 401
    locked = login("creator@test.example")             # even the right password now
    assert locked.status_code == 429 and "Try again" in locked.json()["detail"]


def test_a_successful_login_resets_the_counter(team):
    for _ in range(4):
        login("creator@test.example", "wrong-password-123")
    assert login("creator@test.example").status_code == 200
    for _ in range(4):
        login("creator@test.example", "wrong-password-123")
    assert login("creator@test.example").status_code == 200   # never reached 5 in a row


def test_admin_can_unlock(team):
    for _ in range(5):
        login("creator@test.example", "wrong-password-123")
    admin = bearer(login("admin@test.example").json()["token"])
    r = client.patch(f"/api/users/{team['creator'].id}", headers=admin, json={"unlock": True})
    assert r.json()["locked"] is False
    assert login("creator@test.example").status_code == 200


# --- passwords and sessions -------------------------------------------------------
def test_changing_password_ends_old_sessions(team):
    old = login("creator@test.example").json()["token"]
    new_pw = "a-much-longer-passphrase-42"
    r = client.post("/api/auth/password", headers=bearer(old),
                    json={"current_password": PASSWORD, "new_password": new_pw})
    assert r.status_code == 200
    assert client.get("/api/auth/me", headers=bearer(old)).status_code == 401
    assert client.get("/api/auth/me", headers=bearer(r.json()["token"])).status_code == 200
    assert login("creator@test.example", new_pw).status_code == 200


def test_weak_new_password_is_refused(team):
    token = login("creator@test.example").json()["token"]
    r = client.post("/api/auth/password", headers=bearer(token),
                    json={"current_password": PASSWORD, "new_password": "short"})
    assert r.status_code == 422


# --- user management ------------------------------------------------------------------
def test_only_admins_create_users_and_passwords_never_reach_the_audit_log(team):
    body = {"email": "Nadia.Farouk@test.example", "name": "Nadia Farouk", "role": "creator",
            "password": "temporary-pass-2026"}
    creator = bearer(login("creator@test.example").json()["token"])
    assert client.post("/api/users", headers=creator, json=body).status_code == 403

    admin = bearer(login("admin@test.example").json()["token"])
    r = client.post("/api/users", headers=admin, json=body)
    assert r.status_code == 201 and r.json()["email"] == "nadia.farouk@test.example"
    assert client.post("/api/users", headers=admin, json=body).status_code == 409   # duplicate
    assert login("nadia.farouk@test.example", "temporary-pass-2026").status_code == 200

    audit = client.get("/api/audit?action=user", headers=admin).text
    assert "temporary-pass-2026" not in audit


# --- health and settings ----------------------------------------------------------------
def test_health_reports_the_database():
    r = client.get("/api/health")
    assert r.status_code == 200 and r.json()["database"] == "up"


def test_api_responses_carry_security_headers():
    r = client.get("/api/health")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize("override, problem", [
    ({"jwt_secret": ""}, "JWT_SECRET"),
    ({"seed_demo_data": True}, "SEED_DEMO_DATA"),
    ({"database_url": "sqlite:///x.db"}, "MySQL"),
    ({"cors_origins": ["http://localhost:3000"]}, "CORS_ORIGINS"),
    ({"token_encryption_key": ""}, "TOKEN_ENCRYPTION_KEY"),
])
def test_production_refuses_unsafe_settings(override, problem):
    good = {"app_env": "production", "database_url": "mysql+pymysql://u:p@db/marketing_ops",
            "jwt_secret": "x" * 48, "seed_demo_data": False, "cors_origins": ["https://console.example.com"],
            "token_encryption_key": "k" * 44}
    Settings(_env_file=None, **good)   # the safe baseline is accepted
    with pytest.raises(ValidationError, match=problem):
        Settings(_env_file=None, **{**good, **override})


def test_database_url_is_required():
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        Settings(_env_file=None, database_url="")


def test_good_body_still_passes_checks():
    # Guard: the fixtures used across the suite stay compliant under the starter rules.
    assert "Investments are subject to market risks" in GOOD_BODY
