"""Ship a sweep's rows to the droplet. Runs on the home box.

Talks to *your own* droplet, not Home Depot, so `urllib` is deliberate --
no impersonation is needed and the constraint that only `tools/hdclient.py`
may speak to Home Depot does not apply here.

`_post` retries a bounded number of times on a transport failure (timeout,
connection refused, DNS, a 5xx from Caddy/uvicorn) but never retries a 4xx:
`api/ingest.py` returning 400/401 means the request itself was rejected
(bad token, bad shape), and hammering it again wastes the retry budget on
something that will never succeed. This makes `send()` at-least-once, not
exactly-once -- a batch that timed out *after* the droplet already
committed it gets sent again on retry. That is a deliberate tradeoff, not
a free one: a duplicate `observation` row is invisible to every read
endpoint today (`api/main.py`'s `LATEST` is `distinct on (item_id,
store_id) order by observed_at desc`, so a duplicate just loses a tiebreak
against itself), but Plan 3's penny-prediction work reads `observation` as
raw history, where a duplicated row inflates a frequency signal without
representing a second real observation. Retrying only widens the window in
which that duplicate can happen -- it does not introduce it, since a
single unretried request could already time out after committing -- but
the bill is real and deferred, not absent. `TIMEOUT` is set to match
`api/ingest.py`'s original single-attempt budget (120s) rather than
something shorter: a shorter per-attempt timeout would trigger this retry
-- and mint a duplicate -- more often, on nothing worse than a batch that
was simply still being committed when the client gave up.

`send()` does not swallow a chunk that exhausts its retries into a bare
exception, either: chunks before it already committed, permanently (no
`on conflict do nothing`, no delete path -- see `api/ingest.py`), and a
caller that doesn't know how much of the batch got through would either
under-report (silently drop the rest of a night's rows) or, if it just
resends everything on the next run, duplicate every already-committed
chunk on top of the one duplicate the timeout may already have caused.
`UploadError.partial` carries the accepted/rejected counts from every
chunk that succeeded before the failing one, so whoever is watching (or
re-running) knows where it stopped.
"""
import json
import time
import urllib.error
import urllib.request

CHUNK = 1000
MAX_ATTEMPTS = 3
BACKOFF = 2  # seconds; doubles each retry
TIMEOUT = 120  # seconds per attempt -- see module docstring


class UploadError(Exception):
    """Raised by `send()` when a chunk exhausts `_post`'s retries.

    `partial` is the `{"accepted", "rejected"}` total from whatever
    chunks committed before this one failed -- those rows are permanent,
    so a caller catching this needs that number, not just the fact that
    something went wrong. `cause` is the underlying exception from `_post`
    (a `urllib.error.HTTPError`/`URLError`, or whatever transport failure
    it last saw).
    """

    def __init__(self, partial, cause):
        super().__init__(
            f"stopped after {partial['accepted']} accepted, "
            f"{partial['rejected']} rejected: {cause}")
        self.partial = partial
        self.cause = cause


def to_observation(row):
    """Every field `sweep.row()` produces, not just the eight that used to
    make it onto the wire. Two things worth knowing:

    - Money (`clearance_price`, `list_price`) is sent as a string, not a
      `float` -- `row[3]`/`row[4]` are already `round(x, 2)` in
      `sweep.row()`, and formatting with `:.2f` here is the last place a
      `float` can still leak into the request body. `api/validate.py`
      parses the string into an exact `Decimal`, and `api/ingest.py`
      inserts that `Decimal`, so money never touches `float` on this path
      end to end.
    - `category`/`canonical_url`/`upc`/`store_sku`/`model_number` come
      straight off the row; an empty string (Home Depot didn't return
      that field, or `sweep.row()` had nothing to put there) is sent as
      `None`, not `""` -- `api/ingest.py`'s upsert uses
      `coalesce(excluded.x, product.x)` for these, which only skips a
      real `NULL`. Sending `""` would silently blank out a value already
      on file. `replacement_id` is Home Depot's boolean "this item has a
      successor SKU" flag (`sweep.row()`'s `superseded`), not an actual
      SKU -- `product.replacement_id` records the flag as `"1"`, and
      `None` (not `"0"`) when there isn't one, for the same
      coalesce-safety reason.
    """
    return {"item_id": row[1], "store_id": row[6], "name": row[0] or None,
            "category": row[2] or None,
            "clearance_price": f"{row[3]:.2f}", "list_price": f"{row[4]:.2f}",
            "pct_off": row[5], "quantity": row[7],
            "store_only": bool(row[8]),
            "canonical_url": row[9] or None, "upc": row[10] or None,
            "store_sku": row[11] or None, "model_number": row[12] or None,
            "replacement_id": "1" if row[13] else None}


def _post(url, body, token):
    data = json.dumps(body).encode()
    headers = {"Content-Type": "application/json",
               "Authorization": f"Bearer {token}"}
    last_exc = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=data, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if 400 <= e.code < 500:
                raise  # a rejected batch is rejected -- do not retry
            last_exc = e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            last_exc = e
        if attempt < MAX_ATTEMPTS:
            time.sleep(BACKOFF * attempt)
    raise last_exc


def send(rows, base_url, token):
    url = base_url.rstrip("/") + "/api/v1/discovery"
    total = {"accepted": 0, "rejected": 0}
    for i in range(0, len(rows), CHUNK):
        body = {"observations": [to_observation(r) for r in rows[i:i + CHUNK]]}
        try:
            got = _post(url, body, token)
        except Exception as e:
            # Everything before this chunk already committed -- surface
            # what got through rather than losing it in a bare raise.
            raise UploadError(dict(total), e) from e
        total["accepted"] += got.get("accepted", 0)
        total["rejected"] += got.get("rejected", 0)
    return total
