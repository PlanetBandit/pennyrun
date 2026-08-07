"""End-to-end proof that a row the collector gathers survives the full
write path with nothing silently dropped along the way.

The branch review found three separate spots that each discarded part of
what `sweep.row()` produces -- `upload.to_observation` only sent 8 of its
15 fields, `api/ingest.py` only ever upserted `(item_id, name)`, and
money crossed `float` on the wire and (again) in the insert -- and
pointed out that all 124 tests passing at the time never actually caught
any of it: every ingest test sends a hand-built dict straight to the API,
every sweep test stops at `row()` or `upload.send()`, and nothing checked
what a phone would eventually read back out. This test walks the real
path front to back against a canned Home Depot payload:

    sweep.row()  ->  upload.to_observation()  ->  POST /api/v1/discovery
        ->  GET /store/{id}/clearance

so a regression at any one of those hops fails here even if every other
test in the suite still passes.
"""
import json
import os
import pathlib

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_SCHEMA
from tools import sweep, upload

pytestmark = pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")

FIX = pathlib.Path(__file__).parent / "fixtures"
TOKEN = "test-token"

# `fresh_conn` (tests/conftest.py) gives this module its own throwaway
# schema, the same way tests/test_api_ingest.py does -- `observation` is
# append-only, so anything posted through the endpoint here can never be
# cleaned up with DML afterwards, and landing it in the shared session
# schema would leave permanent rows behind for test_seed.py's exact-count
# assertions later in the session.
INGEST_SCHEMA = f"{TEST_SCHEMA}_fresh"


@pytest.fixture
def client(fresh_conn):
    from db import migrate, seed

    migrate.apply(fresh_conn)
    seed.stores(fresh_conn, "pennyrun/hd-stores.json", "pennyrun/stores.json")
    # products() is deliberately NOT seeded here -- the whole point of
    # this test is that the collector's own upload path, not the
    # one-time catalogue load, is what has to create this row, with
    # every field the review found missing intact.

    os.environ["PENNYRUN_INGEST_TOKEN"] = TOKEN
    os.environ["PENNYRUN_DB_SCHEMA"] = INGEST_SCHEMA
    try:
        from api.main import app
        yield TestClient(app)
    finally:
        del os.environ["PENNYRUN_DB_SCHEMA"]
        del os.environ["PENNYRUN_INGEST_TOKEN"]


def test_sweep_row_survives_upload_and_ingest_intact(client):
    product = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    # meta carries the (name, category) scan() would have built from the
    # pool/hot list -- row()'s category field ([2]) comes from here, not
    # from the product API response itself.
    meta = {"204767783": ["Black Mineral Surface Roll Low Slope Roofing", "Building"]}
    row = sweep.row(product, "2502", meta, {})
    assert row is not None

    obs = upload.to_observation(row)
    r = client.post("/api/v1/discovery", json={"observations": [obs]},
                     headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"accepted": 1, "rejected": 0}

    got = client.get("/api/v1/store/2502/clearance?limit=50")
    assert got.status_code == 200
    by_item = {x["item_id"]: x for x in got.json()}
    assert "204767783" in by_item, "the item collected above must be readable back"
    hit = by_item["204767783"]

    # exact money strings -- money never touches float on this path, on
    # the wire or in the numeric(10,2) column it lands in
    assert hit["clearance_price"] == "1.20"
    assert hit["list_price"] == "49.98"

    # fields the review found permanently NULL for every item discovered
    # after the one-time 811-product seed -- this item was never seeded
    # at all, so a non-null value here can only have come through the
    # collector's own upload path
    assert hit["category"] == "Building"
    assert hit["upc"] == "791308000105"
    assert hit["canonical_url"] == "/p/x/204767783"

    # the barcode-scan entry point (GET /api/v1/lookup?upc=) depends on
    # this same upc column -- prove it actually resolves a
    # collector-discovered item, not just a seeded one
    looked_up = client.get("/api/v1/lookup?upc=791308000105")
    assert looked_up.status_code == 200
    assert looked_up.json()["item_id"] == "204767783"
