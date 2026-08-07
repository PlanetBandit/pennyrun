import os
import pytest
from db import migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("PENNYRUN_DB_URL"),
    reason="needs PENNYRUN_DB_URL pointing at a scratch database")

# `pennyrun_test` is a shared, persistent database -- migrations recorded in
# `public.schema_migration` by one test run stay there for the next one, so
# `apply()` only looks idempotent-from-empty the very first time anybody
# ever runs this suite. Give each test session its own schema instead: a
# deterministic name (the pid, not a uuid) so a crashed run's leftovers are
# still findable and droppable, created fresh and dropped CASCADE at
# teardown so `apply()` genuinely starts from nothing every run. The
# isolation lives here in the fixture, not in db/migrate.py -- production
# code stays exactly as it runs on the droplet, with no test-only knobs.
TEST_SCHEMA = f"pennyrun_test_{os.getpid()}"


@pytest.fixture(scope="session")
def test_schema():
    setup = migrate.connect()
    try:
        with setup.cursor() as cur:
            cur.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
            cur.execute(f"create schema {TEST_SCHEMA}")
        setup.commit()
    finally:
        setup.close()

    try:
        yield TEST_SCHEMA
    finally:
        teardown = migrate.connect()
        try:
            with teardown.cursor() as cur:
                cur.execute(f"drop schema if exists {TEST_SCHEMA} cascade")
            teardown.commit()
        finally:
            teardown.close()


@pytest.fixture
def conn(test_schema):
    """A plain migrate.connect() connection, pointed at the session's
    scratch schema via search_path. Unqualified DDL/DML in apply() and in
    the tests below then lands in that schema instead of `public` --
    including the schema_migration table that applied() creates itself."""
    c = migrate.connect()
    with c.cursor() as cur:
        cur.execute(f"set search_path to {test_schema}")
    c.commit()
    try:
        yield c
    finally:
        c.close()


def test_apply_is_idempotent(conn):
    first = migrate.apply(conn)
    assert "001_core.sql" in first
    second = migrate.apply(conn)
    assert second == []


def test_observation_rejects_bad_source(conn):
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9999') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t1','t') on conflict do nothing")
        with pytest.raises(Exception):
            cur.execute("insert into observation (item_id, store_id, source) "
                        "values ('t1','9999','nonsense')")
    conn.rollback()


def test_is_penny_is_computed(conn):
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9998') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t2','t') on conflict do nothing")
        cur.execute("insert into confirmation (item_id, store_id, scanned_price) "
                    "values ('t2','9998',0.01) returning is_penny")
        assert cur.fetchone()[0] is True
    conn.rollback()
