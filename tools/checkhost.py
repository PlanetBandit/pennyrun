#!/usr/bin/env python3
"""Can this machine run the sweep?

Home Depot answers some address ranges and not others. GitHub's hosted
runners get 206 "Generic Errors API" from the pricing API no matter what
headers they send; a residential address gets 200. Datacentre ranges vary,
so the only way to know about any given box is to ask from it.

Standalone on purpose -- no repo, no checkout, no dependencies:

  curl -fsSL https://raw.githubusercontent.com/PlanetBandit/pennyrun/main/tools/checkhost.py | python3 -
"""

import json
import sys
import urllib.error
import urllib.request

API = "https://apionline.homedepot.com/federation-gateway/graphql"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

# One real product at one real store. A working host quotes a price;
# a blocked one returns the generic error with no data in it.
QUERY = ('query q($ids: [String!]!) { products(itemIds: $ids) { itemId '
         'identifiers { productLabel } '
         'pricing(storeId: "2577", isBrandPricingPolicyCompliant: false) '
         '{ value clearance { value } } } }')


def hit(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read(600).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read(300).decode("utf-8", "replace")
    except Exception as e:
        return None, str(e)


def main():
    body = json.dumps({"operationName": "q", "variables": {"ids": ["100161821"]},
                       "query": QUERY}).encode()
    status, text = hit(API, body, {"Content-Type": "application/json",
                                   "x-experience-name": "general-merchandise",
                                   "User-Agent": UA})
    priced = False
    try:
        products = ((json.loads(text).get("data") or {}).get("products") or [])
        priced = bool(products and (products[0].get("pricing") or {}).get("value"))
    except Exception:
        pass

    print("pricing API   HTTP %s  %s" % (status, "quoted a price" if priced
                                         else text[:120].replace("\n", " ")))

    s_map, _ = hit("https://www.homedepot.com/sitemap/P/PIPs.xml")
    s_search, _ = hit("https://www.homedepot.com/s/mulch%20clearance")
    print("sitemap       HTTP %s" % s_map)
    print("search page   HTTP %s" % s_search)
    print()

    if priced:
        print("GOOD -- this machine can run the sweep.")
        if s_search != 200:
            print("     Search pages are blocked, so the weekly 'harvest' stage will")
            print("     fail here. It is the least useful stage; discover and scan")
            print("     are what matter and both only need the API and the sitemap.")
        return 0

    print("BLOCKED -- Home Depot will not quote prices to this address range.")
    print("     No header or user-agent changes this; it was measured four ways")
    print("     from a GitHub runner and all four were refused. The sweep needs")
    print("     to run somewhere else -- a home network is the reliable answer.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
