"""Test setup: builds a fresh <dbname>_test database, migrates and seeds it once per run.

Each test gets a connection inside a transaction that is rolled back afterwards,
so tests never see each other's rows. Needs DATABASE_URL (from .env or the shell)
pointing at a Postgres where the user may CREATE DATABASE (the docker one can).
"""

from __future__ import annotations

import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from _env import database_url  # noqa: E402
from migrate import migrate  # noqa: E402
from seed_fields import apply_seed, load_seed  # noqa: E402


@pytest.fixture(scope="session")
def db_url():
    base = database_url()
    test_db = conninfo_to_dict(base).get("dbname", "blabzu") + "_test"
    admin = make_conninfo(base, dbname="postgres")
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(test_db)))
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(test_db)))
    url = make_conninfo(base, dbname=test_db)
    migrate(url, verbose=False)
    with psycopg.connect(url, autocommit=True) as conn:
        apply_seed(conn, load_seed())
    yield url
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(test_db)))


@pytest.fixture
def conn(db_url):
    with psycopg.connect(db_url) as c:
        c.execute("SELECT 1")   # open the test transaction so rejected() uses savepoints
        yield c
        c.rollback()


@contextmanager
def rejected(conn, error=psycopg.errors.CheckViolation, match: str | None = None):
    """Assert the block fails with `error`, without poisoning the test's transaction."""
    with pytest.raises(error, match=match):
        with conn.transaction():
            yield


@pytest.fixture
def user(conn):
    handle = "u_" + uuid.uuid4().hex[:8]
    return conn.execute(
        "INSERT INTO users (handle, display_name) VALUES (%s, %s) RETURNING id", (handle, handle)
    ).fetchone()[0]


@pytest.fixture
def make_note(conn, user):
    def _make(kind_id="beauty.sunscreen", **cols):
        values = {"author_id": user, "format": "photo", "kind_id": kind_id, "title": "Test note", **cols}
        names = list(values)
        query = sql.SQL("INSERT INTO notes ({}) VALUES ({}) RETURNING id").format(
            sql.SQL(", ").join(map(sql.Identifier, names)),
            sql.SQL(", ").join(sql.Placeholder() * len(names)),
        )
        return conn.execute(query, [values[n] for n in names]).fetchone()[0]
    return _make
