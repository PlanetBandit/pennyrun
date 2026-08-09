import json
import pathlib
from tools import sweep

FIX = pathlib.Path(__file__).parent / "fixtures"


def test_row_uses_clearance_plus_dollar_off_as_was_price():
    """pricing.value can itself be a promotion; the discount is computed
    from clearance.value + dollarOff."""
    p = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    r = sweep.row(p, "2502", {}, {})
    assert r[3] == 1.2                      # clearance price
    assert round(r[4], 2) == 49.98          # was = 1.2 + 48.78
    assert r[5] == 98                       # percent off
    assert r[1] == "204767783"
    assert r[10] == "791308000105"          # upc
    assert r[11] == "418625"                # store sku


def test_row_reports_fall_when_price_dropped():
    p = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    r = sweep.row(p, "2502", {}, {"204767783@2502": 3.40})
    assert r[14] == 3.40


def test_row_has_no_fall_when_price_unchanged():
    p = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    r = sweep.row(p, "2502", {}, {"204767783@2502": 1.2})
    assert r[14] is None


# ---------------------------------------------------------------------------
# Shelf stock. Home Depot files two different numbers under `pickup`, both
# carrying this store's id with isAnchor set, and we recorded whichever came
# last. Two thirds of the quantities we hold are the wrong one.
# ---------------------------------------------------------------------------

def _fulfil(*services):
    return {"itemId": "1", "fulfillment": {"fulfillmentOptions": [
        {"type": "pickup", "fulfillable": True, "services": list(services)}]}}


def _svc(kind, loc_id, loc_type, qty):
    return {"type": kind, "locations": [
        {"locationId": loc_id, "isAnchor": True, "type": loc_type,
         "inventory": {"quantity": qty}}]}


def test_bopis_at_this_store_is_the_shelf_count():
    assert sweep.shelf_qty(_fulfil(_svc("bopis", "2504", "store", 3)), "2504") == 3


def test_ship_to_store_stock_is_not_shelf_stock():
    """The bug. `boss` is the online fulfilment pool, reported identically
    at every store -- one lawn fertiliser read 17,391 at five of them --
    and it arrives with this store's own id and isAnchor true, so neither
    could tell it apart from a shelf count."""
    p = _fulfil(_svc("boss", "2504", "online", 17391))
    assert sweep.shelf_qty(p, "2504") is None


def test_bopis_wins_when_both_are_offered():
    p = _fulfil(_svc("boss", "2504", "online", 17391),
                _svc("bopis", "2504", "store", 2))
    assert sweep.shelf_qty(p, "2504") == 2

    # and the other way round in the payload, since the old loop simply
    # kept the last match it saw
    p = _fulfil(_svc("bopis", "2504", "store", 2),
                _svc("boss", "2504", "online", 17391))
    assert sweep.shelf_qty(p, "2504") == 2


def test_a_neighbours_shelf_is_not_ours_even_when_it_is_the_anchor():
    """Neighbouring stores appear in the same list and isAnchor is not
    reliably ours, so the id is checked directly."""
    p = _fulfil(_svc("bopis", "2587", "store", 26))
    assert sweep.shelf_qty(p, "2504") is None

    p = {"itemId": "1", "fulfillment": {"fulfillmentOptions": [
        {"type": "pickup", "services": [{"type": "bopis", "locations": [
            {"locationId": "2587", "isAnchor": True, "type": "store",
             "inventory": {"quantity": 26}},
            {"locationId": "2504", "isAnchor": False, "type": "store",
             "inventory": {"quantity": 0}}]}]}]}}
    assert sweep.shelf_qty(p, "2504") == 0, "ours, not the anchor's"


def test_zero_on_the_shelf_is_recorded_as_zero_not_dropped():
    """A known-empty shelf is the most useful fact in the app -- it is what
    stops a wasted drive. It must not be confused with unknown."""
    assert sweep.shelf_qty(_fulfil(_svc("bopis", "2504", "store", 0)), "2504") == 0


def test_delivery_is_never_shelf_stock():
    p = {"itemId": "1", "fulfillment": {"fulfillmentOptions": [
        {"type": "delivery", "services": [
            _svc("sth", "8119", "online", 990),
            _svc("express delivery", "2504", "store", 1)]}]}}
    assert sweep.shelf_qty(p, "2504") is None


def test_missing_or_malformed_fulfillment_is_unknown_not_a_crash():
    for p in ({}, {"fulfillment": None}, {"fulfillment": {}},
              {"fulfillment": {"fulfillmentOptions": None}},
              {"fulfillment": {"fulfillmentOptions": [{"type": "pickup"}]}},
              {"fulfillment": {"fulfillmentOptions": [
                  {"type": "pickup", "services": [{"type": "bopis"}]}]}},
              _fulfil(_svc("bopis", "2504", "store", None))):
        assert sweep.shelf_qty(p, "2504") is None


def test_store_id_type_mismatch_still_matches():
    """store ids travel as text in our tables and sometimes as numbers in
    theirs; a type mismatch would silently report every shelf as unknown."""
    p = _fulfil(_svc("bopis", 2504, "store", 5))
    assert sweep.shelf_qty(p, "2504") == 5


def test_row_carries_the_shelf_count_not_the_warehouse_pool():
    import json
    import pathlib
    p = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    r = sweep.row(p, "2502", {}, {})
    assert r[7] == sweep.shelf_qty(p, "2502")


def test_against_a_real_captured_payload():
    """Home Depot's own response for two items at store 2565, captured as
    it came back. 204177723 is offered ship-to-store only and its `boss`
    location carries locationId "2565" with isAnchor true -- identical to a
    shelf count in every field the old query requested. 100267863 is really
    stocked there. The pool number is 4,995 at every store in the metro;
    the shelf number is 4."""
    import json
    payload = json.loads((FIX / "fulfillment_bopis_vs_boss.json").read_text())
    by_id = {p["itemId"]: p for p in payload["data"]["products"]}

    assert sweep.shelf_qty(by_id["204177723"], "2565") is None, \
        "the 4,995 ship-to-store pool is not stock on a shelf"
    assert sweep.shelf_qty(by_id["100267863"], "2565") == 4

    # and the field that gives it away is one the query used not to ask for
    boss = by_id["204177723"]["fulfillment"]["fulfillmentOptions"][0]["services"][0]
    loc = boss["locations"][0]
    assert boss["type"] == "boss" and loc["type"] == "online"
    assert loc["locationId"] == "2565" and loc["isAnchor"] is True, \
        "id and anchor both look like ours -- only the types differ"
