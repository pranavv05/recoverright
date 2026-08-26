"""Repo-root fixtures — available to every test directory, not just tests/.

This file exists for one reason: `require_database`. Test modules live in four
places (tests/, policy/, execution/, evaluation/), and a fixture defined in
tests/conftest.py is invisible to the other three. A database guard that only
half the suite can reach is a guard that silently fails open, which is exactly
what happened: six tests reached Postgres with no skip and turned a missing
`docker compose up` into six errors instead of six skips.

The reachability probe is deliberately its own connection rather than
`database.connection.get_connection()`. It needs a short `connect_timeout`,
and the production helper correctly has no reason to offer one — without it a
down database costs the default TCP timeout (~130s) *per probe*, which is the
difference between a suite that skips in two seconds and one that appears to
hang.
"""

import os

import pytest
from dotenv import load_dotenv

# The application calls load_dotenv() at import time, so a test file that
# happens to import api.pipeline gets DATABASE_URL for free and one that does not
# silently skips every database test. Loading it at the root makes the
# environment identical no matter which subset of the suite is selected.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# Long enough for a local container that is still starting, short enough that
# "no database" is answered in seconds rather than minutes.
DB_PROBE_TIMEOUT_SECONDS = 3


def database_is_reachable() -> bool:
    """True when DATABASE_URL points at a Postgres that answers a SELECT 1."""
    url = os.environ.get("DATABASE_URL")
    if not url:
        return False
    try:
        import psycopg

        with psycopg.connect(url, connect_timeout=DB_PROBE_TIMEOUT_SECONDS) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


@pytest.fixture(scope="session")
def require_database():
    """Skips the test when no database is reachable, rather than erroring.

    Session-scoped so the probe runs at most once per suite, and a *fixture*
    rather than a module-level `skipif` condition so it runs at setup time
    rather than at collection time — a probe in a `skipif` blocks `pytest
    --collect-only`, `pytest -k`, and every other invocation that was never
    going to touch the database.
    """
    if not database_is_reachable():
        pytest.skip("no database reachable at DATABASE_URL — start it with docker compose up -d")
