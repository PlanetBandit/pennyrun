from unittest.mock import patch
from tools import checkhost


def test_all_ok_is_exit_zero():
    code, msg = checkhost.verdict({"pricing API": "ok", "sitemap": "ok", "search page": "ok"})
    assert code == 0
    assert "GOOD" in msg


def test_search_403_alone_is_still_good():
    """discover() walks the sitemap; the search page is not required."""
    code, msg = checkhost.verdict(
        {"pricing API": "ok", "sitemap": "ok", "search page": "refused: HTTP 403"})
    assert code == 0


def test_pricing_refused_is_blocked():
    code, msg = checkhost.verdict(
        {"pricing API": "refused: HTTP 206", "sitemap": "ok", "search page": "refused: HTTP 403"})
    assert code == 1
    assert "BLOCKED" in msg
    assert "datacentre" in msg or "datacenter" in msg


def test_unreachable_is_not_blocked():
    """This is the bug. A local TLS failure must never read as a block."""
    code, msg = checkhost.verdict(
        {"pricing API": "unreachable: OSError: SSL: CERTIFICATE_VERIFY_FAILED",
         "sitemap": "unreachable: OSError: SSL", "search page": "unreachable: OSError: SSL"})
    assert code == 2
    assert "BLOCKED" not in msg
    assert "could not reach" in msg.lower()


def test_missing_pricing_key_is_unknown():
    """If 'pricing API' key is missing, we don't know what happened."""
    code, msg = checkhost.verdict({"sitemap": "ok", "search page": "ok"})
    assert code == 2
    assert "UNKNOWN" in msg
    assert "BLOCKED" not in msg


def test_unrecognized_pricing_value_is_unknown():
    """An unrecognized value from hdclient means we don't understand the state."""
    code, msg = checkhost.verdict(
        {"pricing API": "weird new state", "sitemap": "ok", "search page": "ok"})
    assert code == 2
    assert "UNKNOWN" in msg
    assert "BLOCKED" not in msg


def test_refused_still_blocked():
    """Regression guard: 'refused' prefix must still produce BLOCKED (exit 1)."""
    code, msg = checkhost.verdict(
        {"pricing API": "refused: HTTP 206", "sitemap": "ok", "search page": "ok"})
    assert code == 1
    assert "BLOCKED" in msg


def test_main_with_empty_results():
    """main() should not crash if probe() returns empty dict."""
    with patch("tools.checkhost.hdclient.probe") as mock_probe:
        mock_probe.return_value = {}
        code = checkhost.main()
        assert code == 2  # Empty dict should result in UNKNOWN
