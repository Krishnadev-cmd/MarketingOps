"""Test setup: in-memory SQLite, no AI, no demo data.

Environment variables beat backend/.env, so these lines make the tests
independent of whatever is configured on your machine.
"""

import os

# Default: in-memory SQLite. To run the whole suite against a real (throwaway!)
# MySQL database: set TEST_DATABASE_URL=mysql+pymysql://.../marketing_ops_test
os.environ["DATABASE_URL"] = os.environ.get("TEST_DATABASE_URL", "sqlite://")
os.environ["APP_ENV"] = "development"
os.environ["LLM_ENABLED"] = "false"        # never call Groq from the tests
os.environ["SEED_DEMO_DATA"] = "false"
os.environ["LINKEDIN_CLIENT_ID"] = ""      # LinkedIn not configured unless a test sets it
os.environ["LINKEDIN_ORGANIZATION_URN"] = ""
os.environ["CMS_WEBHOOK_URL"] = ""
os.environ["JWT_SECRET"] = "test-secret-that-is-long-enough-for-hs256"

import pytest  # noqa: E402

from app import runtime  # noqa: E402
from app.database import Base, SessionLocal, create_tables, engine  # noqa: E402
from app.enums import Role  # noqa: E402
from app.models import User  # noqa: E402
from app.seed import MAIN_DISCLAIMER, seed  # noqa: E402
from app import security  # noqa: E402
from app.security import hash_password  # noqa: E402

security.ROUNDS = 1_000   # fast hashing in tests; production keeps 310k

PASSWORD = "Test-pass-123"
GOOD_BODY = f"Plan early and review your plan every year.\n\n{MAIN_DISCLAIMER}"


@pytest.fixture(autouse=True)
def fresh_db():
    """Every test starts with empty tables plus the starter compliance rules."""
    Base.metadata.drop_all(engine)
    create_tables()
    with SessionLocal() as db:
        seed(db, runtime.settings)
    yield


@pytest.fixture
def db():
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def team(db):
    """One user per role, plus a second creator and a second compliance officer."""
    people = {
        "creator": Role.CREATOR, "creator2": Role.CREATOR, "reviewer": Role.REVIEWER,
        "compliance": Role.COMPLIANCE, "compliance2": Role.COMPLIANCE,
        "publisher": Role.PUBLISHER, "admin": Role.ADMIN,
    }
    users = {}
    for key, role in people.items():
        users[key] = User(email=f"{key}@test.example", name=key.title(), role=role,
                          password_hash=hash_password(PASSWORD))
        db.add(users[key])
    db.commit()
    return users
