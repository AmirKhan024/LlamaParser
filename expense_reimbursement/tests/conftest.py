"""Points DATABASE_URL at expense_test (not the dev DB) and forces
PIPELINE_MODE=fake before server.py (and everything it imports) loads,
so the whole suite runs with no API keys or real extraction calls.
Every test gets a clean database via the autouse truncate fixture."""

import os
import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(BASE_DIR / ".env")
os.environ["DATABASE_URL"] = os.environ["TEST_DATABASE_URL"]
os.environ["PIPELINE_MODE"] = "fake"

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

import server  # noqa: E402
from db import get_engine, get_sessionmaker  # noqa: E402

UPLOADS_DIR = BASE_DIR / "uploads"
MOBILE_PDF = UPLOADS_DIR / "may26_mobile.pdf"
CONVEYANCE_PDF = UPLOADS_DIR / "May-26 Local conveyance.pdf"
APPROVAL_PDF = UPLOADS_DIR / "May-26 mail approval.pdf"


@pytest.fixture(scope="session", autouse=True)
def _migrate_test_db():
    """Runs `alembic upgrade head` against expense_test once per test
    session, before any test (session-scoped autouse fixtures run
    before function-scoped ones) -- so the suite works against a
    freshly created database (docker-compose up, no manual migration
    step) instead of requiring it as an undocumented setup step."""
    subprocess.run(["alembic", "upgrade", "head"], check=True, cwd=str(BASE_DIR), env=os.environ.copy())
    yield


@pytest.fixture(autouse=True)
def clean_db():
    engine = get_engine()
    with engine.begin() as conn:
        conn.execute(
            text(
                "TRUNCATE audit_events, corrections, check_results, extractions, "
                "documents, claims, employees RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def client():
    # Must be entered as a context manager: that's what starts (and
    # keeps alive) TestClient's own background event loop, which the
    # pipeline's asyncio.create_task() background tasks need in order to
    # actually run to completion between requests -- without it, a
    # scheduled task never gets scheduled and every upload stays
    # "processing" forever (confirmed directly: only the entered form
    # let a task make any progress at all).
    with TestClient(server.app) as c:
        yield c


@pytest.fixture
def db_session():
    session = get_sessionmaker()()
    try:
        yield session
    finally:
        session.close()
