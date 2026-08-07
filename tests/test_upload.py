import urllib.error

import pytest

from tools import upload

ROW = ["Roofing", "204767783", "Building", 1.2, 49.98, 98, "2502", 3, 0,
       "/p/x/204767783", "791308000105", "418625", "4305036", 0, None]


def test_row_becomes_an_observation():
    obs = upload.to_observation(ROW)
    assert obs["item_id"] == "204767783"
    assert obs["store_id"] == "2502"
    assert obs["pct_off"] == 98
    assert obs["quantity"] == 3


def test_money_is_sent_as_a_string_not_a_float():
    """Important 1 of the branch review: `sweep.py` builds prices with
    `float()`/`round()`, and putting those floats straight into the JSON
    body is the one place `float` could still reach the wire even though
    `api/validate.py` parses everything into an exact `Decimal`. Sending
    a string here is what makes "money never touches float" true end to
    end, not just inside `validate.py`."""
    obs = upload.to_observation(ROW)
    assert obs["clearance_price"] == "1.20"
    assert obs["list_price"] == "49.98"
    assert isinstance(obs["clearance_price"], str)
    assert isinstance(obs["list_price"], str)


def test_row_carries_the_product_name():
    """The other half of the discovery heal path (api/ingest.py's
    `_placeholder_name`): without a name in the observation, a discovered
    item's placeholder name can never be healed and displays as
    "(discovered) <id>" forever."""
    obs = upload.to_observation(ROW)
    assert obs["name"] == "Roofing"


def test_row_carries_the_catalogue_fields_the_seed_alone_would_never_grow():
    """Critical 3 of the branch review: every catalogue field but the name
    used to stop at `sweep.row()` -- `to_observation` dropped them before
    they ever left the home box, so `category`/`upc`/`store_sku`/
    `model_number`/`canonical_url`/`replacement_id` were permanently NULL
    for anything discovered after the one-time 811-product seed. That
    silently broke `GET /api/v1/lookup?upc=`, the app's barcode-scan entry
    point, for every item found after day one."""
    obs = upload.to_observation(ROW)
    assert obs["category"] == "Building"
    assert obs["canonical_url"] == "/p/x/204767783"
    assert obs["upc"] == "791308000105"
    assert obs["store_sku"] == "418625"
    assert obs["model_number"] == "4305036"
    assert obs["replacement_id"] is None  # ROW's flag (index 13) is 0


def test_replacement_id_flag_becomes_the_string_one_not_zero():
    """The flag is stored as `"1"`/`None`, never `"0"` -- `api/ingest.py`'s
    `coalesce(excluded.replacement_id, product.replacement_id)` upsert
    only skips a real `NULL`, so a `"0"` would be indistinguishable from
    "actually superseded" the moment anything treats the column as
    truthy, and would itself defeat coalesce by being a non-NULL value."""
    row = list(ROW)
    row[13] = 1
    obs = upload.to_observation(row)
    assert obs["replacement_id"] == "1"


def test_empty_catalogue_fields_become_none_not_empty_string():
    """An empty string must never reach the wire for an optional
    catalogue field -- `coalesce(excluded.x, product.x)` only skips a
    real `NULL`, so a blank string sent here would silently overwrite a
    value already stored for this item_id from an earlier, more complete
    row."""
    row = list(ROW)
    row[2] = row[9] = row[10] = row[11] = row[12] = ""
    obs = upload.to_observation(row)
    assert obs["category"] is None
    assert obs["canonical_url"] is None
    assert obs["upc"] is None
    assert obs["store_sku"] is None
    assert obs["model_number"] is None


def test_batches_are_capped(monkeypatch):
    sent = []
    monkeypatch.setattr(upload, "_post", lambda url, body, token: sent.append(body) or {"accepted": len(body["observations"]), "rejected": 0})
    upload.send([ROW] * 2500, "http://x", "t")
    assert len(sent) == 3, "2500 rows should go in batches of 1000"


def test_send_reports_partial_progress_on_a_mid_batch_failure(monkeypatch):
    """Chunks before a failing one already committed -- permanently, since
    observation is append-only. Losing that count in a bare exception
    would leave a human re-running with no idea two of three chunks
    already landed, and no way to avoid resending (and duplicating) them
    other than by guesswork."""
    calls = {"n": 0}

    def flaky_post(url, body, token):
        calls["n"] += 1
        if calls["n"] == 3:
            raise urllib.error.URLError("connection refused")
        return {"accepted": len(body["observations"]), "rejected": 0}

    monkeypatch.setattr(upload, "_post", flaky_post)
    with pytest.raises(upload.UploadError) as exc_info:
        upload.send([ROW] * 2500, "http://x", "t")  # 3 chunks: 1000, 1000, 500

    err = exc_info.value
    assert err.partial == {"accepted": 2000, "rejected": 0}, \
        "the first two chunks' 2000 accepted rows must not be thrown away"
    assert calls["n"] == 3
    assert isinstance(err.cause, urllib.error.URLError)


def test_post_retries_on_transport_failure_then_succeeds(monkeypatch):
    """A night's collection should not be lost because the droplet hiccuped
    -- a transport-level failure (timeout, connection refused, DNS) gets a
    bounded number of retries before giving up."""
    monkeypatch.setattr(upload.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def flaky_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("connection refused")
        return _FakeResponse({"accepted": 1, "rejected": 0})

    monkeypatch.setattr(upload.urllib.request, "urlopen", flaky_urlopen)
    got = upload._post("http://x/api/v1/discovery", {"observations": []}, "t")
    assert got == {"accepted": 1, "rejected": 0}
    assert calls["n"] == 3


def test_post_gives_up_after_bounded_retries(monkeypatch):
    monkeypatch.setattr(upload.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def always_fails(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(upload.urllib.request, "urlopen", always_fails)
    with pytest.raises(urllib.error.URLError):
        upload._post("http://x/api/v1/discovery", {"observations": []}, "t")
    assert calls["n"] == upload.MAX_ATTEMPTS


def test_post_does_not_retry_a_4xx(monkeypatch):
    """A rejected batch is rejected -- retrying a 400/401 just burns time
    hammering an endpoint that will never accept this payload."""
    monkeypatch.setattr(upload.time, "sleep", lambda s: None)
    calls = {"n": 0}

    def rejected(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError("http://x", 400, "Bad Request", {}, None)

    monkeypatch.setattr(upload.urllib.request, "urlopen", rejected)
    with pytest.raises(urllib.error.HTTPError):
        upload._post("http://x/api/v1/discovery", {"observations": []}, "t")
    assert calls["n"] == 1, "a 4xx must not be retried"


class _FakeResponse:
    def __init__(self, body):
        import json
        self._data = json.dumps(body).encode()

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
