"""Recording what the store says about items it is NOT discounting.

Two things this buys, neither of which can be backfilled:

  A markdown that ENDED now says so. Before this, an item that came off
  clearance produced no row, so its last clearance row stayed newest for
  ever and the app kept offering a price that was gone.

  A trail. ACTIVE -> INACTIVE -> CLEARANCE only exists if somebody was
  writing it down at the time.
"""
import os

import pytest

from tools import sweep

pytestmark_db = pytest.mark.skipif(
    not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")


def _prod(status, clearance=None, item="1"):
    p = {"itemId": item, "fulfillment": {"anchorStoreStatusType": status},
         "pricing": {"value": 335.0,
                     "clearance": {"value": clearance, "dollarOff": 1.0}
                     if clearance is not None else None}}
    return p


# ------------------------------------------------------------- the collector

def test_status_row_carries_the_store_verdict():
    r = sweep.status_row(_prod("INACTIVE"), "2504", {"1": ["Drill", "Tools"]})
    assert r == {"item_id": "1", "store_id": "2504", "anchor_status": "INACTIVE",
                 "name": "Drill", "category": "Tools"}


def test_status_row_upper_cases_so_one_state_is_one_state():
    r = sweep.status_row(_prod("inactive"), "2504", {})
    assert r["anchor_status"] == "INACTIVE"


def test_no_status_means_no_row_rather_than_a_row_saying_nothing():
    for p in ({"itemId": "1"}, {"itemId": "1", "fulfillment": None},
              _prod(None), _prod("")):
        assert sweep.status_row(p, "2504", {}) is None


def test_priced_pairs_do_not_produce_a_status_row(monkeypatch):
    """A hit is always CLEARANCE, so storing it again would be a constant
    column. Only the unpriced pairs are worth the rows."""
    hit = _prod("CLEARANCE", clearance=1.20, item="A")
    miss = _prod("INACTIVE", item="B")
    monkeypatch.setattr(sweep, "call", lambda chunk, sid: [hit, miss])
    sink = []
    out = sweep._rows_from(["A", "B"], "2504", {}, {}, sink)
    assert len(out) == 1 and out[0][1] == "A"
    assert [s["item_id"] for s in sink] == ["B"]


def test_the_cold_pool_is_not_recorded(monkeypatch):
    """8,000 items at one rotating store with a measured 0.00% clearance
    rate -- thousands of near-constant rows a night for a population that
    never converts."""
    monkeypatch.setattr(sweep, "call", lambda chunk, sid: [_prod("ACTIVE", item="C")])
    out = sweep._rows_from(["C"], "2504", {}, {}, None)
    assert out == []


def test_the_sink_belongs_to_the_caller(monkeypatch):
    """scan() and tools/jobs.py both price stores through price_at. A
    module-level accumulator would pool one run into the next and upload
    everything twice."""
    monkeypatch.setattr(sweep, "call", lambda chunk, sid: [_prod("ACTIVE", item="D")])
    a, b = [], []
    sweep._rows_from(["D"], "2504", {}, {}, a)
    sweep._rows_from(["D"], "2504", {}, {}, b)
    assert len(a) == 1 and len(b) == 1, "one run must not see the other's rows"
    assert not hasattr(sweep, "_STATUS"), "no module-level accumulator may exist"


def test_upload_passes_a_status_row_through_untouched():
    from tools import upload
    st = {"item_id": "1", "store_id": "2504", "anchor_status": "INACTIVE",
          "name": None, "category": None}
    assert upload.to_observation(st) is st


def test_upload_still_formats_a_priced_row():
    from tools import upload
    row = ["Name", "1", "Tools", 1.2, 49.98, 98, "2504", 3, 0, "/p/1",
           "upc", "sku", "model", 0, None]
    got = upload.to_observation(row)
    assert got["clearance_price"] == "1.20" and got["item_id"] == "1"
