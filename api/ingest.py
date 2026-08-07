"""The write path: what the home collector uploads after a sweep.

Several things worth writing down so nobody "fixes" them back the wrong
way.

1. `observation.item_id` has a foreign key to `product`. Discovering an
   item `product` has never seen is the collector's entire job, not an
   error condition -- so each row upserts its product first instead of
   assuming Task 6's one-time seed already covers it. If the observation
   insert then fails for a reason that *is* a real error -- an unknown
   `store_id`, which is a fixed, curated list and not something the
   collector "discovers" the way it discovers products -- only that
   row's work rolls back, via a `SAVEPOINT`. The rest of the batch,
   already-executed statements included, is untouched. One malformed row
   costs one row, never the batch.

   That savepoint only means anything if a transaction is already open
   around it -- `conn.transaction()` promotes itself to a top-level
   `BEGIN`/`COMMIT` when called on an idle connection, which is exactly
   what a production connection is at the start of every request (no
   `PENNYRUN_DB_SCHEMA`-triggered `SET search_path` has opened one
   first, the way it incidentally does under the test fixtures). Without
   an explicit outer transaction, every row in production would silently
   become its own top-level commit -- one fsync per row, ~1000 per
   chunk, and a claim about nesting in this docstring that was only ever
   true in tests. The `with conn.transaction():` wrapping the whole loop
   below exists so the inner, per-row `conn.transaction()` is *always* a
   real `SAVEPOINT`, in both environments, and a batch costs one commit,
   not one per row.

2. `obs_unique_idx` is `(item_id, store_id, observed_at)`, and Postgres's
   `now()` -- the column's default -- is frozen for the whole
   transaction. Leaving `observed_at` to the default means every row in
   one batch that shares an item and store collides on that index, and
   the sweep's pool and hot-list genuinely do overlap. `clock_timestamp()`
   is used explicitly instead: it reads the wall clock at each statement,
   not at transaction start, so same-batch rows for the same item/store
   get distinct timestamps and both survive.
   That fixes collisions *within* a batch. It does not, and cannot,
   fix collisions *across* requests: a batch resent after a timeout gets
   fresh `clock_timestamp()` values and inserts again rather than
   deduplicating against the earlier attempt, because nothing in this
   schema identifies "this is the same batch as before." The guarantee on
   retry is therefore at-least-once, not exactly-once -- a resend may
   double-write, but it will never look like a resend was silently
   absorbed when it wasn't. `on conflict do nothing` is deliberately not
   used anywhere in this file: it would hide that data loss instead of
   surfacing it, and a hidden drop is worse than a duplicate row or a
   counted rejection.

3. `validate.check`'s contract is "bad input raises `ValueError`, nothing
   else" -- but that contract can be violated by input we don't control
   (a bug in `validate.py`, or a shape it hasn't been taught about yet),
   and when the discovery collector has no retry (Task 9's `upload.send`
   doesn't), an uncaught exception here doesn't just reject one row, it
   500s the whole chunk. The `except` around `validate.check` is
   therefore broader than `ValueError` alone, as defense in depth on top
   of `validate.py` doing the same hardening at the source. Likewise the
   request envelope itself is untrusted shape, not just its rows: a
   string where a list of observations should be raises `AttributeError`
   from the first `.get()` if not checked explicitly first, so that
   shape is rejected as a 400 before the loop ever starts.
"""
import hmac
import logging
import os

import psycopg
from fastapi import APIRouter, Header, HTTPException

from api import validate
from api.db import rows

router = APIRouter()
logger = logging.getLogger("api.ingest")

# On conflict, only `last_seen` moves, in step with `db/seed.py`'s own
# convention -- except for `name`. A discovery row with no name gets a
# placeholder (`_placeholder_name`), and `product.name` must never move
# from a real name back to a placeholder; but a placeholder *must* be
# healed the day a row carrying a real name shows up for that item_id,
# or every "(discovered) <id>" name would be permanent -- there is no
# other write path to `product.name` once one exists.
INSERT_PRODUCT = (
    "insert into product (item_id, name) values (%s, %s) "
    "on conflict (item_id) do update set "
    "last_seen = current_date, "
    "name = case when product.name like '(discovered) %%' "
    "            then excluded.name else product.name end"
)
INSERT_OBSERVATION = (
    "insert into observation (item_id, store_id, observed_at, list_price, "
    "clearance_price, pct_off, quantity, store_only, source, trusted) "
    "values (%s, %s, clock_timestamp(), %s, %s, %s, %s, %s, 'discovery', true)"
)
BEARER_PREFIX = "Bearer "

# Errors `validate.check` must never let past it, but that `discovery()`
# treats identically to a `ValueError` anyway: a bug there costs one row,
# the same as bad data does, never the whole chunk.
_BAD_ROW_ERRORS = (ValueError, TypeError, ArithmeticError, AttributeError)


def _placeholder_name(item_id):
    # `product.name` is `not null`. Discovery uploads a price, not a
    # catalogue entry -- if the collector didn't send a name, this marks
    # "we know this item exists, we don't yet know its name" rather than
    # a real product title, so `INSERT_PRODUCT` can find and heal it
    # later without ever mistaking a real name for a placeholder.
    return f"(discovered) {item_id}"


def _authorise(header):
    token = os.environ.get("PENNYRUN_INGEST_TOKEN")
    if not token:
        # Server misconfiguration, not a client failure: silently
        # accepting every request because nobody set the token would be
        # far worse than refusing all of them until it's fixed.
        raise HTTPException(500, "PENNYRUN_INGEST_TOKEN is not set on the server")

    supplied = header[len(BEARER_PREFIX):] if header and header.startswith(BEARER_PREFIX) else ""
    # Constant-time comparison -- `==` would leak the token's length and
    # prefix through response timing. Compared as bytes, not `str`:
    # Starlette decodes headers as latin-1, so a header byte above 0x7F
    # produces a non-ASCII `str`, and `hmac.compare_digest` raises
    # `TypeError` on non-ASCII `str` operands -- an unauthenticated
    # request must 401, never 500.
    if not hmac.compare_digest(supplied.encode("utf-8", "ignore"), token.encode()):
        raise HTTPException(401, "bad or missing ingest token")


@router.post("/api/v1/discovery")
def discovery(payload: dict, authorization: str = Header(None)):
    _authorise(authorization)

    observations = payload.get("observations", [])
    if not isinstance(observations, list):
        raise HTTPException(400, "observations must be a list")

    candidates, rejected = [], 0
    for obs in observations:
        if not isinstance(obs, dict):
            rejected += 1
            continue
        try:
            validate.check(obs)
        except _BAD_ROW_ERRORS:
            rejected += 1
            continue
        candidates.append(obs)

    accepted = 0
    with rows() as cur:
        conn = cur.connection
        with conn.transaction():  # the batch's one real transaction/commit
            for o in candidates:
                try:
                    with conn.transaction():  # always a SAVEPOINT: nested in the above
                        cur.execute(
                            INSERT_PRODUCT,
                            (o["item_id"], o.get("name") or _placeholder_name(o["item_id"])))
                        cur.execute(
                            INSERT_OBSERVATION,
                            (o["item_id"], o["store_id"], o.get("list_price"),
                             o.get("clearance_price"), o.get("pct_off"),
                             o.get("quantity"), o.get("store_only")))
                except psycopg.Error as exc:
                    rejected += 1
                    # %r, not %s: item_id/store_id already passed
                    # validate.check's charset check by the time we get
                    # here, but logging is not the place to rely on that
                    # holding forever -- %r escapes a newline instead of
                    # letting it forge a second log line.
                    logger.warning(
                        "discovery: row rejected at the database "
                        "(item_id=%r store_id=%r): %s",
                        o.get("item_id"), o.get("store_id"), exc.__class__.__name__)
                else:
                    accepted += 1

    return {"accepted": accepted, "rejected": rejected}
