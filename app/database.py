"""Database plumbing: the engine, a session factory and a FastAPI dependency.

The schema is owned by Alembic (backend/migrations). `create_tables()` exists
only for the test suite; the running app never creates or alters tables.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

from sqlalchemy import DateTime, MetaData, create_engine, text
from sqlalchemy.dialects import mysql
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy.types import TypeDecorator

from .config import get_settings


# Predictable constraint names, so migrations can find and change them later.
NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    # Every MySQL table: InnoDB (transactions, row locks) and full Unicode (emoji, ₹).
    __table_args__ = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator):
    """Store every time as UTC, hand back timezone-aware UTC datetimes.

    MySQL's DATETIME (and SQLite, used by the tests) has no timezone. So we
    convert to UTC and drop the zone on the way in, and put UTC back on the
    way out. Without this, comparing a stored time with `utcnow()` would raise.
    """

    impl = DateTime
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == "mysql":
            # DATETIME(6) keeps microseconds; plain DATETIME rounds to the second.
            return dialect.type_descriptor(mysql.DATETIME(fsp=6))
        return dialect.type_descriptor(DateTime())

    def process_bind_param(self, value, dialect):
        if value is not None and value.tzinfo is not None:
            value = value.astimezone(UTC).replace(tzinfo=None)
        return value

    def process_result_value(self, value, dialect):
        if value is not None and value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value


def make_engine(url: str, pool_size: int = 10, max_overflow: int = 10):
    kwargs: dict = {}
    if url.startswith("mysql"):
        # MySQL closes idle connections after `wait_timeout` (8h by default).
        # Recycling hourly means the pool never hands out a dead one.
        kwargs["pool_recycle"] = 3600
        kwargs["pool_size"] = pool_size
        kwargs["max_overflow"] = max_overflow
    if url.startswith("sqlite"):
        # SQLite is for tests and quick local runs. Let one connection cross
        # threads, and keep a single connection for in-memory databases.
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool
    return create_engine(url, pool_pre_ping=True, **kwargs)


_settings = get_settings()
engine = make_engine(_settings.database_url, _settings.db_pool_size, _settings.db_max_overflow)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def create_tables() -> None:
    """Tests only. Real databases are built with `alembic upgrade head`."""
    from . import models  # noqa: F401  (registers the tables on Base)

    Base.metadata.create_all(engine)


def ping() -> bool:
    """Can we reach the database right now? Used by /api/health."""
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


def schema_problem() -> str | None:
    """None if the database is at the newest migration, otherwise what to do about it.

    The app checks this at startup instead of creating tables itself, so a
    deploy that forgot to migrate fails loudly rather than half-working.
    """
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from .config import BACKEND_DIR

    head = ScriptDirectory.from_config(Config(str(BACKEND_DIR / "alembic.ini"))).get_current_head()
    with engine.connect() as conn:
        current = MigrationContext.configure(conn).get_current_revision()
    if current == head:
        return None
    return (f"Database schema is at {current or 'nothing'}, the code expects {head}. "
            "Run: .venv/Scripts/python -m alembic upgrade head")


def get_db() -> Iterator[Session]:
    """FastAPI dependency: one session per request, always closed."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
