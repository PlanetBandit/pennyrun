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


class FakeGetResponse:
    """A response as returned by hdclient._get() -- status, text, and headers."""
    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.text = text
        self.headers = headers or {}


def test_sitemap_200_challenge_page_raises_refused_not_returned_as_content(monkeypatch):
    """Akamai answers a shard fetch with HTTP 200 and a JS challenge page.
    Parsing that as if it were the sitemap makes a blocked slice look like
    an empty one -- the exact silent-empty-result failure this rebuild
    exists to eliminate."""
    challenge = ('<!DOCTYPE html><html lang="en"><body>'
                 '<script src="/1gD1TBGg/HSk/QFn?t=1"></script>'
                 '<div id="sec-if-cpt-container">...</div></body></html>')
    monkeypatch.setattr(hdclient, "_get", lambda *a, **k:
                         FakeGetResponse(200, challenge, {"Content-Type": "text/html"}))
    with pytest.raises(hdclient.Refused, match="not XML|non-XML|challenge"):
        hdclient.sitemap("https://www.homedepot.com/sitemap/P/PIP-0.xml")


def test_sitemap_real_xml_returned_unchanged(monkeypatch):
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://www.homedepot.com/p/x/204767783</loc></url>'
           '</urlset>')
    monkeypatch.setattr(hdclient, "_get", lambda *a, **k:
                         FakeGetResponse(200, xml, {"Content-Type": "text/xml"}))
    got = hdclient.sitemap("https://www.homedepot.com/sitemap/P/PIPs.xml")
    assert got == xml


def test_sitemap_index_markup_also_accepted(monkeypatch):
    """The root sitemap is a <sitemapindex>, not a <urlset> -- both are real."""
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<sitemap><loc>https://www.homedepot.com/sitemap/P/PIP-0.xml</loc></sitemap>'
           '</sitemapindex>')
    monkeypatch.setattr(hdclient, "_get", lambda *a, **k:
                         FakeGetResponse(200, xml, {"Content-Type": "text/xml"}))
    got = hdclient.sitemap("https://www.homedepot.com/sitemap/P/PIPs.xml")
    assert got == xml


def test_sitemap_valid_xml_with_mislabeled_html_content_type_is_accepted(monkeypatch):
    """CDN/Akamai edges routinely serve a real .xml object with
    Content-Type: text/html. The body sniff is what matters -- gating on
    Content-Type too would refuse a genuine sitemap over a header, not its
    content, and that's a false refusal discover() would now die on."""
    xml = ('<?xml version="1.0" encoding="UTF-8"?>'
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
           '<url><loc>https://www.homedepot.com/p/x/204767783</loc></url>'
           '</urlset>')
    monkeypatch.setattr(hdclient, "_get", lambda *a, **k:
                         FakeGetResponse(200, xml, {"Content-Type": "text/html"}))
    got = hdclient.sitemap("https://www.homedepot.com/sitemap/P/PIPs.xml")
    assert got == xml


def test_sitemap_expect_xml_false_skips_the_content_check(monkeypatch):
    """sitemap() also fetches plain HTML (search result pages via harvest() and
    checkhost's probe). Those callers opt out of the XML check explicitly --
    it isn't a sitemap and was never supposed to look like one."""
    html = "<html><body><a href='/p/some-product-name/205606416'>x</a></body></html>"
    monkeypatch.setattr(hdclient, "_get", lambda *a, **k:
                         FakeGetResponse(200, html, {"Content-Type": "text/html"}))
    got = hdclient.sitemap("https://www.homedepot.com/s/mulch%20clearance", expect_xml=False)
    assert got == html


def test_the_product_query_requests_the_fields_that_identify_shelf_stock():
    """`services.type` and `locations.type` are the only things separating a
    shelf count from the ship-to-store pool -- both arrive with this store's
    id and isAnchor true. Dropping either from the query silently turns
    warehouse figures back into shelf counts, and nothing else would fail."""
    from tools import hdclient

    q = hdclient._FULL
    assert "services { type locations" in q, "services.type not requested"
    assert "locationId isAnchor type" in q, "locations.type not requested"
