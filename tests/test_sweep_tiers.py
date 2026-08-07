"""scan() prices two tiers at different rates, and stops at a wall.

Measured on the run of 2026-08-07, over the four stores that answered
before this address was cut off:

    hot list    1,288 items -> 1,173 on clearance somewhere   (91.1%)
    cold pool   8,097 items ->     0 on clearance anywhere    ( 0.00%)

Pricing the cold pool at every store spent 86% of the run's requests to
find nothing, and the run then fired 2,348 more at four stores that had
already stopped answering. These tests pin both fixes.
"""
import json

import pytest

from tools import hdclient, sweep


STORES = [["1001", "Alpha"], ["1002", "Bravo"], ["1003", "Charlie"], ["1004", "Delta"]]
# Sized to mirror the real ratio: the live pool is 1,288 hot against 8,097
# cold, so cold is ~6x hot. A fixture with cold ~= hot would make the saving
# look far smaller than it is.
HOT_IDS = [f"h{i}" for i in range(32)]      # 2 chunks of 16
COLD_IDS = [f"c{i}" for i in range(256)]    # 16 chunks of 16


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """A scan() wired to fake files and a recording fake gateway."""
    stores = tmp_path / "stores.json"
    stores.write_text(json.dumps(STORES))

    pool = {"pool": [[f"name {i}", i, "Cat"] for i in HOT_IDS + COLD_IDS]}
    hot = {i: {"name": f"name {i}", "cat": "Cat", "seen": sweep.TODAY} for i in HOT_IDS}
    (tmp_path / "pool.json").write_text(json.dumps(pool))
    (tmp_path / "hot.json").write_text(json.dumps(hot))
    (tmp_path / "cursor.json").write_text(json.dumps({"shard": 0, "offset": 0, "store": 0}))
    (tmp_path / "prices.json").write_text("{}")

    monkeypatch.setattr(sweep, "STORES", str(stores))
    monkeypatch.setattr(sweep, "POOL", str(tmp_path / "pool.json"))
    monkeypatch.setattr(sweep, "HOT", str(tmp_path / "hot.json"))
    monkeypatch.setattr(sweep, "CURSOR", str(tmp_path / "cursor.json"))
    monkeypatch.setattr(sweep, "PRICES", str(tmp_path / "prices.json"))
    monkeypatch.setattr(sweep, "OUT", str(tmp_path / "clearance.json"))
    monkeypatch.setattr(sweep, "SEED", str(tmp_path))
    monkeypatch.delenv("PENNYRUN_API", raising=False)
    monkeypatch.delenv("PENNYRUN_INGEST_TOKEN", raising=False)

    calls = []          # (store_id, tuple(ids)) per chunk actually requested

    def fake_call(chunk, sid, lite=False):
        calls.append((sid, tuple(chunk)))
        # every item is on clearance, so hits are never empty and the
        # safety rails don't fire for reasons unrelated to what's tested
        return [{"itemId": i,
                 "identifiers": {"productLabel": f"name {i}", "canonicalUrl": f"/p/{i}",
                                 "upc": "0", "storeSkuNumber": "0", "modelNumber": "0"},
                 "availabilityType": {"type": "Shared"},
                 "info": {"replacementOMSID": None},
                 "pricing": {"value": 10.0,
                             "clearance": {"value": 1.0, "dollarOff": 9.0,
                                           "percentageOff": 90.0}},
                 "fulfillment": None} for i in chunk]

    monkeypatch.setattr(sweep, "call", fake_call)
    return tmp_path, calls


def ids_asked_at(calls, sid):
    return {i for s, chunk in calls if s == sid for i in chunk}


def test_hot_list_is_priced_at_every_store(bench):
    _, calls = bench
    sweep.scan()
    for sid, _ in STORES:
        assert set(HOT_IDS) <= ids_asked_at(calls, sid), f"hot list missed store {sid}"


def test_cold_pool_is_priced_at_exactly_one_store(bench):
    """The whole point. Pricing it everywhere is 86% of the requests for 0% of
    the hits."""
    _, calls = bench
    sweep.scan()
    asked = [sid for sid, _ in STORES if ids_asked_at(calls, sid) & set(COLD_IDS)]
    assert len(asked) == 1, f"cold pool priced at {len(asked)} stores, expected 1"


def test_request_count_is_far_below_pricing_everything_everywhere(bench):
    _, calls = bench
    sweep.scan()
    flat = sweep.chunks_for(len(HOT_IDS) + len(COLD_IDS)) * len(STORES)
    assert len(calls) < flat / 2, f"{len(calls)} requests vs {flat} flat -- no real saving"


def test_the_rotation_advances_so_every_cold_item_is_rechecked(bench):
    tmp, calls = bench
    first = json.loads((tmp / "cursor.json").read_text()).get("scan_store", 0)
    sweep.scan()
    second = json.loads((tmp / "cursor.json").read_text())["scan_store"]
    assert second == (first + 1) % len(STORES)


def test_a_refused_store_stops_the_run_instead_of_firing_doomed_requests(bench, monkeypatch):
    """The 2026-08-07 run kept going through four fully-blocked stores,
    spending 2,348 requests that could not succeed."""
    tmp, calls = bench
    real = sweep.call
    attempted = []          # every store we KNOCKED on, refused or not --
                            # `calls` only records requests that succeeded

    def blocked_after_first(chunk, sid, lite=False):
        attempted.append(sid)
        if sid != STORES[0][0]:
            raise hdclient.Refused("HTTP 206: Generic Errors API")
        return real(chunk, sid, lite=lite)

    monkeypatch.setattr(sweep, "call", blocked_after_first)
    sweep.scan()
    assert set(attempted) == {STORES[0][0], STORES[1][0]}, (
        f"kept going after the wall: knocked on {sorted(set(attempted))}")
    # and it stopped inside the refused store too, not after grinding all of it
    assert attempted.count(STORES[1][0]) <= sweep.chunks_for(len(HOT_IDS))


def test_a_refused_cold_sweep_does_not_advance_the_rotation(bench, monkeypatch):
    """Otherwise a slice that was never priced gets marked done and skipped
    for a full rotation."""
    tmp, calls = bench
    real = sweep.call
    turn = json.loads((tmp / "cursor.json").read_text()).get("scan_store", 0)
    cold_store = STORES[turn][0]

    def cold_refused(chunk, sid, lite=False):
        if sid == cold_store and chunk[0].startswith("c"):
            raise hdclient.Refused("HTTP 206: Generic Errors API")
        return real(chunk, sid, lite=lite)

    monkeypatch.setattr(sweep, "call", cold_refused)
    sweep.scan()
    after = json.loads((tmp / "cursor.json").read_text()).get("scan_store", 0)
    assert after == turn, "rotation advanced past a slice that was never priced"


def test_stores_n_reports_stores_actually_covered(bench, monkeypatch):
    """A run stopped early covered fewer stores; the app must not be told
    it got all of them."""
    tmp, calls = bench
    real = sweep.call

    def blocked_after_first(chunk, sid, lite=False):
        if sid != STORES[0][0]:
            raise hdclient.Refused("HTTP 206")
        return real(chunk, sid, lite=lite)

    monkeypatch.setattr(sweep, "call", blocked_after_first)
    sweep.scan()
    out = json.loads((tmp / "clearance.json").read_text())
    assert out["stores_n"] == 1, f"claimed {out['stores_n']} stores, only 1 answered"
