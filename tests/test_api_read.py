import os

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_SCHEMA

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
    seed.stores(conn, "pennyrun/hd-stores.json", "pennyrun/stores.json")
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


def test_stores_by_latlon_returns_real_stores_and_excludes_one_missing_lon(test_schema, client):
    """`lat is not null` alone lets a row with `lat` set and `lon` NULL
    through -- NULL propagates so `miles` comes back NULL rather than
    raising, and the row is returned (sorted last) as a store with no
    distance. `where lat is not null and lon is not null` is what keeps
    it out.

    This used to be the whole test, and it passed trivially: nothing in
    the codebase wrote real coordinates onto any of the 2,021 seeded
    stores, so the geo branch returned `[]` unconditionally and a
    half-coordinate row was "excluded" the same way every other row was.
    `db/seed.py`'s `stores()` now backfills lat/lon from
    `pennyrun/stores.json` by zip match for the ~91% of stores whose zip
    is unambiguous on both sides -- store 2502 (White Marsh, zip 21220)
    is one of them (`(39.35902, -76.44301)`) -- so asserting it comes
    back from a real geo lookup is what actually proves the endpoint
    works, not just that one synthetic bad row is filtered.
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
            r = client.get("/api/v1/stores?lat=39.35902&lon=-76.44301&n=50")
            assert r.status_code == 200
            body = r.json()
            assert any(s["store_id"] == "2502" for s in body), \
                "a store with real seeded coordinates must come back from a geo lookup"
            assert not any(s["store_id"] == "9001" for s in body)
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


# ---------------------------------------------------------------------------
# Department filtering. The point of these is the ORDERING interaction: paint
# is both the bulk of a store's clearance and the deepest-discounted, so it
# owns the top of `order by pct_off desc`. A caller that fetched a page and
# dropped paint itself would be filtering a paint-saturated prefix. These
# assert the exclusion happens before the limit is spent.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def dept_client():
    """Its own schema, not the session's shared one.

    These tests need products with categories they control, and `observation`
    is append-only by trigger -- a DELETE is rejected, so a fixture that
    writes into the shared schema can never clean up after itself. The seven
    products it adds would sit there for the rest of the session and
    test_seed's "3,000 rows deduplicate to 811 distinct items" would read
    818. Isolation is the only honest option; the extra migrate+seed is the
    price of it.
    """
    from db import migrate, seed
    from tests.conftest import _create_schema, _drop_schema

    schema = TEST_SCHEMA + "_depts"
    _create_schema(schema)
    conn = migrate.connect()
    with conn.cursor() as cur:
        cur.execute(f"set search_path to {schema}")
    conn.commit()
    migrate.apply(conn)
    seed.stores(conn, "pennyrun/hd-stores.json", "pennyrun/stores.json")

    with conn.cursor() as cur:
        # Deep paint on top, a shallow drill underneath: the exact shape that
        # makes client-side filtering of a fetched page useless.
        for i in range(5):
            cur.execute("insert into product (item_id, name, category)"
                        " values (%s,%s,'Paint')", (f"paint{i}", f"Paint {i}"))
            cur.execute(
                "insert into observation (item_id, store_id, list_price, clearance_price,"
                " pct_off, quantity, source, trusted)"
                " values (%s,'2577',100,2,98,4,'discovery',true)", (f"paint{i}",))
        cur.execute("insert into product (item_id, name, category)"
                    " values ('drill1','Drill','Tools')")
        cur.execute(
            "insert into observation (item_id, store_id, list_price, clearance_price,"
            " pct_off, quantity, source, trusted)"
            " values ('drill1','2577',100,60,40,1,'discovery',true)")
        # no category at all -- must stay reachable, not silently dropped
        cur.execute("insert into product (item_id, name, category)"
                    " values ('mystery1','Mystery',null)")
        cur.execute(
            "insert into observation (item_id, store_id, list_price, clearance_price,"
            " pct_off, quantity, source, trusted)"
            " values ('mystery1','2577',100,50,50,1,'discovery',true)")
    conn.commit()

    prev = os.environ.get("PENNYRUN_DB_SCHEMA")
    os.environ["PENNYRUN_DB_SCHEMA"] = schema
    try:
        from api.main import app
        yield TestClient(app)
    finally:
        if prev is None:
            os.environ.pop("PENNYRUN_DB_SCHEMA", None)
        else:
            os.environ["PENNYRUN_DB_SCHEMA"] = prev
        conn.close()
        _drop_schema(schema)


def test_categories_reports_every_department_with_its_count(dept_client):
    r = dept_client.get("/api/v1/store/2577/categories")
    assert r.status_code == 200
    counts = {c["category"]: c["n"] for c in r.json()}
    assert counts["Paint"] == 5
    assert counts["Tools"] == 1
    assert counts["Other"] == 1, "a null category must be counted, not dropped"
    assert list(counts)[0] == "Paint", "biggest pile first, so it is one tap away"


def test_excluding_a_department_removes_exactly_it(dept_client):
    all_rows = dept_client.get("/api/v1/store/2577/clearance?limit=50").json()
    kept = dept_client.get("/api/v1/store/2577/clearance?limit=50&exclude=Paint").json()
    assert len(all_rows) == 7
    assert len(kept) == 2
    assert {r["category"] for r in kept} == {"Tools", None}


def test_exclusion_happens_before_the_limit_is_spent(dept_client):
    """The whole reason this lives on the server.

    limit=2 unfiltered returns two paint rows and the drill is unreachable.
    Excluding paint must spend those same two rows on what is left -- not
    return an empty page because the prefix it would have filtered was all
    paint.
    """
    top = dept_client.get("/api/v1/store/2577/clearance?limit=2").json()
    assert [r["category"] for r in top] == ["Paint", "Paint"]

    kept = dept_client.get("/api/v1/store/2577/clearance?limit=2&exclude=Paint").json()
    assert len(kept) == 2
    assert "Paint" not in {r["category"] for r in kept}


def test_null_category_is_excludable_as_Other(dept_client):
    kept = dept_client.get("/api/v1/store/2577/clearance?limit=50&exclude=Other").json()
    assert all(r["category"] is not None for r in kept)
    assert len(kept) == 6


def test_excluding_several_departments_at_once(dept_client):
    kept = dept_client.get("/api/v1/store/2577/clearance?limit=50&exclude=Paint,Other").json()
    assert [r["category"] for r in kept] == ["Tools"]


def test_empty_exclude_changes_nothing(dept_client):
    """`exclude=` is what the app sends when nothing is switched off."""
    a = dept_client.get("/api/v1/store/2577/clearance?limit=50").json()
    b = dept_client.get("/api/v1/store/2577/clearance?limit=50&exclude=").json()
    assert [r["item_id"] for r in a] == [r["item_id"] for r in b]


def test_excluding_everything_is_empty_not_unfiltered(dept_client):
    """A filter that silently falls back to "show all" when it excludes
    everything is worse than one that returns nothing -- the app can explain
    an empty list, but it cannot detect a filter that quietly stopped."""
    kept = dept_client.get("/api/v1/store/2577/clearance?limit=50&exclude=Paint,Tools,Other").json()
    assert kept == []
