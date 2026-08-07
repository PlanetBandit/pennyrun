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
    # Infinity is always > MAX_PRICE, so a bare `pytest.raises(ValueError)`
    # here passes whether the rejection comes from `_money`'s finiteness
    # check or from the pre-existing `c > MAX_PRICE` bound -- it passed
    # against the pre-fix code too, and proved nothing about is_finite().
    # The message pins it to the specific path.
    bad = dict(GOOD)
    bad["clearance_price"] = float("inf")
    with pytest.raises(ValueError, match="not a finite number"):
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


# --- Review round 2 -----------------------------------------------------
#
# Critical: `^[A-Za-z0-9_-]+$` with `.match()` lets a single trailing
# `\n` through, because `$` is allowed to match just before a trailing
# newline rather than only at the true end of the string. One newline
# away from a distinct, permanent `product.item_id` -- fixed with
# `fullmatch` instead of an anchored `.match()`.


def test_rejects_item_id_with_a_trailing_newline():
    bad = dict(GOOD)
    bad["item_id"] = "204767783\n"
    with pytest.raises(ValueError):
        validate.check(bad)


def test_rejects_store_id_with_a_trailing_newline():
    bad = dict(GOOD)
    bad["store_id"] = "2502\n"
    with pytest.raises(ValueError):
        validate.check(bad)


# --- Review round 2 -----------------------------------------------------
#
# Important: `int(float("inf"))` raises `OverflowError`, not `TypeError`
# or `ValueError` -- and bare `Infinity` is JSON-legal to stdlib `json`,
# the same door `NaN` came through for Critical 1. It didn't 500 before
# this fix only because `api/ingest.py`'s catch-all also lists
# `ArithmeticError` (which `OverflowError` subclasses) -- defense in
# depth was doing load-bearing work it was never meant to.


def test_rejects_infinite_quantity():
    bad = dict(GOOD)
    bad["quantity"] = float("inf")
    with pytest.raises(ValueError, match="quantity"):
        validate.check(bad)


# --- Review round 2 -----------------------------------------------------
#
# Important: `list_price` only ever went through `_money` as part of the
# `clearance_price > list_price` ceiling check, so a clearance-less row
# never validated it at all. `numeric(10,2)` in Postgres accepts NaN
# happily, and `observation` is append-only -- a NaN `list_price` would
# have been permanent.


def test_rejects_nan_list_price_with_no_clearance_price():
    obs = {"item_id": "204767783", "store_id": "2502", "list_price": float("nan")}
    with pytest.raises(ValueError, match="not a finite number"):
        validate.check(obs)


def test_accepts_a_finite_list_price_with_no_clearance_price():
    obs = {"item_id": "204767783", "store_id": "2502", "list_price": "49.98"}
    validate.check(obs)


# --- Review round 2 -----------------------------------------------------
#
# `name` had a length cap but no charset rule -- a newline or other
# control character would be stored and served as-is to `/item/{id}` and
# `/store/{id}/clearance`, which is a display bug on a good day and a
# log-forging trick on a bad one.


def test_rejects_name_with_a_control_character():
    bad = dict(GOOD)
    bad["name"] = "Ryobi Drill\nfake log line: root logged in"
    with pytest.raises(ValueError, match="control"):
        validate.check(bad)
