import urllib.error

import pytest

from tools import upload

ROW = ["Roofing", "204767783", "Building", 1.2, 49.98, 98, "2502", 3, 0,
       "/p/x/204767783", "791308000105", "418625", "4305036", 0, None]


def test_row_becomes_an_observation():
    obs = upload.to_observation(ROW)
    assert obs["item_id"] == "204767783"
    assert obs["store_id"] == "2502"
    assert obs["clearance_price"] == 1.2
    assert obs["list_price"] == 49.98
    assert obs["pct_off"] == 98
    assert obs["quantity"] == 3


def test_row_carries_the_product_name():
    """The other half of the discovery heal path (api/ingest.py's
    `_placeholder_name`): without a name in the observation, a discovered
    item's placeholder name can never be healed and displays as
    "(discovered) <id>" forever."""
    obs = upload.to_observation(ROW)
    assert obs["name"] == "Roofing"


def test_batches_are_capped(monkeypatch):
    sent = []
    monkeypatch.setattr(upload, "_post", lambda url, body, token: sent.append(body) or {"accepted": len(body["observations"]), "rejected": 0})
    upload.send([ROW] * 2500, "http://x", "t")
    assert len(sent) == 3, "2500 rows should go in batches of 1000"


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
