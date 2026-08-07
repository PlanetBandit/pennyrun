"""Bounds every observation must clear, whoever sent it.

Discovery is trusted because we run it. Phones are not, because anyone
can POST. These rules apply to both — a bug in our own collector is as
capable of poisoning the list as a stranger is.

Every rejection here must surface as `ValueError` and nothing else --
`api/ingest.py` catches this module's contract to keep one bad row from
costing a whole chunk, and a leaked `TypeError`/`InvalidOperation`/
`AttributeError` defeats that. Two traps worth naming:

- `Decimal("nan")` does not raise. `Decimal(str(float("nan")))` builds a
  quiet NaN happily; it's the *comparison* (`c < MIN_PRICE`) that raises
  `decimal.InvalidOperation`, unlike `float`, where NaN comparisons just
  return `False`. Starlette parses request bodies with stdlib `json`,
  which accepts the bare `NaN`/`Infinity` tokens even though they aren't
  valid JSON -- so a NaN clearance price is one `json.loads` away from a
  caller, not a hypothetical. `_money` rejects non-finite `Decimal`s
  explicitly, before anything compares them.
- `int(qty)` on a non-scalar (`[]`, `{}`, `None` already excluded above)
  raises `TypeError`, not `ValueError`. Caught and re-raised as the
  latter here so callers only ever need to catch one exception type.

`item_id` and `store_id` are validated as identifiers, not just checked
for truthiness: a dict is truthy, an unbounded string is truthy, and
`product.item_id` is a `text primary key` that an `observation` row's
foreign key -- append-only, undeletable -- can pin in place forever once
written. A junk primary key from a bad discovery row would be permanent.
"""
import re
from decimal import Decimal, InvalidOperation

MAX_PRICE = Decimal("100000")
MIN_PRICE = Decimal("0.01")

ID_MAX_LEN = 64
ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
NAME_MAX_LEN = 200


def _money(raw, field):
    try:
        d = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError(f"{field} is not a number: {raw!r}")
    if not d.is_finite():
        raise ValueError(f"{field} is not a finite number: {raw!r}")
    return d


def _identifier(raw, field):
    if not isinstance(raw, str):
        raise ValueError(f"{field} must be a string: {raw!r}")
    if len(raw) > ID_MAX_LEN or not ID_RE.match(raw):
        raise ValueError(f"{field} is not a valid identifier: {raw!r}")
    return raw


def check(obs):
    for required in ("item_id", "store_id"):
        value = obs.get(required)
        if not value:
            raise ValueError(f"{required} is required")
        _identifier(value, required)

    name = obs.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > NAME_MAX_LEN):
        raise ValueError(f"name is not valid: {name!r}")

    clearance = obs.get("clearance_price")
    listed = obs.get("list_price")

    if clearance is not None:
        c = _money(clearance, "clearance_price")
        if c < MIN_PRICE:
            raise ValueError(f"clearance_price below {MIN_PRICE}: {c}")
        if c > MAX_PRICE:
            raise ValueError(f"clearance_price above {MAX_PRICE}: {c}")
        if listed is not None and c > _money(listed, "list_price"):
            raise ValueError("clearance_price above list price")

    pct = obs.get("pct_off")
    if pct is not None:
        p = _money(pct, "pct_off")
        if not (0 <= p <= 100):
            raise ValueError(f"pct_off outside 0-100 percent: {p}")

    qty = obs.get("quantity")
    if qty is not None:
        try:
            q = int(qty)
        except (TypeError, ValueError):
            raise ValueError(f"quantity is not a number: {qty!r}")
        if q < 0:
            raise ValueError(f"negative quantity: {qty}")
