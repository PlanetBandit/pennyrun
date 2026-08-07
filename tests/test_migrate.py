import os
from decimal import Decimal

import pytest
from db import migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("PENNYRUN_DB_URL"),
    reason="needs PENNYRUN_DB_URL pointing at a scratch database")

# The `test_schema` / `conn` / `fresh_conn` fixtures used below live in
# tests/conftest.py now, shared with tests/test_seed.py. See the
# docstrings there for why each one exists.


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
