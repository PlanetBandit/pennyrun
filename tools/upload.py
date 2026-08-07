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
committed it gets sent again, and `api/ingest.py`'s docstring already
covers why that is the right tradeoff (a duplicate observation row costs
nothing a dedup query can't handle later; a silently dropped night of
collection is the failure worth avoiding). Retrying only widens the window
in which that duplicate can happen -- it does not introduce it, since a
single unretried request could already time out after committing.
"""
import json
import time
import urllib.error
import urllib.request

CHUNK = 1000
MAX_ATTEMPTS = 3
BACKOFF = 2  # seconds; doubles each retry
TIMEOUT = 30  # seconds per attempt


def to_observation(row):
    return {"item_id": row[1], "store_id": row[6], "name": row[0],
            "clearance_price": row[3], "list_price": row[4],
            "pct_off": row[5], "quantity": row[7],
            "store_only": bool(row[8])}


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
        got = _post(url, body, token)
        total["accepted"] += got.get("accepted", 0)
        total["rejected"] += got.get("rejected", 0)
    return total
