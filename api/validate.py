"""Bounds every observation must clear, whoever sent it.

Discovery is trusted because we run it. Phones are not, because anyone
can POST. These rules apply to both — a bug in our own collector is as
capable of poisoning the list as a stranger is.

Every rejection here must surface as `ValueError` and nothing else --
`api/ingest.py` catches this module's contract to keep one bad row from
costing a whole chunk, and a leaked `TypeError`/`InvalidOperation`/
`AttributeError`/`OverflowError` defeats that. Traps worth naming:

- `Decimal("nan")` does not raise. `Decimal(str(float("nan")))` builds a
  quiet NaN happily; it's the *comparison* (`c < MIN_PRICE`) that raises
  `decimal.InvalidOperation`, unlike `float`, where NaN comparisons just
  return `False`. Starlette parses request bodies with stdlib `json`,
  which accepts the bare `NaN`/`Infinity` tokens even though they aren't
  valid JSON -- so a NaN clearance price is one `json.loads` away from a
  caller, not a hypothetical. `_money` rejects non-finite `Decimal`s
  explicitly, before anything compares them, and every money field --
  `list_price` included -- runs through it whenever it's present, not
  only when a comparison happens to need it. A clearance-less row with a
  NaN `list_price` used to sail straight past `check()` into a
  `numeric(10,2)` column that (unlike this module) is happy to store NaN
  forever.
- `int(qty)` on a non-scalar (`[]`, `{}`, `None` already excluded above)
  raises `TypeError`, and `int(float("inf"))` raises `OverflowError` --
  bare `Infinity` is JSON-legal to stdlib `json`, the same door `NaN`
  came through. Both are caught and re-raised as `ValueError` here so
  callers only ever need to catch one exception type.

`item_id` and `store_id` are validated as identifiers, not just checked
for truthiness: a dict is truthy, an unbounded string is truthy, and
`product.item_id` is a `text primary key` that an `observation` row's
foreign key -- append-only, undeletable -- can pin in place forever once
written. A junk primary key from a bad discovery row would be permanent.
`re.fullmatch`, not `.match` with a trailing `$`: `$` matches just
*before* a trailing `\n`, so `"^[A-Za-z0-9_-]+$".match("204767783\n")`
was `True` -- one newline away from a distinct, permanent primary key.
`fullmatch` requires the match to cover the entire string, so the
trailing character the charset itself rejects has nowhere left to hide.

`name` gets the same treatment against control characters for a
different reason: it isn't a key, but it's the phone display for
`/store/{id}/clearance` and `/item/{id}`, and an unescaped `\n` or other
control character there is a display bug (or a log-forging trick) that
we'd otherwise be trusting the collector never to send.
"""
import re
from decimal import Decimal, InvalidOperation

MAX_PRICE = Decimal("100000")
MIN_PRICE = Decimal("0.01")

ID_MAX_LEN = 64
ID_RE = re.compile(r"[A-Za-z0-9_-]+")
NAME_MAX_LEN = 200
CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


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
    if len(raw) > ID_MAX_LEN or not ID_RE.fullmatch(raw):
        raise ValueError(f"{field} is not a valid identifier: {raw!r}")
    return raw


def check(obs):
    for required in ("item_id", "store_id"):
        value = obs.get(required)
        if not value:
            raise ValueError(f"{required} is required")
        _identifier(value, required)

    name = obs.get("name")
    if name is not None:
        if not isinstance(name, str) or len(name) > NAME_MAX_LEN:
            raise ValueError(f"name is not valid: {name!r}")
        if CONTROL_CHAR_RE.search(name):
            raise ValueError(f"name contains control characters: {name!r}")

    clearance = obs.get("clearance_price")
    listed = obs.get("list_price")

    # list_price is validated whenever it's present, not only when
    # clearance_price happens to need it for the ceiling comparison below
    # -- a clearance-less row is exactly the case where nothing else
    # would ever run it through `_money`.
    listed_d = _money(listed, "list_price") if listed is not None else None

    if clearance is not None:
        c = _money(clearance, "clearance_price")
        if c < MIN_PRICE:
            raise ValueError(f"clearance_price below {MIN_PRICE}: {c}")
        if c > MAX_PRICE:
            raise ValueError(f"clearance_price above {MAX_PRICE}: {c}")
        if listed_d is not None and c > listed_d:
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
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"quantity is not a number: {qty!r}")
        if q < 0:
            raise ValueError(f"negative quantity: {qty}")
