#!/usr/bin/env python3
"""Rebuild pennyrun/clearance.json from Home Depot's own pricing API.

Two stages:

  harvest  scrape product ids out of Home Depot's clearance search pages
           into tools/pool.json. The pool moves slowly, so this runs
           weekly rather than nightly.

  scan     ask the pricing API for every product in the pool, once per
           store in tools/stores.json, and keep the ones that come back
           with an in-store clearance price.

The API takes no key and no cookie, but it will only quote a price when
a store is named -- an unlocalised request returns null. That is the
whole reason this list has to be built store by store.

  python3 tools/sweep.py scan
  python3 tools/sweep.py harvest scan
"""

import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
POOL = os.path.join(HERE, "pool.json")
STORES = os.path.join(HERE, "stores.json")
OUT = os.path.join(ROOT, "pennyrun", "clearance.json")

API = "https://apionline.homedepot.com/federation-gateway/graphql"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

BATCH = 16        # the cap the products() query enforces on itemIds
WORKERS = 6
PAGES = [0, 24, 48, 72]

# Every store aisle worth walking. The search terms are deliberately
# mundane -- the deepest markdowns sit on things nobody thinks to look up.
CATS = {
    "Paint": ["paint", "spray paint", "primer", "paint brushes", "stain", "caulk"],
    "Tools": ["tools", "power tools", "hand tools", "drill", "saw", "tool storage",
              "wrench", "screwdriver", "sander", "air compressor"],
    "Lighting": ["lighting", "ceiling light", "outdoor lighting", "lamps", "light bulbs",
                 "chandelier", "ceiling fan light", "landscape lighting"],
    "Flooring": ["flooring", "tile", "area rugs", "laminate", "vinyl plank", "carpet",
                 "hardwood flooring", "underlayment"],
    "Appliances": ["appliances", "refrigerator", "microwave", "dishwasher", "washer",
                   "dryer", "range", "freezer", "small appliances"],
    "Bath": ["bath", "vanity", "faucet", "shower", "toilet", "bath accessories",
             "medicine cabinet", "shower door"],
    "Kitchen": ["kitchen", "cabinet", "countertop", "kitchen faucet", "sink",
                "backsplash", "range hood"],
    "Garden": ["garden", "plants", "planters", "lawn care", "fertilizer", "mulch",
               "garden tools", "hose", "sprinkler", "seeds"],
    "Patio": ["patio furniture", "outdoor decor", "patio umbrella", "outdoor cushions",
              "fire pit", "gazebo", "patio set"],
    "Grills": ["grills", "smoker", "grill accessories", "charcoal", "griddle"],
    "Pools": ["pools", "pool chemicals", "pool toys", "pool pump"],
    "Fans & AC": ["fans", "air conditioner", "ceiling fan", "dehumidifier", "air purifier"],
    "Heating": ["heater", "fireplace", "furnace filter", "thermostat"],
    "Storage": ["storage", "shelving", "closet", "totes", "garage storage", "workbench",
                "storage cabinet", "shed"],
    "Hardware": ["hardware", "door hardware", "cabinet hardware", "fasteners", "screws",
                 "hinges", "locks", "door knob"],
    "Electrical": ["electrical", "wire", "outlet", "light switch", "extension cord",
                   "circuit breaker", "generator", "smart plug"],
    "Plumbing": ["plumbing", "pipe", "water heater", "sump pump", "drain", "valve"],
    "Decor": ["home decor", "mirrors", "wall art", "curtains", "throw pillows",
              "picture frames", "clocks", "candles"],
    "Furniture": ["furniture", "office chair", "desk", "bar stool", "bookcase",
                  "dining set", "sofa", "mattress"],
    "Doors": ["doors", "screen door", "interior door", "storm door", "barn door"],
    "Windows": ["window", "blinds", "shades", "window film", "curtain rod"],
    "Building": ["building materials", "lumber", "insulation", "drywall", "plywood",
                 "concrete", "roofing", "siding", "fencing", "deck"],
    "Safety": ["safety", "gloves", "smoke detector", "fire extinguisher", "security camera"],
    "Cleaning": ["cleaning", "vacuum", "mop", "trash can", "laundry", "cleaner"],
    "Smart Home": ["smart home", "doorbell", "smart lock", "smart thermostat"],
    "Holiday": ["holiday decor", "christmas", "halloween", "string lights", "wreath"],
    "Ladders": ["ladders", "step stool", "scaffolding"],
    "Outdoor": ["outdoor power", "lawn mower", "leaf blower", "trimmer", "pressure washer",
                "snow blower", "chainsaw"],
    "Auto": ["automotive", "car care", "tarp", "hand truck"],
    "Pet": ["pet supplies", "dog"],
}

LINK = re.compile(r"/p/([A-Za-z0-9][A-Za-z0-9%-]{6,90})/(\d{6,})")


# ---------------------------------------------------------------- harvest

def harvest():
    jobs = [(cat, term + " clearance", nao)
            for cat, terms in CATS.items() for term in terms for nao in PAGES]
    say("harvest: %d search pages" % len(jobs))

    def fetch(job):
        cat, q, nao = job
        url = ("https://www.homedepot.com/s/" + urllib.parse.quote(q)
               + "?Nao=%d&hv=%d" % (nao, int(time.time())))
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=35) as r:
                html = r.read().decode("utf-8", "replace")
        except Exception:
            return []
        out = []
        for m in LINK.finditer(html):
            name = re.sub(r"\s+", " ",
                          urllib.parse.unquote(m.group(1)).replace("-", " ")).strip()
            if len(name) >= 6:
                out.append((m.group(2), name[:78], cat))
        return out

    cand = {}
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for res in ex.map(fetch, jobs):
            for pid, name, cat in res:
                cand.setdefault(pid, [name, cat])

    pool = [[v[0], k, v[1]] for k, v in cand.items()]
    # A harvest that collapses means Home Depot changed their markup, not
    # that the catalogue shrank. Keep the pool we already trust.
    old = load_pool()
    if old and len(pool) < len(old) * 0.6:
        say("harvest: only %d products vs %d already on file -- keeping the old pool"
            % (len(pool), len(old)))
        return old
    json.dump({"pool": pool}, open(POOL, "w"), separators=(",", ":"))
    say("harvest: %d unique products" % len(pool))
    return pool


def load_pool():
    try:
        return json.load(open(POOL))["pool"]
    except Exception:
        return []


# ------------------------------------------------------------------- scan

Q = ('query q($ids: [String!]!) { products(itemIds: $ids) { itemId '
     'identifiers { productLabel canonicalUrl upc storeSkuNumber modelNumber } '
     'availabilityType { type } '
     'info { replacementOMSID } '
     'pricing(storeId: "%s", isBrandPricingPolicyCompliant: false) '
     '{ value clearance { value dollarOff percentageOff } } '
     'fulfillment(storeId: "%s") { fulfillmentOptions { type fulfillable '
     'services { locations { locationId isAnchor inventory { quantity } } } } } } }')


def call(ids, sid, tries=3):
    body = json.dumps({"operationName": "q", "variables": {"ids": ids},
                       "query": Q % (sid, sid)}).encode()
    req = urllib.request.Request(API, data=body, headers={
        "Content-Type": "application/json",
        "x-experience-name": "general-merchandise", "User-Agent": UA})
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
                return [p for p in ((d.get("data") or {}).get("products") or []) if p]
        except Exception:
            if attempt == tries - 1:
                return []
            time.sleep(1.0 + attempt)
    return []


def row(p, sid, meta):
    pr = p.get("pricing") or {}
    cl = pr.get("clearance")
    if not cl or cl.get("value") is None:
        return None

    # Home Depot returns several pickup locations: this store plus nearby
    # ones that could fill the order. Only the anchor is ours -- taking any
    # other reports a neighbour's pile as stock on the shelf you're standing at.
    qty = None
    for o in ((p.get("fulfillment") or {}).get("fulfillmentOptions") or []):
        if o.get("type") != "pickup":
            continue
        for s in (o.get("services") or []):
            for loc in (s.get("locations") or []):
                if not (loc.get("isAnchor") or str(loc.get("locationId")) == sid):
                    continue
                inv = loc.get("inventory") or {}
                if inv.get("quantity") is not None:
                    qty = inv["quantity"]

    store_only = 1 if (p.get("availabilityType") or {}).get("type") == "Store Only" else 0
    # a designated successor SKU means the item is being phased out; it runs
    # ~10x richer among clearance items than among everything else
    superseded = 1 if (p.get("info") or {}).get("replacementOMSID") else 0

    ident = p.get("identifiers") or {}
    m = meta.get(p["itemId"], ["", ""])
    value = float(cl["value"])
    # the list price the discount is actually computed from; pricing.value can
    # itself be a promotion, so prefer clearance value + dollarOff
    off = cl.get("dollarOff")
    was = round(value + float(off), 2) if off is not None else round(float(pr.get("value") or 0), 2)

    return [(ident.get("productLabel") or m[0])[:78], p["itemId"], m[1],
            round(value, 2), was, round(float(cl.get("percentageOff") or 0)),
            sid, qty, store_only, ident.get("canonicalUrl") or "",
            ident.get("upc") or "", ident.get("storeSkuNumber") or "",
            ident.get("modelNumber") or "", superseded]


def scan_store(sid, pool, meta):
    ids = [p[1] for p in pool]
    chunks = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]

    def work(chunk):
        return [r for r in (row(p, sid, meta) for p in call(chunk, sid)) if r]

    hits = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for got in ex.map(work, chunks):
            hits += got
    return hits


def scan():
    pool = load_pool()
    if not pool:
        die("no pool on file -- run the harvest stage first")
    meta = {p[1]: [p[0], p[2]] for p in pool}
    stores = json.load(open(STORES))
    say("scan: %d products x %d stores" % (len(pool), len(stores)))

    hits, t0 = [], time.time()
    for sid, name in stores:
        s0 = time.time()
        got = scan_store(sid, pool, meta)
        hits += got
        say("  %-16s %4d hits in %5.1fs" % (name, len(got), time.time() - s0))

    if not hits:
        die("scan came back empty -- leaving the list already on file alone")
    prev = previous_hits()
    if prev and len(hits) < len(prev) * 0.4:
        die("scan found %d hits against %d last time; that is a broken run, "
            "not an empty store -- leaving the list alone" % (len(hits), len(prev)))

    hits.sort(key=lambda r: -r[5])
    json.dump({"harvested": time.strftime("%Y-%m-%d"), "pool_n": len(pool),
               "stores_n": len(stores), "hits": hits},
              open(OUT, "w"), separators=(",", ":"))
    say("scan: %d hits across %d stores in %.1f min"
        % (len(hits), len(stores), (time.time() - t0) / 60))


def previous_hits():
    try:
        return json.load(open(OUT))["hits"]
    except Exception:
        return []


# ------------------------------------------------------------------ plumbing

def say(msg):
    print(msg, flush=True)


def die(msg):
    print("sweep: " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


if __name__ == "__main__":
    stages = sys.argv[1:] or ["scan"]
    for s in stages:
        if s == "harvest":
            harvest()
        elif s == "scan":
            scan()
        else:
            die("unknown stage %r (want harvest and/or scan)" % s)
