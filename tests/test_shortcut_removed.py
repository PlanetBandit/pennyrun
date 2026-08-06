"""The iOS Shortcut path is gone. This test keeps it gone."""
import pathlib
import re

APP = pathlib.Path(__file__).parent.parent / "pennyrun" / "index.html"

# Identifiers and copy that existed only to serve the Shortcut.
FORBIDDEN = [
    "rx-pat", "rx-url", "copyrow",
    "parseHDShare", "handleHDShare", "parseSweep", "parseAppShot",
    "applyAppShot", "readShotBatch", "readShotFile",
    "shortcutName", "linkin", "linkgo", "shotbtn",
    "Send to Penny Run", "Show in Share Sheet",
    "planetbandit.github.io",
]


def test_no_shortcut_identifiers_remain():
    src = APP.read_text(encoding="utf-8")
    found = [token for token in FORBIDDEN if token in src]
    assert found == [], f"Shortcut leftovers still in index.html: {found}"


def test_no_share_query_intake():
    src = APP.read_text(encoding="utf-8")
    assert 'q.get("text")' not in src
    assert 'q.get("url")' not in src


def test_app_still_has_its_core():
    """Deleting the Shortcut must not take the real app with it."""
    src = APP.read_text(encoding="utf-8")
    for keep in ["startScan", "onCode", "computeVerdict", "loadCatalog",
                 "renderSweep", "snapTag", "hdClearance"]:
        assert keep in src, f"deleted too much — {keep} is missing"


def test_html_tags_balance():
    """Crude but effective: section/details counts must match after surgery."""
    src = APP.read_text(encoding="utf-8")
    for tag in ["section", "details", "ol", "script"]:
        opens = len(re.findall(rf"<{tag}[\s>]", src))
        closes = len(re.findall(rf"</{tag}>", src))
        assert opens == closes, f"<{tag}> unbalanced: {opens} open, {closes} close"
