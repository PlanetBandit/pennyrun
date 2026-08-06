import json
import pathlib
import pytest
from tools import hdclient

FIX = pathlib.Path(__file__).parent / "fixtures"


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload
        self.text = json.dumps(payload) if isinstance(payload, dict) else str(payload)

    def json(self):
        return self._payload


class FakeNonJSONResponse:
    """A 200 whose body is HTML (an interstitial/challenge page), not JSON."""
    def __init__(self, html):
        self.status_code = 200
        self.text = html

    def json(self):
        raise ValueError("Expecting value: line 1 column 1 (char 0)")


def test_uses_the_measured_profile():
    assert hdclient.PROFILE == "safari17_0"


def test_returns_products_on_200(monkeypatch):
    payload = json.loads((FIX / "products_ok.json").read_text())
    monkeypatch.setattr(hdclient, "_post", lambda *a, **k: FakeResponse(200, payload))
    got = hdclient.products(["204767783"], "2502")
    assert len(got) == 1
    assert got[0]["itemId"] == "204767783"
    assert got[0]["pricing"]["clearance"]["value"] == 1.2


def test_206_raises_refused_not_empty_list(monkeypatch):
    """The old code returned [] here, so a wall looked like 'nothing on sale'."""
    body = {"data": {"Generic Errors API": None},
            "error": [{"message": "Generic Errors  API errors"}]}
    monkeypatch.setattr(hdclient, "_post", lambda *a, **k: FakeResponse(206, body))
    with pytest.raises(hdclient.Refused):
        hdclient.products(["204767783"], "2502")


def test_403_raises_refused(monkeypatch):
    monkeypatch.setattr(hdclient, "_post", lambda *a, **k: FakeResponse(403, {}))
    with pytest.raises(hdclient.Refused):
        hdclient.products(["1"], "2502")


def test_transport_error_is_unreachable_not_refused(monkeypatch):
    """An SSL failure is not a block. checkhost.py got this wrong."""
    def boom(*a, **k):
        raise OSError("SSL: CERTIFICATE_VERIFY_FAILED")
    monkeypatch.setattr(hdclient, "_post", boom)
    with pytest.raises(hdclient.Unreachable):
        hdclient.products(["1"], "2502")


def test_batch_cap_is_enforced():
    with pytest.raises(ValueError, match="16"):
        hdclient.products([str(i) for i in range(17)], "2502")


def test_duplicate_ids_rejected():
    """The gateway errors on duplicates; catch it before spending a request."""
    with pytest.raises(ValueError, match="duplicate"):
        hdclient.products(["1", "1"], "2502")


def test_graphql_errors_raise(monkeypatch):
    body = {"errors": [{"message": "ItemIds cannot have duplicates"}], "data": {"products": None}}
    monkeypatch.setattr(hdclient, "_post", lambda *a, **k: FakeResponse(200, body))
    with pytest.raises(hdclient.Refused, match="duplicates"):
        hdclient.products(["1"], "2502")


def test_200_with_non_json_body_raises_refused(monkeypatch):
    """An anti-bot interstitial can be served with HTTP 200 and an HTML body."""
    monkeypatch.setattr(hdclient, "_post",
                         lambda *a, **k: FakeNonJSONResponse("<html>are you a robot?</html>"))
    with pytest.raises(hdclient.Refused, match="non-JSON"):
        hdclient.products(["1"], "2502")


def test_bare_string_graphql_error_raises_refused_not_attributeerror(monkeypatch):
    body = {"errors": ["a bare string"], "data": {"products": None}}
    monkeypatch.setattr(hdclient, "_post", lambda *a, **k: FakeResponse(200, body))
    with pytest.raises(hdclient.Refused):
        hdclient.products(["1"], "2502")


def test_empty_errors_array_is_not_an_error(monkeypatch):
    payload = json.loads((FIX / "products_ok.json").read_text())
    payload["errors"] = []
    monkeypatch.setattr(hdclient, "_post", lambda *a, **k: FakeResponse(200, payload))
    got = hdclient.products(["204767783"], "2502")
    assert len(got) == 1
    assert got[0]["itemId"] == "204767783"
