"""The migrations build exactly the schema the models describe, and undo cleanly.

If someone edits app/models.py and forgets a migration, `test_no_drift` fails
and names the difference. Run on a throwaway SQLite file, so it needs no server.
(The MySQL-only trigger migration is skipped on SQLite by design.)
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from app.config import BACKEND_DIR
from app.database import Base


@pytest.fixture
def migrated(tmp_path: Path, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'migrate.db').as_posix()}"
    # env.py reads the URL from the app settings; point the cached settings at the temp file.
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "database_url", url)
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    yield cfg, engine
    engine.dispose()


def test_upgrade_reaches_head(migrated):
    cfg, engine = migrated
    with engine.connect() as conn:
        assert MigrationContext.configure(conn).get_current_revision() == "0004"


def test_no_drift_between_models_and_migrations(migrated):
    _, engine = migrated
    with engine.connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": True}), Base.metadata)
    assert diff == [], f"Models and migrations disagree; write a migration for: {diff}"


def test_downgrade_removes_everything(migrated):
    cfg, engine = migrated
    command.downgrade(cfg, "base")
    assert set(inspect(engine).get_table_names()) <= {"alembic_version"}
