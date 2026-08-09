import json
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


# --- Review round 1 -----------------------------------------------------
#
# Critical 1: `validate.check` used to leak `InvalidOperation` (NaN
# comparisons) and `TypeError` (`int([])`) past its `ValueError`
# contract, and `discovery()` only caught `ValueError` -- so one bad row
# 500'd the whole request. Task 9's collector has no retry, so a 500 here
# costs an entire chunk, not one row. Fixed at both ends: `validate.py`
# rejects non-finite `Decimal`s and non-scalar quantities as `ValueError`
# at the source, and `discovery()`'s catch is broadened as defense in
# depth. A malformed envelope (`observations` not a list) is a 400, not
# a fall-through to `AttributeError` -> 500.


def test_nan_clearance_price_is_rejected_not_500(client):
    # httpx's own `json=` convenience encoder refuses to serialize NaN
    # (`allow_nan=False`) -- stricter than what actually reaches the
    # server. stdlib `json.dumps` (`allow_nan=True`, the default) is what
    # Starlette's request parser uses, and what a real client sending a
    # bare `NaN` token would produce, so the raw body is built with it
    # directly rather than going through httpx's `json=` shortcut.
    body = {"observations": [
        BODY["observations"][0],
        {"item_id": "204767783", "store_id": "2502", "clearance_price": float("nan")}]}
    r = client.post("/api/v1/discovery", content=json.dumps(body),
                    headers={"Authorization": f"Bearer {TOKEN}",
                             "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "rejected": 1}


def test_non_scalar_quantity_is_rejected_not_500(client):
    body = {"observations": [
        BODY["observations"][0],
        {"item_id": "204767783", "store_id": "2502", "clearance_price": "1.00", "quantity": []}]}
    r = client.post("/api/v1/discovery", json=body,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "rejected": 1}


def test_malformed_envelope_is_400_not_500(client):
    r = client.post("/api/v1/discovery", json={"observations": "abc"},
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 400


def test_infinite_quantity_is_rejected_not_500(client):
    # Same trap as NaN, round 2: bare `Infinity` is JSON-legal to stdlib
    # `json`, `int(float("inf"))` raises `OverflowError`, and
    # `validate.py` must catch it at the source now rather than relying
    # on `api/ingest.py`'s broader catch to save the batch.
    body = {"observations": [
        BODY["observations"][0],
        {"item_id": "204767783", "store_id": "2502", "clearance_price": "1.00",
         "quantity": float("inf")}]}
    r = client.post("/api/v1/discovery", content=json.dumps(body),
                    headers={"Authorization": f"Bearer {TOKEN}",
                             "Content-Type": "application/json"})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "rejected": 1}


# --- Review round 1 -----------------------------------------------------
#
# Important 1: Starlette decodes headers as latin-1, so any header byte
# above 0x7F produces a non-ASCII `str`, and `hmac.compare_digest` raises
# `TypeError` on non-ASCII `str` operands. An unauthenticated request
# must 401, never 500 -- compared as bytes now, not `str`.


def test_non_ascii_auth_header_is_401_not_500(client):
    # httpx's `headers=` dict encodes `str` values as strict ASCII
    # client-side and refuses non-ASCII outright -- passing raw bytes
    # instead is what actually reaches the wire (and what Starlette then
    # decodes as latin-1 on the way back in), reproducing the real
    # scenario rather than being blocked a layer too early.
    r = client.post("/api/v1/discovery", json=BODY,
                    headers={"Authorization": ("Bearer " + "\xe9\xe9\xe9").encode("latin-1")})
    assert r.status_code == 401


# --- Review round 1 -----------------------------------------------------
#
# Critical 2: the placeholder name (`"(discovered) <item_id>"`) used to
# be the only path -- nothing ever wrote a real name over it, so every
# item the collector found without a name attached would keep that
# placeholder forever, and that string is what `/store/{id}/clearance`
# and `/item/{id}` serve to a phone. `INSERT_PRODUCT` now heals a
# placeholder the first time a real name arrives, and never lets a
# later placeholder clobber a real name that's already there.


def test_real_name_heals_a_placeholder(client):
    item_id = "555000111"
    no_name = {"observations": [
        {"item_id": item_id, "store_id": "2502", "clearance_price": "1.00"}]}
    with_name = {"observations": [
        {"item_id": item_id, "store_id": "2504", "clearance_price": "2.00",
         "name": "Real Product Name"}]}

    r1 = client.post("/api/v1/discovery", json=no_name,
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert r1.json()["accepted"] == 1
    assert client.get(f"/api/v1/item/{item_id}").json()["name"] == f"(discovered) {item_id}"

    r2 = client.post("/api/v1/discovery", json=with_name,
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert r2.json()["accepted"] == 1
    assert client.get(f"/api/v1/item/{item_id}").json()["name"] == "Real Product Name"


def test_placeholder_never_clobbers_a_real_name(client):
    item_id = "555000222"
    with_name = {"observations": [
        {"item_id": item_id, "store_id": "2502", "clearance_price": "1.00",
         "name": "Real Product Name"}]}
    no_name = {"observations": [
        {"item_id": item_id, "store_id": "2504", "clearance_price": "2.00"}]}

    r1 = client.post("/api/v1/discovery", json=with_name,
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert r1.json()["accepted"] == 1
    assert client.get(f"/api/v1/item/{item_id}").json()["name"] == "Real Product Name"

    r2 = client.post("/api/v1/discovery", json=no_name,
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert r2.json()["accepted"] == 1
    assert client.get(f"/api/v1/item/{item_id}").json()["name"] == "Real Product Name"


# --- Re-review, Medium 2 -------------------------------------------------
#
# A bad *catalogue* field (category/canonical_url/upc/store_sku/
# model_number/replacement_id) must degrade, not reject -- `observation`
# is append-only, so rejecting the whole row over a decorative field
# would drop the price permanently. Proven here at the API layer, not
# just in api/validate.py's own unit tests: the row must still be
# accepted, and the price must still be readable back.


def test_bad_catalogue_field_degrades_but_the_row_and_price_still_land(client):
    item_id = "555000333"
    body = {"observations": [
        {"item_id": item_id, "store_id": "2502", "clearance_price": "1.00",
         "list_price": "2.00", "upc": 791308000105,  # numeric, not a string
         "canonical_url": "x" * 1000}]}  # far past URL_MAX_LEN

    r = client.post("/api/v1/discovery", json=body,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "rejected": 0}

    got = client.get(f"/api/v1/item/{item_id}")
    assert got.status_code == 200
    assert got.json()["upc"] is None
    assert got.json()["prices"][0]["clearance_price"] == "1.00"


# ---------------------------------------------------------------------------
# Observations with no clearance price. An item that came OFF clearance
# produced no row before this, so its last clearance row stayed newest for
# ever and the app kept offering a price that had ended.
# ---------------------------------------------------------------------------

def test_an_unpriced_observation_is_accepted_and_stored(client):
    r = client.post("/api/v1/discovery", json={"observations": [
        {"item_id": "204767783", "store_id": "2502", "anchor_status": "INACTIVE"}]},
        headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["accepted"] == 1

    from api.db import rows
    with rows() as cur:
        cur.execute("select anchor_status, clearance_price from observation"
                    " where item_id = '204767783' and store_id = '2502'"
                    " order by observed_at desc limit 1")
        got = cur.fetchone()
    assert got["anchor_status"] == "INACTIVE" and got["clearance_price"] is None


def test_an_ended_markdown_stops_being_served(client):
    """The behaviour change that makes this worth doing."""
    hdr = {"Authorization": f"Bearer {TOKEN}"}
    client.post("/api/v1/discovery", json={"observations": [
        {"item_id": "204767783", "store_id": "2502", "name": "Thing",
         "clearance_price": "1.20", "list_price": "49.98", "pct_off": 98,
         "anchor_status": "CLEARANCE"}]}, headers=hdr)
    before = client.get("/api/v1/store/2502/clearance?limit=50").json()
    assert any(x["item_id"] == "204767783" for x in before), "on clearance"

    client.post("/api/v1/discovery", json={"observations": [
        {"item_id": "204767783", "store_id": "2502", "anchor_status": "ACTIVE"}]},
        headers=hdr)
    after = client.get("/api/v1/store/2502/clearance?limit=50").json()
    assert not any(x["item_id"] == "204767783" for x in after), \
        "the markdown ended -- it must stop being offered"


def test_status_is_upper_cased_so_one_state_is_one_state(client):
    client.post("/api/v1/discovery", json={"observations": [
        {"item_id": "204767783", "store_id": "2577", "anchor_status": "inactive"}]},
        headers={"Authorization": f"Bearer {TOKEN}"})
    from api.db import rows
    with rows() as cur:
        cur.execute("select anchor_status from observation where store_id = '2577'"
                    " order by observed_at desc limit 1")
        assert cur.fetchone()["anchor_status"] == "INACTIVE"
