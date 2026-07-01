"""Pytest fixtures for unit and integration tests.

Default backend: SQLite (aiosqlite) — no external DB required.
Set TEST_DATABASE_URL=postgresql+asyncpg://... to run against a real Postgres DB.

Each test function gets an AsyncSession whose transaction is rolled back
after the test, keeping tests isolated without recreating tables each time.
"""
from __future__ import annotations

import asyncio
import os

# Set required env vars BEFORE any app imports so pydantic-settings validation passes.
os.environ.setdefault("META_VERIFY_TOKEN", "test_token")
os.environ.setdefault("META_APP_SECRET", "test_secret_32bytes_padding_here!")
os.environ.setdefault("META_ACCESS_TOKEN", "test_access_token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "123456789")
os.environ.setdefault("ADMIN_WHATSAPP_NUMBER", "+10000000000")
os.environ.setdefault("ANTHROPIC_API_KEY", "test_anthropic_key")
os.environ.setdefault("ENABLE_METRICS", "false")  # don't start prometheus server in tests
os.environ.setdefault("SUPERADMIN_KEY", "test-superadmin-key")
# Use SQLite in-memory by default; override with TEST_DATABASE_URL for Postgres
_DEFAULT_DB = "sqlite+aiosqlite:///:memory:"
os.environ.setdefault("DATABASE_URL", os.environ.get("TEST_DATABASE_URL", _DEFAULT_DB))

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool, StaticPool

from app.db.models import Base

TEST_DATABASE_URL = os.environ["DATABASE_URL"]
_IS_SQLITE = TEST_DATABASE_URL.startswith("sqlite")


def _make_engine():
    """Create an async engine appropriate for the test backend."""
    if _IS_SQLITE:
        # StaticPool keeps the single in-memory database alive across connections
        return create_async_engine(
            TEST_DATABASE_URL,
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
    return create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)


@pytest.fixture(scope="session")
def event_loop():
    """Single event loop for the whole test session."""
    policy = asyncio.get_event_loop_policy()
    loop = policy.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture(scope="session")
async def test_engine():
    """Create DB schema once per session, tear down after."""
    engine = _make_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(test_engine) -> AsyncSession:
    """Per-test session: wraps work in a transaction that rolls back on teardown."""
    connection = await test_engine.connect()
    transaction = await connection.begin()
    session = AsyncSession(bind=connection, expire_on_commit=False)
    yield session
    await session.close()
    await transaction.rollback()
    await connection.close()


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture(autouse=True)
def _reset_rate_limit_state():
    """Auth-failure lockout state is a module-level global — clear it between
    tests so one test's intentional 401s don't trip the lockout in another."""
    from app import rate_limit

    rate_limit.reset_all()
    yield
    rate_limit.reset_all()
