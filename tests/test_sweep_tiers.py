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
    with pytest.raises(SystemExit):
        sweep.scan()
    assert set(attempted) == {STORES[0][0], STORES[1][0]}, (
        f"kept going after the wall: knocked on {sorted(set(attempted))}")


def test_a_refused_cold_sweep_does_not_advance_the_rotation(bench, monkeypatch):
    """Otherwise a slice that was never priced gets marked done and skipped
    for a full rotation.

    Seeded at scan_store=2 on purpose: with the default 0 this assertion is
    0 == 0 and passes even against code that never writes the field at all.
    """
    tmp, calls = bench
    (tmp / "cursor.json").write_text(json.dumps(
        {"shard": 0, "offset": 0, "store": 0, "scan_store": 2}))
    real = sweep.call
    cold_store = STORES[2][0]

    def cold_refused(chunk, sid, lite=False):
        if sid == cold_store and chunk[0].startswith("c"):
            raise hdclient.Refused("HTTP 206: Generic Errors API")
        return real(chunk, sid, lite=lite)

    monkeypatch.setattr(sweep, "call", cold_refused)
    sweep.scan()
    after = json.loads((tmp / "cursor.json").read_text()).get("scan_store")
    assert after == 2, "rotation advanced past a slice that was never priced"


def test_discover_does_not_clobber_the_scan_rotation(bench, monkeypatch):
    """discover() used to write a literal cursor dict, dropping scan_store.
    Since discover runs first in both collect.sh and the systemd timers, that
    reset the cold rotation to 0 every night -- so seven of eight stores never
    had their cold pool priced at all, ever, while the log cheerfully said
    'cold at <store 0> only' as though that were the plan."""
    tmp, _ = bench
    (tmp / "cursor.json").write_text(json.dumps(
        {"shard": 0, "offset": 0, "store": 1, "scan_store": 2}))

    # discover() has to RUN TO COMPLETION for this to mean anything -- the bug
    # is in the cursor write at the very end, so a discover that die()s early
    # (a refused sitemap, say) never touches it and the test would pass
    # against the defect.
    def fake_get(url, timeout=60):
        if url == sweep.SITEMAP:
            return "<loc>https://x/shard-0.xml</loc>"
        return "".join(f"<loc>https://x/p/thing-{i}/{100000+i}</loc>" for i in range(32))

    def fake_call(chunk, sid, lite=False):
        return [{"itemId": i, "identifiers": {"productLabel": f"n{i}"},
                 "taxonomy": {"breadCrumbs": [{"label": "Cat"}]},
                 "pricing": {"clearance": {"value": 1.0}}} for i in chunk]

    monkeypatch.setattr(sweep, "get", fake_get)
    monkeypatch.setattr(sweep, "call", fake_call)
    sweep.discover()

    cur = json.loads((tmp / "cursor.json").read_text())
    assert cur["scan_store"] == 2, (
        "discover ate the scan rotation -- the cold pool would be priced at "
        "store 0 every night, forever")
    assert cur["store"] == 2, "discover's own rotation stopped advancing"


def test_a_blocked_run_never_overwrites_a_fuller_list(bench, monkeypatch):
    """The ratchet. The 40%-of-last-time rail compares against a total that may
    itself have been truncated, so without an explicit guard a blocked night
    walks the list down 8 stores -> 4 -> 2 -> 1 and the rail never fires,
    because each drop is under 60%. save_prices() replaces prices.json
    wholesale too, so the skipped stores lose the baseline that powers the
    'price dropped overnight' signal."""
    tmp, _ = bench
    sweep.scan()                                   # night 1: full
    full = json.loads((tmp / "clearance.json").read_text())
    assert full["stores_n"] == len(STORES)

    real = sweep.call

    def blocked_after_two(chunk, sid, lite=False):
        if sid in (STORES[0][0], STORES[1][0]):
            return real(chunk, sid, lite=lite)
        raise hdclient.Refused("HTTP 206")

    monkeypatch.setattr(sweep, "call", blocked_after_two)
    with pytest.raises(SystemExit):
        sweep.scan()                               # night 2: blocked at store 3

    after = json.loads((tmp / "clearance.json").read_text())
    assert after["stores_n"] == full["stores_n"], "a partial run replaced a full one"
    assert after["total_hits"] == full["total_hits"]


def test_an_empty_hot_list_still_prices_every_store(bench, monkeypatch):
    """On a fresh install, or after a long block ages the hot list out, tiering
    would degenerate into a one-store scan that also never rebuilds a hot list
    to price everywhere later."""
    tmp, calls = bench
    (tmp / "hot.json").write_text("{}")
    sweep.scan()
    for sid, _ in STORES:
        assert ids_asked_at(calls, sid), f"store {sid} was never priced"
    out = json.loads((tmp / "clearance.json").read_text())
    assert out["stores_n"] == len(STORES)


def test_stores_n_counts_stores_actually_present_in_the_hits(bench, monkeypatch):
    """stores_n is derived from the hits rather than from len(stores), so it
    cannot claim coverage the run did not have.

    A run stopped by the circuit breaker no longer reaches this write at all
    (it die()s -- see test_a_blocked_run_never_overwrites_a_fuller_list), so
    the case this pins is the quieter one: a store that answers cleanly and
    genuinely has nothing on clearance contributes no rows, and must not be
    counted as covered."""
    tmp, _ = bench
    real = sweep.call

    def empty_at_delta(chunk, sid, lite=False):
        if sid == STORES[3][0]:
            return []          # answered fine, nothing marked down here
        return real(chunk, sid, lite=lite)

    monkeypatch.setattr(sweep, "call", empty_at_delta)
    sweep.scan()
    out = json.loads((tmp / "clearance.json").read_text())
    assert out["stores_n"] == 3, f"claimed {out['stores_n']} stores, 3 had hits"
