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
