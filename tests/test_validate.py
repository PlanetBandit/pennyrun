import pytest
from api import validate

GOOD = {"item_id": "204767783", "store_id": "2502", "list_price": "49.98",
        "clearance_price": "1.20", "pct_off": "98", "quantity": 3}


def test_accepts_a_good_row():
    validate.check(dict(GOOD))


@pytest.mark.parametrize("field,value,msg", [
    ("clearance_price", "60.00", "above list"),
    ("clearance_price", "-1", "below"),
    ("clearance_price", "200000", "above"),
    ("pct_off", "140", "percent"),
    ("pct_off", "-5", "percent"),
    ("quantity", -3, "quantity"),
])
def test_rejects_impossible_values(field, value, msg):
    bad = dict(GOOD)
    bad[field] = value
    with pytest.raises(ValueError, match=msg):
        validate.check(bad)


def test_rejects_missing_ids():
    with pytest.raises(ValueError, match="item_id"):
        validate.check({"store_id": "2502", "clearance_price": "1.00"})


# --- Review round 1, Critical 1 --------------------------------------
#
# `Decimal("nan")` does not raise on construction -- only the later
# ordering comparison (`c < MIN_PRICE`) does, with `InvalidOperation`,
# which is not a `ValueError` and used to escape `check()` entirely.
# Starlette parses request bodies with stdlib `json`, which accepts the
# bare `NaN` token even though it isn't valid JSON, so this is reachable
# from a real request body, not just a unit test fixture.


def test_rejects_nan_clearance_price():
    bad = dict(GOOD)
    bad["clearance_price"] = float("nan")
    with pytest.raises(ValueError):
        validate.check(bad)


def test_rejects_infinite_clearance_price():
    bad = dict(GOOD)
    bad["clearance_price"] = float("inf")
    with pytest.raises(ValueError):
        validate.check(bad)


def test_rejects_non_scalar_quantity():
    # int([]) raises TypeError, not ValueError -- used to escape check().
    bad = dict(GOOD)
    bad["quantity"] = []
    with pytest.raises(ValueError):
        validate.check(bad)


# --- Review round 1, Important 3 --------------------------------------
#
# `product.item_id` is a `text primary key`; once any `observation`
# references it, the append-only trigger and the foreign key make that
# row permanent. A junk primary key from a bad discovery row would be
# permanent too, so identifiers are bounded, not just checked truthy.


def test_rejects_overlong_item_id():
    bad = dict(GOOD)
    bad["item_id"] = "9" * (validate.ID_MAX_LEN + 1)
    with pytest.raises(ValueError):
        validate.check(bad)


def test_rejects_item_id_with_disallowed_characters():
    bad = dict(GOOD)
    bad["item_id"] = "204767783; drop table product;"
    with pytest.raises(ValueError):
        validate.check(bad)


def test_rejects_non_string_item_id():
    # A dict is truthy -- `not obs.get("item_id")` alone lets it through.
    bad = dict(GOOD)
    bad["item_id"] = {"a": 1}
    with pytest.raises(ValueError):
        validate.check(bad)


def test_rejects_overlong_name():
    bad = dict(GOOD)
    bad["name"] = "x" * (validate.NAME_MAX_LEN + 1)
    with pytest.raises(ValueError):
        validate.check(bad)


def test_accepts_a_reasonable_name():
    good = dict(GOOD)
    good["name"] = "3 ft. x 33.3 ft. Black Mineral Surface Roll"
    validate.check(good)
