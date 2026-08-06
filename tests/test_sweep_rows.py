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
