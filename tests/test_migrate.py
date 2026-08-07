import os
import pytest
from db import migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("PENNYRUN_DB_URL"),
    reason="needs PENNYRUN_DB_URL pointing at a scratch database")


def test_apply_is_idempotent():
    conn = migrate.connect()
    first = migrate.apply(conn)
    assert "001_core.sql" in first
    second = migrate.apply(conn)
    assert second == []


def test_observation_rejects_bad_source():
    conn = migrate.connect()
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9999') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t1','t') on conflict do nothing")
        with pytest.raises(Exception):
            cur.execute("insert into observation (item_id, store_id, source) "
                        "values ('t1','9999','nonsense')")
    conn.rollback()


def test_is_penny_is_computed():
    conn = migrate.connect()
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9998') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t2','t') on conflict do nothing")
        cur.execute("insert into confirmation (item_id, store_id, scanned_price) "
                    "values ('t2','9998',0.01) returning is_penny")
        assert cur.fetchone()[0] is True
    conn.rollback()
