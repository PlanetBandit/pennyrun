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
