import os
from decimal import Decimal

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


def _create_schema(name):
    c = migrate.connect()
    try:
        with c.cursor() as cur:
            cur.execute(f"drop schema if exists {name} cascade")
            cur.execute(f"create schema {name}")
        c.commit()
    finally:
        c.close()


def _drop_schema(name):
    c = migrate.connect()
    try:
        with c.cursor() as cur:
            cur.execute(f"drop schema if exists {name} cascade")
        c.commit()
    finally:
        c.close()


def _connect_in(name):
    c = migrate.connect()
    with c.cursor() as cur:
        cur.execute(f"set search_path to {name}")
    c.commit()
    return c


@pytest.fixture(scope="session")
def test_schema():
    _create_schema(TEST_SCHEMA)
    try:
        yield TEST_SCHEMA
    finally:
        _drop_schema(TEST_SCHEMA)


@pytest.fixture
def conn(test_schema):
    """A plain migrate.connect() connection, pointed at the session's
    shared scratch schema via search_path. Unqualified DDL/DML on it,
    including the schema_migration table that applied() creates itself,
    lands in that schema instead of `public`. Several tests share this
    schema; none of them assert on apply()'s return value, only on side
    effects (constraint rejection, trigger rejection, generated-column
    value), so which order they run in doesn't matter."""
    c = migrate.connect()
    with c.cursor() as cur:
        cur.execute(f"set search_path to {test_schema}")
    c.commit()
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def fresh_conn():
    """Its own schema, created and dropped just for this one test -- not
    the shared session schema. apply()'s first-call/second-call return
    values are asserted here, so this needs a schema that is *actually*
    untouched by any other test, regardless of what order tests run in or
    whether a test earlier in the file (or a random-order plugin) already
    called apply() against the shared schema."""
    name = f"{TEST_SCHEMA}_fresh"
    _create_schema(name)
    c = _connect_in(name)
    try:
        yield c
    finally:
        c.close()
        _drop_schema(name)


def test_apply_reports_each_migration_once_then_noop(fresh_conn):
    first = migrate.apply(fresh_conn)
    assert first == ["001_core.sql", "002_append_only.sql"]
    second = migrate.apply(fresh_conn)
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


def test_observation_insert_succeeds(conn):
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9996') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t3','t') on conflict do nothing")
        cur.execute("insert into observation (item_id, store_id, source) "
                    "values ('t3','9996','discovery') returning id")
        assert cur.fetchone()[0] is not None
    conn.rollback()


def test_observation_update_raises(conn):
    # A savepoint around the blocked statement matters here, not just style:
    # once a statement errors, the whole transaction is aborted and no
    # further query can run on it until a rollback happens. Rolling back to
    # a savepoint set *after* the insert undoes only the failed UPDATE,
    # leaving the row queryable so we can prove it's still there, unchanged
    # -- not just that *some* exception was raised.
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9995') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t4','t') on conflict do nothing")
        cur.execute("insert into observation (item_id, store_id, source, clearance_price) "
                    "values ('t4','9995','discovery', 12.34)")
        cur.execute("savepoint sp_update")
        with pytest.raises(Exception):
            cur.execute("update observation set source = 'phone' where item_id = 't4'")
        cur.execute("rollback to savepoint sp_update")
        cur.execute("select source, clearance_price from observation where item_id = 't4'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "discovery"
        assert row[1] == Decimal("12.34")
    conn.rollback()


def test_observation_delete_raises(conn):
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9994') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t5','t') on conflict do nothing")
        cur.execute("insert into observation (item_id, store_id, source, clearance_price) "
                    "values ('t5','9994','discovery', 45.67)")
        cur.execute("savepoint sp_delete")
        with pytest.raises(Exception):
            cur.execute("delete from observation where item_id = 't5'")
        cur.execute("rollback to savepoint sp_delete")
        cur.execute("select clearance_price from observation where item_id = 't5'")
        row = cur.fetchone()
        assert row is not None
        assert row[0] == Decimal("45.67")
    conn.rollback()


def test_observation_truncate_raises(conn):
    # TRUNCATE never fires a row-level trigger, so this needs its own
    # coverage separate from update/delete -- see 002_append_only.sql.
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9993') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t6','t') on conflict do nothing")
        cur.execute("insert into observation (item_id, store_id, source) "
                    "values ('t6','9993','discovery')")
        cur.execute("select count(*) from observation")
        before = cur.fetchone()[0]
        cur.execute("savepoint sp_truncate")
        with pytest.raises(Exception):
            cur.execute("truncate observation")
        cur.execute("rollback to savepoint sp_truncate")
        cur.execute("select count(*) from observation")
        after = cur.fetchone()[0]
        assert after == before
        assert after >= 1
    conn.rollback()
