"""The write path: what the home collector uploads after a sweep.

Two things the brief this was built from got wrong, both worth writing
down so nobody "fixes" them back in.

1. `observation.item_id` has a foreign key to `product`. Discovering an
   item `product` has never seen is the collector's entire job, not an
   error condition -- so each row upserts its product first instead of
   assuming Task 6's one-time seed already covers it. Each row's product
   upsert and observation insert share one savepoint
   (`conn.transaction()`, nested because `api.db.rows()` already holds an
   open transaction): if the observation insert fails for a reason that
   *is* a real error -- an unknown `store_id`, which is a fixed, curated
   list and not something the collector "discovers" the way it discovers
   products -- only that row's work rolls back. The rest of the batch,
   already-executed statements included, is untouched. One malformed row
   costs one row, never the batch.

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

INSERT_PRODUCT = (
    "insert into product (item_id, name) values (%s, %s) "
    "on conflict (item_id) do update set last_seen = current_date"
)
INSERT_OBSERVATION = (
    "insert into observation (item_id, store_id, observed_at, list_price, "
    "clearance_price, pct_off, quantity, store_only, source, trusted) "
    "values (%s, %s, clock_timestamp(), %s, %s, %s, %s, %s, 'discovery', true)"
)
BEARER_PREFIX = "Bearer "


def _placeholder_name(item_id):
    # `product.name` is `not null`. Discovery uploads a price, not a
    # catalogue entry -- if the collector didn't send a name, this is a
    # marker for "we know this item exists, we don't yet know its name"
    # rather than a real product title. `on conflict do update set
    # last_seen` never overwrites it, so a later, better-informed insert
    # (or a future catalogue re-seed) still has room to fix it.
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
    # prefix through response timing.
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(401, "bad or missing ingest token")


@router.post("/api/v1/discovery")
def discovery(payload: dict, authorization: str = Header(None)):
    _authorise(authorization)

    candidates, rejected = [], 0
    for obs in payload.get("observations", []):
        try:
            validate.check(obs)
        except ValueError:
            rejected += 1
            continue
        candidates.append(obs)

    accepted = 0
    with rows() as cur:
        conn = cur.connection
        for o in candidates:
            try:
                with conn.transaction():
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
                logger.warning(
                    "discovery: row rejected at the database "
                    "(item_id=%s store_id=%s): %s",
                    o.get("item_id"), o.get("store_id"), exc.__class__.__name__)
            else:
                accepted += 1

    return {"accepted": accepted, "rejected": rejected}
