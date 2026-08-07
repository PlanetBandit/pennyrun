import os

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_SCHEMA

pytestmark = pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")
TOKEN = "test-token"

# `fresh_conn` (tests/conftest.py) gives each test its own throwaway schema,
# created and dropped just for that one test -- not the session's shared
# `test_schema` that test_api_read.py and friends write into. This module
# needs that: `observation` is append-only (no DELETE, no UPDATE, no
# TRUNCATE -- db/migrations/002_append_only.sql), so any observation these
# tests write through the endpoint can never be cleaned up with DML
# afterwards. Landing them in the shared schema would leave permanent rows
# there for the rest of the session -- e.g. inflating `product` past the
# exact counts tests/test_seed.py asserts, which is exactly what happened
# the first time this module used the brief's plain `migrate.connect()`
# fixture. `fresh_conn` drops the whole schema at teardown (DDL, not DML),
# so it doesn't care that the trigger blocks row-level cleanup -- there is
# nothing left to clean up.
#
# `fresh_conn`'s schema name is deterministic (`f"{TEST_SCHEMA}_fresh"`)
# but not exposed by the fixture itself, so it's recomputed here the same
# way conftest.py computes it, to point `PENNYRUN_DB_SCHEMA` at the same
# schema `fresh_conn` already created.
INGEST_SCHEMA = f"{TEST_SCHEMA}_fresh"


@pytest.fixture
def client(fresh_conn):
    from db import migrate, seed

    migrate.apply(fresh_conn)
    seed.stores(fresh_conn, "pennyrun/hd-stores.json")
    seed.products(fresh_conn, "pennyrun/clearance.json")

    os.environ["PENNYRUN_INGEST_TOKEN"] = TOKEN
    os.environ["PENNYRUN_DB_SCHEMA"] = INGEST_SCHEMA
    try:
        from api.main import app
        yield TestClient(app)
    finally:
        del os.environ["PENNYRUN_DB_SCHEMA"]
        del os.environ["PENNYRUN_INGEST_TOKEN"]


BODY = {"observations": [
    {"item_id": "204767783", "store_id": "2502", "list_price": "49.98",
     "clearance_price": "1.20", "pct_off": "98", "quantity": 3}]}


def test_rejects_without_token(client):
    assert client.post("/api/v1/discovery", json=BODY).status_code == 401


def test_rejects_wrong_token(client):
    r = client.post("/api/v1/discovery", json=BODY, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_accepts_and_marks_trusted(client):
    r = client.post("/api/v1/discovery", json=BODY,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    got = client.get("/api/v1/store/2502/clearance").json()
    assert any(row["item_id"] == "204767783" for row in got)


def test_bad_rows_are_counted_not_fatal(client):
    body = {"observations": [
        BODY["observations"][0],
        {"item_id": "204767783", "store_id": "2502", "clearance_price": "999999"}]}
    r = client.post("/api/v1/discovery", json=body,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["rejected"] == 1


# --- Landmine 1: an unknown item_id must not abort the whole batch. -------
#
# `observation.item_id` has a foreign key to `product`, and the collector's
# entire job is finding items `product` has never seen. A batch that mixes
# a genuinely new item with an already-known one must land both, not
# IntegrityError out of a single shared transaction and lose the whole
# batch.

NEW_ITEM = "999999999"  # not present anywhere in pennyrun/clearance.json


def test_discovery_of_a_brand_new_item_is_accepted_not_rejected(client):
    body = {"observations": [
        {"item_id": NEW_ITEM, "store_id": "2502", "list_price": "10.00",
         "clearance_price": "1.00", "pct_off": "90", "quantity": 1}]}
    r = client.post("/api/v1/discovery", json=body,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "rejected": 0}

    # The product row must now exist -- an unseeded item_id is not a
    # database error to work around, it's new inventory to record.
    got = client.get(f"/api/v1/item/{NEW_ITEM}")
    assert got.status_code == 200


def test_one_db_level_bad_row_does_not_cost_the_whole_batch(client):
    """A row that passes `validate.check` (bounds are fine) but fails at
    the database -- an unknown store_id, which is a real error, unlike an
    unknown item_id -- must be rejected on its own, leaving every other
    row in the same request unaffected. This is the scenario the brief's
    single shared `with rows() as cur:` transaction gets wrong: one
    IntegrityError there aborts every row, not just the bad one.
    """
    body = {"observations": [
        {"item_id": "204767783", "store_id": "2502", "list_price": "49.98",
         "clearance_price": "2.20", "pct_off": "95", "quantity": 2},
        {"item_id": "204767783", "store_id": "no-such-store",
         "list_price": "49.98", "clearance_price": "1.20", "pct_off": "98",
         "quantity": 1},
    ]}
    r = client.post("/api/v1/discovery", json=body,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "rejected": 1}


# --- Landmine 2: `obs_unique_idx` is keyed on transaction time. -----------
#
# `(item_id, store_id, observed_at)` with `observed_at default now()` --
# and Postgres's `now()` is frozen for the whole transaction. Two rows for
# the same item/store landing in one batch (the sweep's pool and hot-list
# can genuinely overlap) must not silently collapse into one under `on
# conflict do nothing`; and a resent batch must not be treated as if it
# were deduplicated when it plainly wasn't.


def _observation_count(conn):
    with conn.cursor() as cur:
        cur.execute("select count(*) from observation")
        return cur.fetchone()[0]


def test_same_item_and_store_twice_in_one_batch_both_persist(client, fresh_conn):
    before = _observation_count(fresh_conn)
    body = {"observations": [
        {"item_id": "204767783", "store_id": "2504", "list_price": "49.98",
         "clearance_price": "3.20", "pct_off": "93", "quantity": 1},
        {"item_id": "204767783", "store_id": "2504", "list_price": "49.98",
         "clearance_price": "3.20", "pct_off": "93", "quantity": 2},
    ]}
    r = client.post("/api/v1/discovery", json=body,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    reported_accepted = r.json()["accepted"]

    # The endpoint's own accepted count must equal what was actually
    # written -- never claim a row was accepted and then silently drop it.
    after = _observation_count(fresh_conn)
    assert after - before == reported_accepted
    assert reported_accepted == 2, (
        "both rows in the same batch for the same item/store must survive; "
        "the old `on conflict do nothing` against a same-`now()` unique "
        "index would silently collapse the second one")


def test_a_resent_batch_is_not_deduplicated(client, fresh_conn):
    """There is no request-level idempotency key in this schema, so a
    batch resent after a timeout is indistinguishable from a genuinely new
    batch of identical readings. The endpoint's guarantee on retry is
    at-least-once, not exactly-once: duplicates may land, but nothing is
    ever silently dropped to fake dedup.
    """
    body = {"observations": [
        {"item_id": "204767783", "store_id": "2577", "list_price": "49.98",
         "clearance_price": "4.20", "pct_off": "91", "quantity": 1}]}
    before = _observation_count(fresh_conn)

    r1 = client.post("/api/v1/discovery", json=body,
                     headers={"Authorization": f"Bearer {TOKEN}"})
    r2 = client.post("/api/v1/discovery", json=body,
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert r1.json()["accepted"] == 1
    assert r2.json()["accepted"] == 1

    after = _observation_count(fresh_conn)
    assert after - before == 2, "a retried batch inserts again; it is not deduped"
