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

`check()` returns the parsed, sanitised fields rather than just
validating in place and discarding the result. Two things depend on
that:

- Money must never touch `float`. `list_price`/`clearance_price`/
  `pct_off` come back as the exact `Decimal` this module already built to
  do the bounds checks above -- `api/ingest.py` inserts that `Decimal`
  straight into `numeric(10,2)`/`numeric(5,2)` rather than re-parsing (or
  worse, letting a caller's raw `float` survive) the same string twice.
- The catalogue string fields (`category`, `canonical_url`, `upc`,
  `store_sku`, `model_number`, `replacement_id`) are *degraded*, not
  rejected: too long gets truncated, the wrong type or a control
  character becomes `None`, and a bare empty string is normalised to
  `None` too, but none of that ever raises. This is deliberately
  different from `name` (and from `item_id`/`store_id`/the money
  fields), which still raise on the same problems. `observation` is
  append-only with no delete path -- rejecting the whole row over a
  decorative field that happens to be the wrong shape would drop a
  price permanently, and re-drop it every night the same field recurs.
  `item_id`/`store_id` still raise because they're identity (a junk one
  is a permanent, wrong foreign key), and money still raises because a
  bad price is the one thing worth losing the row over. `None` (not an
  empty string) is what a degraded field becomes so it can never win a
  `coalesce(excluded.x, product.x)` upsert against a real value already
  on file the way a genuine `NULL` is designed to lose.
"""
import re
from decimal import Decimal, InvalidOperation

MAX_PRICE = Decimal("100000")
MIN_PRICE = Decimal("0.01")

ID_MAX_LEN = 64
ID_RE = re.compile(r"[A-Za-z0-9_-]+")
NAME_MAX_LEN = 200
CATEGORY_MAX_LEN = 64
URL_MAX_LEN = 300
CATALOG_FIELD_MAX_LEN = 64  # upc / store_sku / model_number / replacement_id
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


def _bounded_text(raw, field, max_len):
    """Hard rejection: length-capped, no control characters, or raise.
    Used only for `name` -- see `_degrade_text` for the six catalogue
    fields, which must never cost the row.

    An empty string is treated as "not sent" (`None`), not as a value --
    otherwise it would beat a real, already-stored value in the
    `coalesce(excluded.x, product.x)` upsert `api/ingest.py` and
    `db/seed.py` both use for `name`'s placeholder-healing equivalent.
    """
    if raw == "":
        return None
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > max_len:
        raise ValueError(f"{field} is not valid: {raw!r}")
    if CONTROL_CHAR_RE.search(raw):
        raise ValueError(f"{field} contains control characters: {raw!r}")
    return raw


def _degrade_text(raw, max_len):
    """Best-effort catalogue field: a bad value degrades the field, never
    the row. `observation` is append-only with no delete path, so raising
    here the way `_bounded_text` does for `name` would drop a price
    permanently over a decorative field that merely had the wrong shape
    -- Home Depot's own numeric-typed `upc`, or an overlong
    `canonical_url`, are realistic triggers (these six fields weren't
    sent to this endpoint at all before this fix wave, so this couldn't
    happen before it either).

    Too long is truncated -- the same spirit as the 78-char truncation
    `sweep.row()` already applies to `name` at collection time. The
    wrong type, `None`, an empty string, or a control character all
    become `None` instead: there's no well-defined prefix to keep for
    any of those, and `None` is also what keeps a bad value from
    winning a `coalesce(excluded.x, product.x)` upsert against a real
    value already on file.
    """
    if raw is None or raw == "":
        return None
    if not isinstance(raw, str):
        return None
    if CONTROL_CHAR_RE.search(raw):
        return None
    return raw[:max_len]


def check(obs):
    for required in ("item_id", "store_id"):
        value = obs.get(required)
        if not value:
            raise ValueError(f"{required} is required")
        _identifier(value, required)

    item_id = obs["item_id"]
    store_id = obs["store_id"]

    name = _bounded_text(obs.get("name"), "name", NAME_MAX_LEN)
    category = _degrade_text(obs.get("category"), CATEGORY_MAX_LEN)
    canonical_url = _degrade_text(obs.get("canonical_url"), URL_MAX_LEN)
    upc = _degrade_text(obs.get("upc"), CATALOG_FIELD_MAX_LEN)
    store_sku = _degrade_text(obs.get("store_sku"), CATALOG_FIELD_MAX_LEN)
    model_number = _degrade_text(obs.get("model_number"), CATALOG_FIELD_MAX_LEN)
    replacement_id = _degrade_text(obs.get("replacement_id"), CATALOG_FIELD_MAX_LEN)

    clearance = obs.get("clearance_price")
    listed = obs.get("list_price")

    # list_price is validated whenever it's present, not only when
    # clearance_price happens to need it for the ceiling comparison below
    # -- a clearance-less row is exactly the case where nothing else
    # would ever run it through `_money`.
    listed_d = _money(listed, "list_price") if listed is not None else None

    clearance_d = None
    if clearance is not None:
        c = _money(clearance, "clearance_price")
        if c < MIN_PRICE:
            raise ValueError(f"clearance_price below {MIN_PRICE}: {c}")
        if c > MAX_PRICE:
            raise ValueError(f"clearance_price above {MAX_PRICE}: {c}")
        if listed_d is not None and c > listed_d:
            raise ValueError("clearance_price above list price")
        clearance_d = c

    pct = obs.get("pct_off")
    pct_d = None
    if pct is not None:
        p = _money(pct, "pct_off")
        if not (0 <= p <= 100):
            raise ValueError(f"pct_off outside 0-100 percent: {p}")
        pct_d = p

    qty = obs.get("quantity")
    q = None
    if qty is not None:
        try:
            q = int(qty)
        except (TypeError, ValueError, OverflowError):
            raise ValueError(f"quantity is not a number: {qty!r}")
        if q < 0:
            raise ValueError(f"negative quantity: {qty}")

    return {
        "item_id": item_id, "store_id": store_id, "name": name,
        "category": category, "canonical_url": canonical_url, "upc": upc,
        "store_sku": store_sku, "model_number": model_number,
        "replacement_id": replacement_id,
        "list_price": listed_d, "clearance_price": clearance_d,
        "pct_off": pct_d, "quantity": q,
    }
