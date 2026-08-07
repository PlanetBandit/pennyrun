import os

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")

# The brief's `client` fixture (task-7-brief.md) seeds `migrate.connect()`
# straight into `public` -- fine for a throwaway database, but this suite
# runs against the shared, persistent `pennyrun_test`. Route it through the
# same isolated-schema fixtures every other test module uses
# (tests/conftest.py: `test_schema`) so this module leaves the database
# exactly as clean as it found it, and set `PENNYRUN_DB_SCHEMA` so the
# API's own connections (api/db.py: `rows()`) land in that schema too --
# otherwise every endpoint queries an empty `public` and every assertion
# below fails in a way that looks like a broken endpoint rather than a
# fixture pointed at the wrong place.


@pytest.fixture(scope="module")
def client(test_schema):
    from db import migrate, seed

    conn = migrate.connect()
    with conn.cursor() as cur:
        cur.execute(f"set search_path to {test_schema}")
    conn.commit()

    migrate.apply(conn)
    seed.stores(conn, "pennyrun/hd-stores.json")
    seed.products(conn, "pennyrun/clearance.json")
    with conn.cursor() as cur:
        cur.execute(
            "insert into observation (item_id, store_id, list_price, clearance_price, "
            "pct_off, quantity, source, trusted) values "
            "('204767783','2502',49.98,1.20,98,3,'discovery',true) "
            "on conflict do nothing")
    conn.commit()

    os.environ["PENNYRUN_DB_SCHEMA"] = test_schema
    try:
        from api.main import app
        yield TestClient(app)
    finally:
        del os.environ["PENNYRUN_DB_SCHEMA"]
        conn.close()


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["stores"] == 2021


def test_stores_by_zip(client):
    r = client.get("/api/v1/stores?zip=21234")
    assert r.status_code == 200
    assert any(s["store_id"] == "2577" for s in r.json())


def test_stores_requires_zip_or_latlon(client):
    r = client.get("/api/v1/stores")
    assert r.status_code == 400


def test_stores_by_latlon_excludes_a_store_missing_lon(test_schema, client):
    """`lat is not null` alone lets a row with `lat` set and `lon` NULL
    through -- NULL propagates so `miles` comes back NULL rather than
    raising, and the row is returned (sorted last) as a store with no
    distance. `where lat is not null and lon is not null` is what keeps
    it out.
    """
    from db import migrate

    # `test_schema` is the shared, session-scoped schema every test module
    # seeds into (tests/conftest.py) -- test_seed.py asserts an exact row
    # count against it later in the session, so this synthetic row must
    # not outlive this test.
    conn = migrate.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(f"set search_path to {test_schema}")
            cur.execute(
                "insert into store (store_id, name, street, city, state, zip, lat, lon) "
                "values ('9001','Half Coords','1 Test Rd','Testville','MD','00000',39.4,null) "
                "on conflict (store_id) do update set lat = excluded.lat, lon = excluded.lon")
        conn.commit()

        try:
            r = client.get("/api/v1/stores?lat=39.4&lon=-76.6&n=50")
            assert r.status_code == 200
            assert not any(s["store_id"] == "9001" for s in r.json())
        finally:
            with conn.cursor() as cur:
                cur.execute("delete from store where store_id = '9001'")
            conn.commit()
    finally:
        conn.close()


def test_store_clearance_returns_the_seeded_row(client):
    r = client.get("/api/v1/store/2502/clearance?limit=50")
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["item_id"] == "204767783"
    assert rows[0]["clearance_price"] == "1.20"
    assert rows[0]["pct_off"] == "98.00"


def test_item_cross_store_compare(client):
    r = client.get("/api/v1/item/204767783?stores=2502,2504,2577")
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == "204767783"
    prices = {p["store_id"]: p for p in body["prices"]}
    assert prices["2502"]["clearance_price"] == "1.20"
    assert "observed_at" in prices["2502"], "staleness must be visible to the phone"


def test_schema_env_var_is_quoted_as_a_single_identifier_not_interpolated_as_sql(
        monkeypatch, client):
    """`PENNYRUN_DB_SCHEMA` reaches `SET search_path` through
    `psycopg.sql.Identifier`, not an f-string. A schema name containing a
    quote and a semicolon -- the classic "close the string, inject a
    statement" shape -- proves it: built with an f-string,
    `f"set search_path to {schema}"` would send
    `set search_path to sneaky; drop table observation; --` as one
    multi-statement query and the second statement would actually run.
    `sql.Identifier` folds the whole string into a single (nonexistent)
    quoted identifier instead, so nothing after the semicolon ever
    executes as a separate statement.
    """
    from api.db import rows

    malicious = 'sneaky"; drop table observation; --'
    # Scoped so PENNYRUN_DB_SCHEMA is back to the real test schema before
    # the client.get() below -- monkeypatch.setenv alone only reverts at
    # test teardown, which is too late to check the API still works.
    with monkeypatch.context() as m:
        m.setenv("PENNYRUN_DB_SCHEMA", malicious)
        with rows() as cur:
            cur.execute("show search_path")
            raw = cur.fetchone()["search_path"]
    # A double-quoted identifier: strip the outer quotes and undo the
    # `""` -> `"` escaping to recover exactly the string we set it to --
    # proof the whole thing was treated as one identifier, not SQL text.
    assert raw.startswith('"') and raw.endswith('"')
    assert raw[1:-1].replace('""', '"') == malicious

    # And nothing was actually dropped: the row the `client` fixture
    # seeded into the real test schema is still there.
    r = client.get("/api/v1/store/2502/clearance?limit=50")
    assert r.status_code == 200
    assert r.json()[0]["item_id"] == "204767783"


def test_lookup_by_upc(client):
    r = client.get("/api/v1/lookup?upc=791308000105")
    assert r.status_code == 200
    assert r.json()["item_id"] == "204767783"


def test_lookup_unknown_upc_is_404(client):
    assert client.get("/api/v1/lookup?upc=000000000000").status_code == 404
