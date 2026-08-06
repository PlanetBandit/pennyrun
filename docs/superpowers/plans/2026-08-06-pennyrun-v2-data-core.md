# Penny Run v2 — Data Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore Home Depot collection from a residential box, and stand up the droplet as the system of record — Postgres price history behind a FastAPI read/write API served over HTTPS.

**Architecture:** Three tiers split by who may talk to Home Depot. A home box runs discovery through a single impersonating client module and uploads results; the droplet stores an append-only observation log in Postgres and serves a JSON API through Caddy; phones (Plan 2) verify per-store prices. The droplet never contacts Home Depot.

**Tech Stack:** Python 3.12, `curl_cffi` (TLS impersonation), pytest, PostgreSQL 16, FastAPI + uvicorn, Caddy 2, systemd.

## Scope note

The approved spec covers subsystems **A (data core)** and **C (new app)**. This plan implements **A plus the Shortcut removal from the current app**. The full app rewrite is **Plan 2**, written after this plan lands — it depends on the read API existing and being stable. Each plan produces working software on its own: at the end of this plan the droplet serves real clearance data over HTTPS, with the existing app still functioning.

## Global Constraints

- Python **3.12**; no dependency may be added outside `tools/requirements.txt` and `api/requirements.txt`.
- **Only `tools/hdclient.py` may import `curl_cffi` or contain a Home Depot URL.** Every other module goes through it. This is the whole point — when Home Depot changes, one file changes.
- Impersonation profile is **`safari17_0`** (measured working 4/4 on 2026-08-05; `chrome`, `chrome131`, `chrome124`, `safari180` all returned 206).
- `BATCH = 16` — the `products(itemIds:)` cap. Raising it silently drops results.
- `WORKERS = 20`, ~330 products/sec. Do not raise; rate discipline is now a design constraint.
- Never run collection from the droplet. It is refused (0/4, six profiles).
- The `observation` table is **append-only**. No `UPDATE`, no `DELETE` in application code.
- Database credential is read from `/root/.pennyrun-db.env`. **Never** hardcode or log it.
- Money is `numeric(10,2)` in Postgres and `Decimal` in Python. Never `float`.
- Commit after every task. Conventional commit prefixes (`feat:`, `fix:`, `test:`, `chore:`).

---

### Task 1: Remove the iOS Shortcut path from the app

The Shortcut existed only because there was no backend. It is ~80 lines of instructions plus the share-intake plumbing, and it is the ugliest surface in the app. Removing it now is independent of everything else.

**Files:**
- Modify: `pennyrun/index.html` (regions listed in Step 3)
- Modify: `pennyrun/sw.js:5` (cache bust)
- Test: `tests/test_shortcut_removed.py`

**Interfaces:**
- Consumes: nothing
- Produces: nothing — this is a deletion. No other task depends on it.

- [ ] **Step 1: Create the test harness and the failing guard test**

```bash
mkdir -p tests
python3 -m venv .venv
./.venv/bin/pip install pytest
printf 'pytest\ncurl_cffi\n' > tools/requirements.txt
```

Create `tests/test_shortcut_removed.py`:

```python
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
```

- [ ] **Step 2: Run it and watch it fail**

Run: `./.venv/bin/pytest tests/test_shortcut_removed.py -v`
Expected: `test_no_shortcut_identifiers_remain` FAILS listing every token; `test_no_share_query_intake` FAILS; the other two PASS.

- [ ] **Step 3: Delete the Shortcut surface**

Remove these regions from `pennyrun/index.html`. Line numbers are from the current `main` — re-locate by the quoted anchor text, do not trust the numbers after the first edit.

| region | anchor to find | what to do |
|---|---|---|
| `.copyrow` CSS (~357) | `.copyrow{display:flex` | delete the rule |
| link-paste row (~479–484) | `<input class="field__i" id="linkin"` | delete the input, the `linkgo` button, the `shotbtn` button, and the `<p class="scan__hint">Paste a share link…` |
| the whole guide (~486–510) | `<details class="guide">` through its `</details>` | delete the entire `<details>` block |
| state field (~733, 745, 758) | `shortcutName` | remove the key from the `state` literal, from `load()`, and from `save()` |
| share parsers (~1390–1600) | `function parseHDShare`, `function parseAppShot`, `function parseSweep` | delete all three functions and their doc comments |
| screenshot readers (~1512–1590) | `function readShotBatch`, `function readOneShot`, `function readShotFile` | delete all three |
| `applyAppShot` (~1492) | `function applyAppShot` | delete |
| share handler (~1794–1866) | `function handleHDShare` | delete |
| listeners (~2528–2566) | `$("shotbtn").addEventListener` | delete the `shotbtn`, `shotfile`, `linkgo`, and `linkin` listeners |
| share intake (~2618–2650) | `/* Share intake: the Android share target` | delete the whole block to the end of that IIFE |
| merge beacon (~1284–1297, 2571) | `armMerge`, `consumeMergeTarget` | delete both functions and the `if(editingId) armMerge(editingId);` line — they exist only to fold a returning share into an item |

Also remove from `pennyrun/manifest.webmanifest` the `share_target` member if present.

- [ ] **Step 4: Bump the service worker cache**

In `pennyrun/sw.js:5`, change:

```javascript
var CACHE = "pennyrun-v55";
```

to:

```javascript
var CACHE = "pennyrun-v56";
```

Without this every installed phone keeps serving the old build from cache.

- [ ] **Step 5: Run the tests until green**

Run: `./.venv/bin/pytest tests/test_shortcut_removed.py -v`
Expected: 4 passed. If `test_html_tags_balance` fails, a closing tag was taken with a deleted block — find it before moving on.

- [ ] **Step 6: Verify the app still loads**

Run: `python3 -m http.server 8080 --directory pennyrun` and open `http://localhost:8080`.
Expected: the app renders, all four tabs switch, no errors in the browser console. The camera will not start over plain HTTP — that is expected and not a regression.

- [ ] **Step 7: Commit**

```bash
git add tests/test_shortcut_removed.py tools/requirements.txt pennyrun/index.html pennyrun/sw.js pennyrun/manifest.webmanifest
git commit -m "feat: drop the iOS Shortcut path

It existed only because there was no backend. The droplet API replaces
every job it did, and it was the ugliest surface in the app."
```

---

### Task 2: The one module that talks to Home Depot

**Files:**
- Create: `tools/hdclient.py`
- Create: `tests/test_hdclient.py`
- Create: `tests/fixtures/products_ok.json`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `PROFILE: str` — `"safari17_0"`
  - `class Refused(Exception)` — Home Depot answered, and said no
  - `class Unreachable(Exception)` — we never got an answer (DNS, TLS, timeout)
  - `products(item_ids: list[str], store_id: str, lite: bool = False) -> list[dict]`
  - `sitemap(url: str) -> str`
  - `probe() -> dict[str, str]` — host name → `"ok"` / `"refused: …"` / `"unreachable: …"`

- [ ] **Step 1: Write the failing tests**

Create `tests/fixtures/products_ok.json`:

```json
{"data": {"products": [{"itemId": "204767783",
  "identifiers": {"productLabel": "Black Mineral Surface Roll Low Slope Roofing",
    "canonicalUrl": "/p/x/204767783", "upc": "791308000105",
    "storeSkuNumber": "418625", "modelNumber": "4305036"},
  "availabilityType": {"type": "Shared"}, "info": {"replacementOMSID": null},
  "pricing": {"value": 49.98, "clearance": {"value": 1.2, "dollarOff": 48.78, "percentageOff": 98.0}}}]}}
```

Create `tests/test_hdclient.py`:

```python
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
```

- [ ] **Step 2: Run and watch it fail**

Run: `./.venv/bin/pytest tests/test_hdclient.py -v`
Expected: all fail with `ModuleNotFoundError: No module named 'tools.hdclient'`.

- [ ] **Step 3: Implement `tools/hdclient.py`**

```python
"""The only module in this repo that talks to Home Depot.

Two independent checks guard their gateway, both measured 2026-08-05:
a browser-grade TLS fingerprint AND a non-datacentre address. Miss either
and every response is 206 "Generic Errors API".

    client                          residential      droplet
    urllib / plain curl             206 refused      206 refused
    curl_cffi safari17_0            200  (4/4)       206 refused (0/4)

There is no sanctioned API. This is homedepot.com's own backend, called
the way their website calls it. It changes without warning, so keep every
assumption about it inside this file.
"""
from curl_cffi import requests

PROFILE = "safari17_0"
API = "https://apionline.homedepot.com/federation-gateway/graphql"
SITEMAP_HOST = "https://www.homedepot.com"
BATCH = 16  # products(itemIds:) silently truncates above this

HEADERS = {"Content-Type": "application/json",
           "x-experience-name": "general-merchandise"}

_FULL = ('query q($ids: [String!]!) { products(itemIds: $ids) { itemId '
         'identifiers { productLabel canonicalUrl upc storeSkuNumber modelNumber } '
         'availabilityType { type } info { replacementOMSID } '
         'pricing(storeId: "%(s)s", isBrandPricingPolicyCompliant: false) '
         '{ value clearance { value dollarOff percentageOff } } '
         'fulfillment(storeId: "%(s)s") { fulfillmentOptions { type fulfillable '
         'services { locations { locationId isAnchor inventory { quantity } } } } } } }')

_LITE = ('query q($ids: [String!]!) { products(itemIds: $ids) { itemId '
         'identifiers { productLabel } taxonomy { breadCrumbs { label } } '
         'pricing(storeId: "%(s)s", isBrandPricingPolicyCompliant: false) '
         '{ clearance { value } } } }')


class Refused(Exception):
    """They answered and said no. A wall, not a wire."""


class Unreachable(Exception):
    """We never got an answer. DNS, TLS, timeout — our side or the network."""


def _post(url, payload, timeout):
    return requests.post(url, json=payload, headers=HEADERS,
                         impersonate=PROFILE, timeout=timeout)


def _get(url, timeout):
    return requests.get(url, impersonate=PROFILE, timeout=timeout)


def products(item_ids, store_id, lite=False, timeout=40):
    if len(item_ids) > BATCH:
        raise ValueError(f"at most {BATCH} itemIds per call, got {len(item_ids)}")
    if len(set(item_ids)) != len(item_ids):
        raise ValueError("duplicate itemIds — the gateway rejects the whole call")

    query = (_LITE if lite else _FULL) % {"s": store_id}
    payload = {"operationName": "q", "variables": {"ids": list(item_ids)}, "query": query}

    try:
        r = _post(API, payload, timeout)
    except Exception as e:                      # transport, not policy
        raise Unreachable(f"{type(e).__name__}: {e}") from e

    if r.status_code != 200:
        raise Refused(f"HTTP {r.status_code}: {r.text[:160]}")

    body = r.json()
    if body.get("errors"):
        raise Refused(body["errors"][0].get("message", "graphql error"))

    data = body.get("data") or {}
    return [p for p in (data.get("products") or []) if p]


def sitemap(url, timeout=60):
    try:
        r = _get(url, timeout)
    except Exception as e:
        raise Unreachable(f"{type(e).__name__}: {e}") from e
    if r.status_code != 200:
        raise Refused(f"HTTP {r.status_code}")
    return r.text


def probe():
    """What does each host do from this machine? No retries, nothing swallowed."""
    out = {}
    try:
        products(["205606416"], "2577", lite=True, timeout=30)
        out["pricing API"] = "ok"
    except Refused as e:
        out["pricing API"] = f"refused: {e}"
    except Unreachable as e:
        out["pricing API"] = f"unreachable: {e}"

    for name, url in [("sitemap", f"{SITEMAP_HOST}/sitemap/P/PIPs.xml"),
                      ("search page", f"{SITEMAP_HOST}/s/mulch%20clearance")]:
        try:
            sitemap(url, timeout=30)
            out[name] = "ok"
        except Refused as e:
            out[name] = f"refused: {e}"
        except Unreachable as e:
            out[name] = f"unreachable: {e}"
    return out
```

Create `tools/__init__.py` (empty) so `from tools import hdclient` resolves.

- [ ] **Step 4: Run tests to green**

Run: `./.venv/bin/pytest tests/test_hdclient.py -v`
Expected: 8 passed.

- [ ] **Step 5: Verify against the real gateway from a residential machine**

Run: `./.venv/bin/python -c "from tools import hdclient; print(hdclient.probe())"`
Expected on a home connection: `{'pricing API': 'ok', 'sitemap': 'ok', 'search page': 'refused: HTTP 403'}`.
The search page 403 is expected and harmless — `discover` uses the sitemap, not search.
Expected on the droplet: `pricing API` refused. That is the measured, correct answer.

- [ ] **Step 6: Commit**

```bash
git add tools/__init__.py tools/hdclient.py tests/test_hdclient.py tests/fixtures/products_ok.json
git commit -m "feat: put every Home Depot call behind one impersonating client

Their gateway needs a browser TLS fingerprint as well as a residential
address. urllib never had one, which is why the sweep died mid-day.
Refused and Unreachable are now different exceptions, because a wall and
a dead wire were indistinguishable and that cost us a night."
```

---

### Task 3: Make `checkhost.py` tell the truth

It currently reports *every* exception as `BLOCKED`. An SSL certificate failure produced a confident "Home Depot will not quote prices to this address range" — the exact swallowed-error failure the repo's own docs warn about.

**Files:**
- Rewrite: `tools/checkhost.py`
- Create: `tests/test_checkhost.py`

**Interfaces:**
- Consumes: `hdclient.probe`, `hdclient.Refused`, `hdclient.Unreachable`
- Produces: `verdict(results: dict[str, str]) -> tuple[int, str]` — exit code and message

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run and watch it fail**

Run: `./.venv/bin/pytest tests/test_checkhost.py -v`
Expected: `AttributeError: module 'tools.checkhost' has no attribute 'verdict'`.

- [ ] **Step 3: Rewrite `tools/checkhost.py`**

```python
#!/usr/bin/env python3
"""Can this machine run the sweep? Ask, and report honestly.

Three outcomes, deliberately distinct:
  0 GOOD     Home Depot quotes prices here.
  1 BLOCKED  They answered and refused. Datacentre ranges get this.
  2 UNKNOWN  We never reached them. Our problem, not theirs.

Exit 2 used to be reported as BLOCKED, which sent one investigation down
the wrong road entirely.
"""
import sys

from tools import hdclient


def verdict(results):
    pricing = results.get("pricing API", "")

    if pricing == "ok":
        return 0, "GOOD -- Home Depot quotes prices from this machine."

    if pricing.startswith("unreachable"):
        return 2, ("UNKNOWN -- could not reach Home Depot at all.\n"
                   "     This is a local network or TLS problem, not a refusal.\n"
                   "     On macOS this is usually Python missing root certificates:\n"
                   "     run /Applications/Python*/Install\\ Certificates.command\n"
                   f"     detail: {pricing}")

    return 1, ("BLOCKED -- they answered and refused this address.\n"
               "     Datacentre ranges (GitHub Actions, DigitalOcean) get 206 no\n"
               "     matter what the client looks like -- measured six impersonation\n"
               "     profiles, all refused. Run the sweep from a residential\n"
               "     connection instead.\n"
               f"     detail: {pricing}")


def main():
    results = hdclient.probe()
    width = max(len(k) for k in results)
    for host, state in results.items():
        print(f"  {host:<{width}}  {state}")
    print()
    code, msg = verdict(results)
    print(msg)
    return code


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to green**

Run: `./.venv/bin/pytest tests/test_checkhost.py -v`
Expected: 4 passed.

- [ ] **Step 5: Run it for real on both machines**

Run locally: `./.venv/bin/python -m tools.checkhost` → expect `GOOD`, exit 0.
Run on the droplet: `ssh root@174.138.39.96 '/opt/probe/bin/python -m tools.checkhost'` → expect `BLOCKED`, exit 1.
Two machines, two different correct answers, is the proof it works.

- [ ] **Step 6: Commit**

```bash
git add tools/checkhost.py tests/test_checkhost.py
git commit -m "fix: stop reporting a dead wire as a wall

Every exception was reported as BLOCKED. A local certificate failure
announced that Home Depot refuses this address range, which is a
confident answer to a question it never asked."
```

---

### Task 4: Move `sweep.py` onto the client

**Files:**
- Modify: `tools/sweep.py:144-200` (the `call`/`get` layer), `tools/sweep.py:536-588` (`probe`)
- Create: `tests/test_sweep_rows.py`

**Interfaces:**
- Consumes: `hdclient.products`, `hdclient.sitemap`, `hdclient.Refused`, `hdclient.Unreachable`
- Produces: `sweep.row(p: dict, sid: str, meta: dict, before: dict) -> list` — unchanged 15-field row shape

- [ ] **Step 1: Write the failing test for row building**

```python
import json
import pathlib
from tools import sweep

FIX = pathlib.Path(__file__).parent / "fixtures"


def test_row_uses_clearance_plus_dollar_off_as_was_price():
    """pricing.value can itself be a promotion; the discount is computed
    from clearance.value + dollarOff."""
    p = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    r = sweep.row(p, "2502", {}, {})
    assert r[3] == 1.2                      # clearance price
    assert round(r[4], 2) == 49.98          # was = 1.2 + 48.78
    assert r[5] == 98                       # percent off
    assert r[1] == "204767783"
    assert r[10] == "791308000105"          # upc
    assert r[11] == "418625"                # store sku


def test_row_reports_fall_when_price_dropped():
    p = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    r = sweep.row(p, "2502", {}, {"204767783@2502": 3.40})
    assert r[14] == 3.40


def test_row_has_no_fall_when_price_unchanged():
    p = json.loads((FIX / "products_ok.json").read_text())["data"]["products"][0]
    r = sweep.row(p, "2502", {}, {"204767783@2502": 1.2})
    assert r[14] is None
```

- [ ] **Step 2: Run and watch it fail**

Run: `./.venv/bin/pytest tests/test_sweep_rows.py -v`
Expected: FAIL — `sweep.py` imports `urllib` at module load and the fixture path resolution differs; fix imports as part of Step 3.

- [ ] **Step 3: Replace the transport layer in `tools/sweep.py`**

Delete `Q`, `QLITE`, `call`, and `get` (lines ~144–200). Replace with:

```python
from tools import hdclient

BATCH = hdclient.BATCH


def call(ids, sid, lite=False):
    """Kept for call-site compatibility. Refusals now surface instead of
    turning into an empty list that reads as 'nothing on clearance'."""
    return hdclient.products(ids, sid, lite=lite)


def get(url, timeout=60):
    return hdclient.sitemap(url, timeout=timeout)
```

Then update the three call sites — `discover()`, `scan()`, `harvest()` — to pass `lite=True` where they previously passed `QLITE`, and to drop the `query` first argument.

Replace `probe()` (lines ~536–588) entirely with:

```python
def probe():
    from tools import checkhost
    if checkhost.main() != 0:
        die("this machine cannot run the sweep")
```

- [ ] **Step 4: Run tests to green**

Run: `./.venv/bin/pytest tests/ -v`
Expected: all tests pass, including Task 1–3 tests.

- [ ] **Step 5: Run a real scan from a residential machine**

Run: `./.venv/bin/python -m tools.sweep probe scan`
Expected: probe prints `GOOD`; scan takes 30–70 seconds per store and reports a non-zero hit count per store. A run finishing in 6–9 seconds with `0 hits` means refusal — stop and re-check.

- [ ] **Step 6: Commit**

```bash
git add tools/sweep.py tests/test_sweep_rows.py
git commit -m "fix: restore collection by fingerprinting as a browser

The sweep has been refused since Home Depot tightened their client check.
Routing it through hdclient revives it, and a refusal now raises instead
of returning an empty list."
```

---

### Task 5: Schema and migration runner

**Files:**
- Create: `db/migrations/001_core.sql`
- Create: `db/migrate.py`
- Create: `tests/test_migrate.py`

**Interfaces:**
- Consumes: nothing
- Produces:
  - `db.migrate.connect(url: str | None = None) -> psycopg.Connection`
  - `db.migrate.apply(conn) -> list[str]` — names of migrations applied this run
  - `db.migrate.db_url() -> str` — reads `PENNYRUN_DB_URL`, else parses `/root/.pennyrun-db.env`

- [ ] **Step 1: Write `db/migrations/001_core.sql`**

Transcribe §3 of the spec verbatim. It is the authoritative source; do not improvise column names.

```sql
create table if not exists schema_migration (
  name       text primary key,
  applied_at timestamptz not null default now()
);

create table if not exists product (
  item_id        text primary key,
  name           text not null,
  category       text,
  upc            text,
  store_sku      text,
  model_number   text,
  canonical_url  text,
  replacement_id text,
  first_seen     date not null default current_date,
  last_seen      date not null default current_date
);
create index if not exists product_upc_idx on product (upc);

create table if not exists store (
  store_id text primary key,
  name     text,
  street   text,
  city     text,
  state    text,
  zip      text,
  lat      double precision,
  lon      double precision
);
create index if not exists store_zip_idx on store (zip);
create index if not exists store_geo_idx on store (lat, lon);

create table if not exists device (
  device_id     uuid primary key,
  created_at    timestamptz not null default now(),
  last_seen     timestamptz,
  trust_score   numeric(4,2) not null default 0.5,
  transfer_code text unique
);

create table if not exists observation (
  id              bigserial primary key,
  item_id         text not null references product,
  store_id        text not null references store,
  observed_at     timestamptz not null default now(),
  list_price      numeric(10,2),
  clearance_price numeric(10,2),
  pct_off         numeric(5,2),
  quantity        integer,
  store_only      boolean,
  source          text not null check (source in ('discovery','phone','confirmation')),
  device_id       uuid references device,
  trusted         boolean not null default false
);
create index if not exists obs_item_store_idx on observation (item_id, store_id, observed_at desc);
create index if not exists obs_store_idx on observation (store_id, observed_at desc)
  where clearance_price is not null;
create unique index if not exists obs_unique_idx on observation (item_id, store_id, observed_at);

create table if not exists candidate (
  item_id      text primary key references product,
  first_marked date not null,
  last_marked  date not null,
  best_pct_off numeric(5,2),
  store_count  integer,
  penny_score  numeric(5,2),
  updated_at   timestamptz not null default now()
);
create index if not exists candidate_score_idx on candidate (penny_score desc, last_marked desc);

create table if not exists device_store (
  device_id uuid references device,
  store_id  text references store,
  primary key (device_id, store_id)
);

create table if not exists confirmation (
  id            bigserial primary key,
  item_id       text not null references product,
  store_id      text not null references store,
  device_id     uuid references device,
  scanned_price numeric(10,2) not null,
  is_penny      boolean generated always as (scanned_price <= 0.01) stored,
  confirmed_at  timestamptz not null default now()
);
```

- [ ] **Step 2: Write the failing test**

```python
import os
import pytest
from db import migrate

pytestmark = pytest.mark.skipif(
    not os.environ.get("PENNYRUN_DB_URL"),
    reason="needs PENNYRUN_DB_URL pointing at a scratch database")


def test_apply_is_idempotent():
    conn = migrate.connect()
    first = migrate.apply(conn)
    assert "001_core.sql" in first
    second = migrate.apply(conn)
    assert second == []


def test_observation_rejects_bad_source():
    conn = migrate.connect()
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9999') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t1','t') on conflict do nothing")
        with pytest.raises(Exception):
            cur.execute("insert into observation (item_id, store_id, source) "
                        "values ('t1','9999','nonsense')")
    conn.rollback()


def test_is_penny_is_computed():
    conn = migrate.connect()
    migrate.apply(conn)
    with conn.cursor() as cur:
        cur.execute("insert into store (store_id) values ('9998') on conflict do nothing")
        cur.execute("insert into product (item_id, name) values ('t2','t') on conflict do nothing")
        cur.execute("insert into confirmation (item_id, store_id, scanned_price) "
                    "values ('t2','9998',0.01) returning is_penny")
        assert cur.fetchone()[0] is True
    conn.rollback()
```

- [ ] **Step 3: Run and watch it fail**

Run: `./.venv/bin/pytest tests/test_migrate.py -v`
Expected: collection error — `No module named 'db'`.

- [ ] **Step 4: Implement `db/migrate.py`**

```python
"""Apply SQL migrations in filename order, once each."""
import pathlib
import psycopg

HERE = pathlib.Path(__file__).parent
MIGRATIONS = HERE / "migrations"
ENV_FILE = pathlib.Path("/root/.pennyrun-db.env")


def db_url():
    import os
    url = os.environ.get("PENNYRUN_DB_URL")
    if url:
        return url
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            if line.startswith("PENNYRUN_DB_URL="):
                return line.split("=", 1)[1].strip()
    raise RuntimeError("no PENNYRUN_DB_URL in env or /root/.pennyrun-db.env")


def connect(url=None):
    return psycopg.connect(url or db_url())


def applied(conn):
    with conn.cursor() as cur:
        cur.execute("create table if not exists schema_migration ("
                    "name text primary key, applied_at timestamptz not null default now())")
        conn.commit()
        cur.execute("select name from schema_migration")
        return {r[0] for r in cur.fetchall()}


def apply(conn):
    done = applied(conn)
    ran = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        if path.name in done:
            continue
        with conn.cursor() as cur:
            cur.execute(path.read_text())
            cur.execute("insert into schema_migration (name) values (%s)", (path.name,))
        conn.commit()
        ran.append(path.name)
    return ran


if __name__ == "__main__":
    with connect() as c:
        for name in apply(c) or ["(nothing new)"]:
            print("applied", name)
```

Create `db/__init__.py` (empty). Add `psycopg[binary]` to `tools/requirements.txt`.

- [ ] **Step 5: Run tests to green**

```bash
sudo -u postgres createdb pennyrun_test
PENNYRUN_DB_URL=postgresql:///pennyrun_test ./.venv/bin/pytest tests/test_migrate.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Apply to the droplet**

```bash
ssh root@174.138.39.96 'cd /opt/pennyrun && /opt/pennyrun/.venv/bin/python -m db.migrate'
```

Expected: `applied 001_core.sql`.

- [ ] **Step 7: Commit**

```bash
git add db/ tests/test_migrate.py tools/requirements.txt
git commit -m "feat: add the price-history schema

observation is append-only on purpose: the worst failure this system had
was a collapsed run overwriting a good list, and a log cannot be
overwritten by a bad night."
```

---

### Task 6: Seed stores and products

**Files:**
- Create: `db/seed.py`
- Create: `tests/test_seed.py`

**Interfaces:**
- Consumes: `db.migrate.connect`
- Produces:
  - `db.seed.stores(conn, path: str) -> int`
  - `db.seed.products(conn, path: str) -> int`

- [ ] **Step 1: Write the failing test**

```python
import os
import pytest
from db import migrate, seed

pytestmark = pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")


def test_seeds_all_stores():
    conn = migrate.connect()
    migrate.apply(conn)
    n = seed.stores(conn, "pennyrun/hd-stores.json")
    assert n == 2021
    with conn.cursor() as cur:
        cur.execute("select name, city, state from store where store_id = '2502'")
        assert cur.fetchone()[2] == "MD"


def test_seeding_twice_does_not_duplicate():
    conn = migrate.connect()
    migrate.apply(conn)
    seed.stores(conn, "pennyrun/hd-stores.json")
    seed.stores(conn, "pennyrun/hd-stores.json")
    with conn.cursor() as cur:
        cur.execute("select count(*) from store")
        assert cur.fetchone()[0] == 2021


def test_seeds_products_from_the_existing_list():
    conn = migrate.connect()
    migrate.apply(conn)
    seed.stores(conn, "pennyrun/hd-stores.json")
    n = seed.products(conn, "pennyrun/clearance.json")
    assert n == 811, "3,000 rows deduplicate to 811 distinct items"
```

- [ ] **Step 2: Run and watch it fail**

Run: `PENNYRUN_DB_URL=postgresql:///pennyrun_test ./.venv/bin/pytest tests/test_seed.py -v`
Expected: `No module named 'db.seed'`.

- [ ] **Step 3: Implement `db/seed.py`**

```python
"""One-time load of the catalogue facts we already have on disk."""
import json


def stores(conn, path):
    rows = json.load(open(path))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into store (store_id, name, street, city, state, zip) "
            "values (%s,%s,%s,%s,%s,%s) on conflict (store_id) do update set "
            "name = excluded.name, street = excluded.street, city = excluded.city, "
            "state = excluded.state, zip = excluded.zip",
            [(r[0], r[1], r[2], r[3], r[4], r[5]) for r in rows])
    conn.commit()
    return len(rows)


def products(conn, path):
    hits = json.load(open(path))["hits"]
    seen = {}
    for r in hits:
        seen.setdefault(r[1], (r[1], r[0], r[2], r[10], r[11], r[12], r[9]))
    with conn.cursor() as cur:
        cur.executemany(
            "insert into product (item_id, name, category, upc, store_sku, "
            "model_number, canonical_url) values (%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (item_id) do update set last_seen = current_date",
            list(seen.values()))
    conn.commit()
    return len(seen)


if __name__ == "__main__":
    from db import migrate
    with migrate.connect() as c:
        migrate.apply(c)
        print("stores  ", stores(c, "pennyrun/hd-stores.json"))
        print("products", products(c, "pennyrun/clearance.json"))
```

- [ ] **Step 4: Run tests to green**

Run: `PENNYRUN_DB_URL=postgresql:///pennyrun_test ./.venv/bin/pytest tests/test_seed.py -v`
Expected: 3 passed. If the product count is not 811, the row indices are wrong — field 1 is `itemId`, not field 0.

- [ ] **Step 5: Commit**

```bash
git add db/seed.py tests/test_seed.py
git commit -m "feat: seed the catalogue from what we already collected"
```

---

### Task 7: The API — health, config, and the read endpoints

**Files:**
- Create: `api/main.py`, `api/db.py`, `api/requirements.txt`
- Create: `tests/test_api_read.py`

**Interfaces:**
- Consumes: `db.migrate.db_url`
- Produces: FastAPI app at `api.main:app` with
  - `GET /api/v1/health` → `{"ok": true, "stores": int, "observations": int}`
  - `GET /api/v1/stores?zip=&lat=&lon=&n=` → `[{store_id, name, city, state, zip, miles?}]`
  - `GET /api/v1/store/{store_id}/clearance?limit=&min_pct=` → `[{item_id, name, clearance_price, list_price, pct_off, quantity, observed_at}]`
  - `GET /api/v1/item/{item_id}?stores=a,b,c` → `{item_id, name, upc, prices: [{store_id, clearance_price, observed_at}]}`
  - `GET /api/v1/lookup?upc=` → same shape as `/item/{id}` or 404

- [ ] **Step 1: Write the failing tests**

```python
import os
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")


@pytest.fixture(scope="module")
def client():
    from db import migrate, seed
    conn = migrate.connect()
    migrate.apply(conn)
    seed.stores(conn, "pennyrun/hd-stores.json")
    seed.products(conn, "pennyrun/clearance.json")
    with conn.cursor() as cur:
        cur.execute(
            "insert into observation (item_id, store_id, list_price, clearance_price, "
            "pct_off, quantity, source, trusted) values "
            "('204767783','2502',49.98,1.20,98,3,'discovery',true) "
            "on conflict do nothing")
    conn.commit()
    from api.main import app
    return TestClient(app)


def test_health(client):
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert r.json()["stores"] == 2021


def test_stores_by_zip(client):
    r = client.get("/api/v1/stores?zip=21234")
    assert r.status_code == 200
    assert any(s["store_id"] == "2577" for s in r.json())


def test_store_clearance_returns_the_seeded_row(client):
    r = client.get("/api/v1/store/2502/clearance?limit=50")
    assert r.status_code == 200
    rows = r.json()
    assert rows[0]["item_id"] == "204767783"
    assert rows[0]["clearance_price"] == "1.20"
    assert rows[0]["pct_off"] == "98.00"


def test_item_cross_store_compare(client):
    r = client.get("/api/v1/item/204767783?stores=2502,2504,2577")
    assert r.status_code == 200
    body = r.json()
    assert body["item_id"] == "204767783"
    prices = {p["store_id"]: p for p in body["prices"]}
    assert prices["2502"]["clearance_price"] == "1.20"
    assert "observed_at" in prices["2502"], "staleness must be visible to the phone"


def test_lookup_by_upc(client):
    r = client.get("/api/v1/lookup?upc=791308000105")
    assert r.status_code == 200
    assert r.json()["item_id"] == "204767783"


def test_lookup_unknown_upc_is_404(client):
    assert client.get("/api/v1/lookup?upc=000000000000").status_code == 404
```

- [ ] **Step 2: Run and watch it fail**

Run: `PENNYRUN_DB_URL=postgresql:///pennyrun_test ./.venv/bin/pytest tests/test_api_read.py -v`
Expected: `No module named 'api'`.

- [ ] **Step 3: Implement the API**

`api/requirements.txt`:

```
fastapi
uvicorn[standard]
psycopg[binary]
```

`api/db.py`:

```python
from contextlib import contextmanager
import psycopg
from psycopg.rows import dict_row
from db.migrate import db_url

_pool_url = db_url()


@contextmanager
def rows():
    with psycopg.connect(_pool_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            yield cur
```

`api/main.py`:

```python
from fastapi import FastAPI, HTTPException, Query
from api.db import rows

app = FastAPI(title="Penny Run", docs_url="/api/v1/docs")
V = "/api/v1"

LATEST = """
select distinct on (o.item_id, o.store_id)
       o.item_id, o.store_id, o.list_price, o.clearance_price,
       o.pct_off, o.quantity, o.store_only, o.observed_at
  from observation o
 where o.trusted
 order by o.item_id, o.store_id, o.observed_at desc
"""


@app.get(V + "/health")
def health():
    with rows() as cur:
        cur.execute("select (select count(*) from store) as stores, "
                    "(select count(*) from observation) as observations")
        r = cur.fetchone()
    return {"ok": True, **r}


@app.get(V + "/stores")
def stores(zip: str | None = None, lat: float | None = None,
           lon: float | None = None, n: int = Query(10, le=50)):
    with rows() as cur:
        if zip:
            cur.execute("select store_id, name, street, city, state, zip "
                        "from store where zip = %s limit %s", (zip, n))
        elif lat is not None and lon is not None:
            cur.execute(
                "select store_id, name, street, city, state, zip, "
                "  3959 * acos(greatest(-1, least(1, "
                "    cos(radians(%s))*cos(radians(lat))*cos(radians(lon)-radians(%s))"
                "    + sin(radians(%s))*sin(radians(lat))))) as miles "
                "from store where lat is not null order by miles limit %s",
                (lat, lon, lat, n))
        else:
            raise HTTPException(400, "give a zip, or lat and lon")
        return cur.fetchall()


@app.get(V + "/store/{store_id}/clearance")
def store_clearance(store_id: str, limit: int = Query(200, le=1000),
                    min_pct: float = 0):
    with rows() as cur:
        cur.execute(
            f"with latest as ({LATEST}) "
            "select l.item_id, p.name, p.category, p.upc, p.canonical_url, "
            "       l.list_price, l.clearance_price, l.pct_off, l.quantity, "
            "       l.store_only, l.observed_at, c.penny_score "
            "  from latest l join product p using (item_id) "
            "  left join candidate c using (item_id) "
            " where l.store_id = %s and l.clearance_price is not null "
            "   and coalesce(l.pct_off, 0) >= %s "
            " order by c.penny_score desc nulls last, l.pct_off desc limit %s",
            (store_id, min_pct, limit))
        return cur.fetchall()


@app.get(V + "/item/{item_id}")
def item(item_id: str, stores: str | None = None):
    ids = [s for s in (stores or "").split(",") if s]
    with rows() as cur:
        cur.execute("select item_id, name, category, upc, store_sku, "
                    "model_number, canonical_url from product where item_id = %s",
                    (item_id,))
        head = cur.fetchone()
        if not head:
            raise HTTPException(404, "unknown item")
        if ids:
            cur.execute(f"with latest as ({LATEST}) select * from latest "
                        "where item_id = %s and store_id = any(%s)", (item_id, ids))
        else:
            cur.execute(f"with latest as ({LATEST}) select * from latest "
                        "where item_id = %s", (item_id,))
        head["prices"] = cur.fetchall()
    return head


@app.get(V + "/lookup")
def lookup(upc: str):
    with rows() as cur:
        cur.execute("select item_id from product where upc = %s", (upc,))
        r = cur.fetchone()
    if not r:
        raise HTTPException(404, "no product with that barcode")
    return item(r["item_id"])
```

Create `api/__init__.py` (empty). Add `fastapi`, `uvicorn[standard]`, `httpx` to `tools/requirements.txt` for tests.

- [ ] **Step 4: Run tests to green**

Run: `PENNYRUN_DB_URL=postgresql:///pennyrun_test ./.venv/bin/pytest tests/test_api_read.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add api/ tests/test_api_read.py tools/requirements.txt
git commit -m "feat: serve price history over a read API

Cross-store compare ships observed_at on every row, because a cached
price the user cannot tell is stale is worse than no price."
```

---

### Task 8: Ingest — discovery uploads, with bounds

**Files:**
- Create: `api/ingest.py`, `api/validate.py`
- Modify: `api/main.py` (mount the router)
- Create: `tests/test_validate.py`, `tests/test_api_ingest.py`

**Interfaces:**
- Consumes: `api.db.rows`
- Produces:
  - `api.validate.check(obs: dict) -> None` — raises `ValueError` on bad data
  - `POST /api/v1/discovery` with header `Authorization: Bearer <PENNYRUN_INGEST_TOKEN>` → `{"accepted": int, "rejected": int}`

- [ ] **Step 1: Write the failing validation tests**

```python
import pytest
from api import validate

GOOD = {"item_id": "204767783", "store_id": "2502", "list_price": "49.98",
        "clearance_price": "1.20", "pct_off": "98", "quantity": 3}


def test_accepts_a_good_row():
    validate.check(dict(GOOD))


@pytest.mark.parametrize("field,value,msg", [
    ("clearance_price", "60.00", "above list"),
    ("clearance_price", "-1", "below"),
    ("clearance_price", "200000", "above"),
    ("pct_off", "140", "percent"),
    ("pct_off", "-5", "percent"),
    ("quantity", -3, "quantity"),
])
def test_rejects_impossible_values(field, value, msg):
    bad = dict(GOOD)
    bad[field] = value
    with pytest.raises(ValueError, match=msg):
        validate.check(bad)


def test_rejects_missing_ids():
    with pytest.raises(ValueError, match="item_id"):
        validate.check({"store_id": "2502", "clearance_price": "1.00"})
```

- [ ] **Step 2: Run and watch it fail**

Run: `./.venv/bin/pytest tests/test_validate.py -v`
Expected: `No module named 'api.validate'`.

- [ ] **Step 3: Implement `api/validate.py`**

```python
"""Bounds every observation must clear, whoever sent it.

Discovery is trusted because we run it. Phones are not, because anyone
can POST. These rules apply to both — a bug in our own collector is as
capable of poisoning the list as a stranger is.
"""
from decimal import Decimal, InvalidOperation

MAX_PRICE = Decimal("100000")
MIN_PRICE = Decimal("0.01")


def _money(raw, field):
    try:
        return Decimal(str(raw))
    except (InvalidOperation, TypeError):
        raise ValueError(f"{field} is not a number: {raw!r}")


def check(obs):
    for required in ("item_id", "store_id"):
        if not obs.get(required):
            raise ValueError(f"{required} is required")

    clearance = obs.get("clearance_price")
    listed = obs.get("list_price")

    if clearance is not None:
        c = _money(clearance, "clearance_price")
        if c < MIN_PRICE:
            raise ValueError(f"clearance_price below {MIN_PRICE}: {c}")
        if c > MAX_PRICE:
            raise ValueError(f"clearance_price above {MAX_PRICE}: {c}")
        if listed is not None and c > _money(listed, "list_price"):
            raise ValueError("clearance_price above list price")

    pct = obs.get("pct_off")
    if pct is not None:
        p = _money(pct, "pct_off")
        if not (0 <= p <= 100):
            raise ValueError(f"pct_off outside 0-100 percent: {p}")

    qty = obs.get("quantity")
    if qty is not None and int(qty) < 0:
        raise ValueError(f"negative quantity: {qty}")
```

- [ ] **Step 4: Write the failing ingest test**

```python
import os
import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")
TOKEN = "test-token"


@pytest.fixture(scope="module")
def client(monkeypatch_session=None):
    os.environ["PENNYRUN_INGEST_TOKEN"] = TOKEN
    from db import migrate, seed
    conn = migrate.connect()
    migrate.apply(conn)
    seed.stores(conn, "pennyrun/hd-stores.json")
    seed.products(conn, "pennyrun/clearance.json")
    from api.main import app
    return TestClient(app)


BODY = {"observations": [
    {"item_id": "204767783", "store_id": "2502", "list_price": "49.98",
     "clearance_price": "1.20", "pct_off": "98", "quantity": 3}]}


def test_rejects_without_token(client):
    assert client.post("/api/v1/discovery", json=BODY).status_code == 401


def test_rejects_wrong_token(client):
    r = client.post("/api/v1/discovery", json=BODY, headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_accepts_and_marks_trusted(client):
    r = client.post("/api/v1/discovery", json=BODY,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["accepted"] == 1
    got = client.get("/api/v1/store/2502/clearance").json()
    assert any(row["item_id"] == "204767783" for row in got)


def test_bad_rows_are_counted_not_fatal(client):
    body = {"observations": [
        BODY["observations"][0],
        {"item_id": "204767783", "store_id": "2502", "clearance_price": "999999"}]}
    r = client.post("/api/v1/discovery", json=body,
                    headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json()["rejected"] == 1
```

- [ ] **Step 5: Run and watch it fail**

Run: `PENNYRUN_DB_URL=postgresql:///pennyrun_test ./.venv/bin/pytest tests/test_api_ingest.py -v`
Expected: 404 on `/api/v1/discovery`.

- [ ] **Step 6: Implement `api/ingest.py`**

```python
import os
from fastapi import APIRouter, Header, HTTPException
from api import validate
from api.db import rows

router = APIRouter()


def _authorise(header):
    token = os.environ.get("PENNYRUN_INGEST_TOKEN")
    if not token:
        raise HTTPException(500, "PENNYRUN_INGEST_TOKEN is not set on the server")
    if header != f"Bearer {token}":
        raise HTTPException(401, "bad or missing ingest token")


@router.post("/api/v1/discovery")
def discovery(payload: dict, authorization: str = Header(None)):
    _authorise(authorization)
    accepted, rejected = [], 0
    for obs in payload.get("observations", []):
        try:
            validate.check(obs)
        except ValueError:
            rejected += 1
            continue
        accepted.append(obs)

    with rows() as cur:
        for o in accepted:
            cur.execute(
                "insert into observation (item_id, store_id, list_price, "
                "clearance_price, pct_off, quantity, store_only, source, trusted) "
                "values (%s,%s,%s,%s,%s,%s,%s,'discovery',true) "
                "on conflict do nothing",
                (o["item_id"], o["store_id"], o.get("list_price"),
                 o.get("clearance_price"), o.get("pct_off"),
                 o.get("quantity"), o.get("store_only")))
    return {"accepted": len(accepted), "rejected": rejected}
```

In `api/main.py`, after the app is created, add:

```python
from api.ingest import router as ingest_router
app.include_router(ingest_router)
```

- [ ] **Step 7: Run all tests to green**

Run: `PENNYRUN_DB_URL=postgresql:///pennyrun_test ./.venv/bin/pytest tests/ -v`
Expected: everything passes.

- [ ] **Step 8: Commit**

```bash
git add api/ingest.py api/validate.py api/main.py tests/test_validate.py tests/test_api_ingest.py
git commit -m "feat: accept discovery uploads behind a token and bounds check

Bad rows are counted and dropped rather than failing the batch: one
malformed product should not cost a night's collection."
```

---

### Task 9: Point the sweep at the droplet, and deploy

**Files:**
- Create: `tools/upload.py`
- Modify: `tools/sweep.py` (`scan()` tail)
- Create: `deploy/pennyrun-api.service`
- Rewrite: `deploy/Caddyfile`
- Create: `tests/test_upload.py`

**Interfaces:**
- Consumes: `hdclient`, the `POST /api/v1/discovery` endpoint
- Produces: `tools.upload.send(rows: list, base_url: str, token: str) -> dict`

- [ ] **Step 1: Write the failing test**

```python
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


def test_batches_are_capped(monkeypatch):
    sent = []
    monkeypatch.setattr(upload, "_post", lambda url, body, token: sent.append(body) or {"accepted": len(body["observations"]), "rejected": 0})
    upload.send([ROW] * 2500, "http://x", "t")
    assert len(sent) == 3, "2500 rows should go in batches of 1000"
```

- [ ] **Step 2: Run and watch it fail**

Run: `./.venv/bin/pytest tests/test_upload.py -v`
Expected: `No module named 'tools.upload'`.

- [ ] **Step 3: Implement `tools/upload.py`**

```python
"""Ship a sweep's rows to the droplet. Runs on the home box."""
import json
import urllib.request

CHUNK = 1000


def to_observation(row):
    return {"item_id": row[1], "store_id": row[6],
            "clearance_price": row[3], "list_price": row[4],
            "pct_off": row[5], "quantity": row[7],
            "store_only": bool(row[8])}


def _post(url, body, token):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())


def send(rows, base_url, token):
    url = base_url.rstrip("/") + "/api/v1/discovery"
    total = {"accepted": 0, "rejected": 0}
    for i in range(0, len(rows), CHUNK):
        body = {"observations": [to_observation(r) for r in rows[i:i + CHUNK]]}
        got = _post(url, body, token)
        total["accepted"] += got.get("accepted", 0)
        total["rejected"] += got.get("rejected", 0)
    return total
```

Note: this uses `urllib` deliberately — it talks to *your* droplet, not Home Depot, so no impersonation is needed and the constraint about `hdclient` does not apply.

At the end of `sweep.scan()`, after the file is written, add:

```python
    base = os.environ.get("PENNYRUN_API")
    token = os.environ.get("PENNYRUN_INGEST_TOKEN")
    if base and token:
        from tools import upload
        got = upload.send(hits, base, token)
        say("uploaded: %d accepted, %d rejected" % (got["accepted"], got["rejected"]))
```

- [ ] **Step 4: Run tests to green**

Run: `./.venv/bin/pytest tests/test_upload.py -v`
Expected: 2 passed.

- [ ] **Step 5: Write the service unit and Caddyfile**

`deploy/pennyrun-api.service`:

```ini
[Unit]
Description=Penny Run API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=pennyrun
WorkingDirectory=/opt/pennyrun
EnvironmentFile=/root/.pennyrun-db.env
EnvironmentFile=/etc/pennyrun/ingest.env
ExecStart=/opt/pennyrun/.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Rewrite `deploy/Caddyfile`:

```
{$PENNYRUN_HOST} {
	encode gzip zstd

	handle /api/* {
		reverse_proxy 127.0.0.1:8000
	}

	handle {
		root * {$PENNYRUN_ROOT}
		@shell path / /index.html /sw.js /manifest.webmanifest
		header @shell Cache-Control "no-cache"
		@static path /ocr/* /zxing.min.js /stores.json /hd-stores.json /*.png
		header @static Cache-Control "public, max-age=604800"
		file_server
	}

	log {
		output file /var/log/caddy/pennyrun.log
		format console
	}
}
```

The `/clearance.json` handler is gone — the app reads the API now.

- [ ] **Step 6: Deploy and verify end to end**

```bash
# on the droplet
ssh root@174.138.39.96 'bash -s' <<'EOF'
set -e
apt-get install -y -qq caddy
mkdir -p /etc/pennyrun
[ -f /etc/pennyrun/ingest.env ] || \
  printf 'PENNYRUN_INGEST_TOKEN=%s\n' "$(openssl rand -hex 24)" > /etc/pennyrun/ingest.env
chmod 600 /etc/pennyrun/ingest.env
git clone https://github.com/PlanetBandit/pennyrun.git /opt/pennyrun 2>/dev/null || \
  git -C /opt/pennyrun pull
python3 -m venv /opt/pennyrun/.venv
/opt/pennyrun/.venv/bin/pip install -q -r /opt/pennyrun/api/requirements.txt
id pennyrun >/dev/null 2>&1 || useradd --system --home /opt/pennyrun pennyrun
chown -R pennyrun:pennyrun /opt/pennyrun
/opt/pennyrun/.venv/bin/python -m db.migrate
/opt/pennyrun/.venv/bin/python -m db.seed
install -m644 /opt/pennyrun/deploy/pennyrun-api.service /etc/systemd/system/
sed -e "s|{\$PENNYRUN_HOST}|penny.premofusa.shop|g" \
    -e "s|{\$PENNYRUN_ROOT}|/opt/pennyrun/pennyrun|g" \
    /opt/pennyrun/deploy/Caddyfile > /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now pennyrun-api caddy
EOF
```

Verify: `curl https://penny.premofusa.shop/api/v1/health`
Expected: `{"ok":true,"stores":2021,"observations":0}` over a valid certificate.

Then from the Mac:

```bash
export PENNYRUN_API=https://penny.premofusa.shop
export PENNYRUN_INGEST_TOKEN=$(ssh root@174.138.39.96 'grep -o "[a-f0-9]\{48\}" /etc/pennyrun/ingest.env')
./.venv/bin/python -m tools.sweep probe scan
curl https://penny.premofusa.shop/api/v1/store/2502/clearance?limit=5
```

Expected: the sweep reports `uploaded: N accepted, 0 rejected`, and the API returns real rows collected minutes earlier from your house, served from a datacentre that cannot collect them itself. That is the whole architecture working.

- [ ] **Step 7: Commit**

```bash
git add tools/upload.py tools/sweep.py deploy/ tests/test_upload.py
git commit -m "feat: sweep uploads to the droplet, droplet serves the API

Home collects because it can; the droplet serves because it cannot."
```

---

## Self-review against the spec

| spec section | covered by |
|---|---|
| §1 constraint, impersonation profile, Refused vs Unreachable | Tasks 2, 3 |
| §2 topology — home discovers, droplet serves | Tasks 4, 9 |
| §3 data model — all eight tables, indexes, append-only | Task 5 |
| §4 penny prediction | **Deferred to Plan 3** — needs weeks of accumulated history before weights mean anything. `candidate.penny_score` exists and is read by `/store/{id}/clearance`; nothing writes it yet. |
| §5 API read endpoints | Task 7 (`/stores`, `/store/{id}/clearance`, `/item/{id}`, `/lookup`, `/health`) |
| §5 API write endpoints | Task 8 (`/discovery`). `/observations`, `/confirmations`, `/device/*`, `/work` are **Plan 2** — they exist to serve the app, and the app does not exist yet. |
| §6 trust — bounds | Task 8. Corroboration, rate limits, spot re-verification and trust decay are **Plan 2**, gated on phone submissions existing. |
| §7 the app — Shortcut removed | Task 1. Full rewrite is **Plan 2**. |
| §8 serving | Task 9 |
| §9 failure modes — probe distinguishes transport from refusal | Tasks 2, 3 |
| §10 non-goals | honoured — no accounts, no proxies, no Chromium, no managed Postgres |
| §11 build order | this plan is steps 1–5 of that order, plus the Shortcut removal pulled forward at your request |

**Known gaps, deliberate:** `candidate` is never populated in this plan (Plan 3), and `device`/`confirmation` are created but unused (Plan 2). They are in the schema now because adding tables later is easy and migrating a live `observation` table is not.
