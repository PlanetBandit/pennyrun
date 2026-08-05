#!/usr/bin/env python3
"""Rebuild pennyrun/clearance.json from Home Depot's own pricing API.

Home Depot has no browsable list of in-store clearance. Ask their API for
a price with no store attached and it returns null -- a price only exists
once the request is localised to one building, and the deep markdowns
belong to that building alone. So the only way to get a list is to ask,
product by product, store by store.

Their sitemap carries about 4.2 million products, which is far more than
can be priced at eight stores in a night. The work is split so that
discovery is cheap and verification is thorough:

  harvest    scrape product ids out of Home Depot's clearance searches
             into tools/pool.json. Small, biased towards markdowns, and
             worth sweeping in full every night. Weekly.

  discover   walk the sitemap a slice at a time, pricing each product at
             ONE rotating store. Anything that comes back on clearance is
             promoted to tools/hot.json. Costs an eighth of a full sweep
             per product and works through the whole catalogue in a few
             weeks. Nightly.

  scan       price the pool plus everything on the hot list at EVERY
             store, and write pennyrun/clearance.json. Nightly.

Discovery is broad and shallow, verification is narrow and deep, and
anything ever found on clearance keeps getting checked until it stops
showing up. Run them in that order:

  python3 tools/sweep.py discover scan
  python3 tools/sweep.py harvest discover scan
"""

import datetime
import gzip
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
HOT = os.path.join(HERE, "hot.json")
CURSOR = os.path.join(HERE, "cursor.json")
PRICES = os.path.join(HERE, "prices.json")
STORES = os.path.join(HERE, "stores.json")
OUT = os.path.join(ROOT, "pennyrun", "clearance.json")

API = "https://apionline.homedepot.com/federation-gateway/graphql"
SITEMAP = "https://www.homedepot.com/sitemap/P/PIPs.xml"
UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
      "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1")

BATCH = 16          # the cap the products() query enforces on itemIds
WORKERS = 20        # measured clean to 40; 20 leaves headroom and stays polite
PAGES = [0, 24, 48, 72]

# How much of the catalogue to price per discover run. Measured at roughly
# 330 products/sec here, so 60k is a few minutes locally and under fifteen on
# a hosted runner. Raise it if the nightly job has room.
SLICE = int(os.environ.get("SWEEP_SLICE", "60000"))

# Rows shipped in clearance.json, deepest cut first. The phone re-fetches this
# on every open, so it has to stay small; anything past this is a shallower
# markdown than 3,000 other things at the same stores.
SHIP = int(os.environ.get("SWEEP_SHIP", "3000"))

# Drop a hot-list entry that has not shown a clearance price in this long.
HOT_DAYS = 45

TODAY = datetime.date.today().isoformat()

# Every aisle worth walking. The terms are deliberately mundane -- the
# deepest markdowns sit on things nobody thinks to look up.
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
LOC = re.compile(r"<loc>([^<]+)</loc>")


# --------------------------------------------------------------- the API

Q = ('query q($ids: [String!]!) { products(itemIds: $ids) { itemId '
     'identifiers { productLabel canonicalUrl upc storeSkuNumber modelNumber } '
     'availabilityType { type } '
     'info { replacementOMSID } '
     'pricing(storeId: "%s", isBrandPricingPolicyCompliant: false) '
     '{ value clearance { value dollarOff percentageOff } } '
     'fulfillment(storeId: "%s") { fulfillmentOptions { type fulfillable '
     'services { locations { locationId isAnchor inventory { quantity } } } } } } }')

# Discovery only needs to know whether a clearance price exists at all.
QLITE = ('query q($ids: [String!]!) { products(itemIds: $ids) { itemId '
         'identifiers { productLabel } taxonomy { breadCrumbs { label } } '
         'pricing(storeId: "%s", isBrandPricingPolicyCompliant: false) '
         '{ clearance { value } } } }')


def call(query, ids, sid, tries=3):
    body = json.dumps({"operationName": "q", "variables": {"ids": ids},
                       "query": query % ((sid, sid) if query is Q else sid)}).encode()
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


def in_batches(ids, fn):
    """Run fn over every batch of ids concurrently and flatten the results."""
    chunks = [ids[i:i + BATCH] for i in range(0, len(ids), BATCH)]
    out = []
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for got in ex.map(fn, chunks):
            out += got
    return out


def get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or url.endswith(".gz"):
            try:
                data = gzip.decompress(data)
            except Exception:
                pass
        return data.decode("utf-8", "replace")


# ---------------------------------------------------------------- harvest

def harvest():
    jobs = [(cat, term + " clearance", nao)
            for cat, terms in CATS.items() for term in terms for nao in PAGES]
    say("harvest: %d search pages" % len(jobs))

    def fetch(job):
        cat, q, nao = job
        url = ("https://www.homedepot.com/s/" + urllib.parse.quote(q)
               + "?Nao=%d&hv=%d" % (nao, int(time.time())))
        try:
            html = get(url, timeout=35)
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
    with ThreadPoolExecutor(max_workers=6) as ex:
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
        return
    write(POOL, {"pool": pool})
    say("harvest: %d unique products" % len(pool))


# --------------------------------------------------------------- discover

def discover():
    """Price one slice of Home Depot's own sitemap at one store, and promote
       whatever comes back on clearance onto the hot list."""
    stores = json.load(open(STORES))
    cur = read(CURSOR, {"shard": 0, "offset": 0, "store": 0})
    sid, sname = stores[cur.get("store", 0) % len(stores)]

    # www.homedepot.com sits behind a bot filter that some datacentre ranges
    # never get past, while apionline.homedepot.com answers fine from the same
    # host. Say so loudly rather than quietly discovering nothing every night.
    try:
        body = get(SITEMAP)
    except Exception as e:
        die("discover: could not reach the sitemap (%s). The pricing API and "
            "www.homedepot.com are different hosts -- this one is blocking us." % e)
    shards = LOC.findall(body)
    if not shards:
        die("discover: the sitemap answered with no <loc> entries in %d bytes. "
            "That is a bot-block page, not an empty catalogue." % len(body))

    shard = cur.get("shard", 0) % len(shards)
    offset = cur.get("offset", 0)

    urls = LOC.findall(get(shards[shard], timeout=120))
    ids = [m.group(1) for m in
           (re.search(r"/(\d{6,})/?$", u) for u in urls) if m]
    if not ids:
        die("discover: shard %d listed %d urls and no product ids" % (shard, len(urls)))
    window = ids[offset:offset + SLICE]
    if not window:
        window, shard, offset = ids[:SLICE], shard, 0

    names = {}
    say("discover: shard %d/%d offset %d -- %d products at %s"
        % (shard, len(shards), offset, len(window), sname))

    def work(chunk):
        found = []
        for p in call(QLITE, chunk, sid):
            label = (p.get("identifiers") or {}).get("productLabel") or ""
            if label:
                names[p["itemId"]] = (label, crumb(p))
            cl = (p.get("pricing") or {}).get("clearance")
            if cl and cl.get("value") is not None:
                found.append(p["itemId"])
        return found

    t0 = time.time()
    found = in_batches(window, work)
    say("discover: %d on clearance out of %d priced in %.1f min"
        % (len(found), len(window), (time.time() - t0) / 60))

    hot = read(HOT, {})
    for pid in found:
        label, cat = names.get(pid, ("", ""))
        prev = hot.get(pid) or {}
        hot[pid] = {"name": label or prev.get("name", ""),
                    "cat": cat or prev.get("cat", ""), "seen": TODAY}
    hot = age_out(hot)
    write(HOT, hot)

    # advance past what we just priced, rolling onto the next shard and the
    # next store so discovery is spread evenly rather than always store one
    nxt = offset + len(window)
    if nxt >= len(ids):
        nxt, shard = 0, (shard + 1) % len(shards)
    write(CURSOR, {"shard": shard, "offset": nxt,
                   "store": (cur.get("store", 0) + 1) % len(stores)})
    say("discover: hot list now %d products" % len(hot))


def crumb(p):
    """Top breadcrumb is Home Depot's own department name, which is what
       the category tile in the app is trying to say."""
    bc = ((p.get("taxonomy") or {}).get("breadCrumbs") or [])
    labels = [b.get("label") for b in bc if b.get("label")]
    return labels[0][:24] if labels else ""


def age_out(hot):
    cutoff = (datetime.date.today() - datetime.timedelta(days=HOT_DAYS)).isoformat()
    return {k: v for k, v in hot.items() if (v.get("seen") or "") >= cutoff}


# ------------------------------------------------------------------- scan

def row(p, sid, meta, before):
    pr = p.get("pricing") or {}
    cl = pr.get("clearance")
    if not cl or cl.get("value") is None:
        return None

    # Home Depot returns several pickup locations: this store plus nearby
    # ones that could fill the order. Only the anchor is ours -- taking any
    # other reports a neighbour's pile as stock on the shelf you're at.
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
    # itself be a promotion, which makes the cut read smaller than it is
    off = cl.get("dollarOff")
    was = round(value + float(off), 2) if off is not None else round(float(pr.get("value") or 0), 2)

    # markdowns cascade, so what it cost yesterday is the strongest lead there
    # is: a price that moved last night is a price still on its way down
    prev = before.get((p["itemId"], sid))
    dropped = round(prev, 2) if prev is not None and prev > value + 0.004 else None

    return [(ident.get("productLabel") or m[0])[:78], p["itemId"], m[1],
            round(value, 2), was, round(float(cl.get("percentageOff") or 0)),
            sid, qty, store_only, ident.get("canonicalUrl") or "",
            ident.get("upc") or "", ident.get("storeSkuNumber") or "",
            ident.get("modelNumber") or "", superseded, dropped]


def scan():
    pool = load_pool()
    hot = read(HOT, {})
    meta = {p[1]: [p[0], p[2]] for p in pool}
    for pid, v in hot.items():
        meta.setdefault(pid, [v.get("name", ""), v.get("cat", "")])
    ids = sorted(set([p[1] for p in pool]) | set(hot))
    if not ids:
        die("nothing to scan -- run the harvest stage first")

    stores = json.load(open(STORES))
    before = previous_prices()
    say("scan: %d products (%d harvested + %d on the hot list) x %d stores"
        % (len(ids), len(pool), len(hot), len(stores)))

    hits, t0 = [], time.time()
    for sid, name in stores:
        s0 = time.time()
        got = in_batches(ids, lambda chunk, s=sid:
                         [r for r in (row(p, s, meta, before) for p in call(Q, chunk, s)) if r])
        hits += got
        say("  %-16s %4d hits in %5.1fs" % (name, len(got), time.time() - s0))

    if not hits:
        die("scan came back empty -- leaving the list already on file alone")
    was = read(OUT, {}).get("total_hits") or len(previous_hits())
    if was and len(hits) < was * 0.4:
        die("scan found %d hits against %d last time; that is a broken run, "
            "not an empty store -- leaving the list alone" % (len(hits), was))

    # anything still on clearance belongs on the hot list, however it was found
    for r in hits:
        e = hot.get(r[1]) or {}
        hot[r[1]] = {"name": r[0], "cat": r[2] or e.get("cat", ""), "seen": TODAY}
    write(HOT, age_out(hot))

    shipped = pick(hits)
    if len(hits) > len(shipped):
        say("scan: shipping %d of %d hits, spread across store and department"
            % (len(shipped), len(hits)))
    write(OUT, {"harvested": TODAY, "pool_n": len(ids), "stores_n": len(stores),
                "total_hits": len(hits), "hits": shipped}, small=True)
    save_prices(hits)
    say("scan: %d hits across %d stores in %.1f min (%d dropped since the last sweep)"
        % (len(hits), len(stores), (time.time() - t0) / 60,
           sum(1 for r in hits if r[14] is not None)))


def pick(hits):
    """Choose the rows to ship.

    Taking the deepest SHIP rows sounds right and is useless in practice:
    discontinued paint colours are marked down 90%+ by the hundred, so a
    naive cut came back 98% paint and buried every other aisle. Deal the
    rows out round-robin by store and department instead -- best of each
    group, then the next best, and so on -- so the list reads like a walk
    through the building rather than one endcap.
    """
    # within a group: what moved last night first, then the deepest cut
    order = lambda r: (0 if r[14] is not None else 1, -r[5])
    groups = {}
    for r in hits:
        groups.setdefault((r[6], r[2] or "?"), []).append(r)
    for g in groups.values():
        g.sort(key=order)

    out, rank = [], 0
    keys = sorted(groups)
    while len(out) < SHIP:
        took = 0
        for k in keys:
            g = groups[k]
            if rank < len(g):
                out.append(g[rank])
                took += 1
                if len(out) >= SHIP:
                    break
        if not took:
            break
        rank += 1
    out.sort(key=order)
    return out


def previous_hits():
    try:
        return json.load(open(OUT))["hits"]
    except Exception:
        return []


def previous_prices():
    """{(itemId, storeId): clearance price} from the sweep before this one.

    Kept in its own ledger rather than read back out of clearance.json,
    because that file only ships the deepest few thousand rows and a price
    that fell out of the top of the list is exactly the one worth noticing
    when it drops again."""
    out = {}
    for key, price in read(PRICES, {}).items():
        pid, _, sid = key.partition("@")
        try:
            out[(pid, sid)] = float(price)
        except Exception:
            pass
    return out


def save_prices(hits):
    write(PRICES, {r[1] + "@" + r[6]: r[3] for r in hits}, small=True)


# --------------------------------------------------------------- plumbing

def load_pool():
    return read(POOL, {}).get("pool", [])


def read(path, default):
    try:
        return json.load(open(path))
    except Exception:
        return default


def write(path, data, small=False):
    json.dump(data, open(path, "w"), separators=(",", ":") if small else None,
              indent=None if small else 0)


def say(msg):
    print(msg, flush=True)


def die(msg):
    print("sweep: " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def probe():
    """Say exactly what each host does when we knock, with no retries and no
       swallowed exceptions. The scan hides failures behind empty results on
       purpose -- this is the stage that refuses to."""
    body = json.dumps({"operationName": "q", "variables": {"ids": ["205606416"]},
                       "query": QLITE % "2577"}).encode()
    checks = [
        ("pricing API", urllib.request.Request(API, data=body, headers={
            "Content-Type": "application/json",
            "x-experience-name": "general-merchandise", "User-Agent": UA})),
        ("sitemap", urllib.request.Request(SITEMAP, headers={"User-Agent": UA})),
        ("search page", urllib.request.Request(
            "https://www.homedepot.com/s/mulch%20clearance", headers={"User-Agent": UA})),
    ]
    # A datacentre IP and a missing header look identical from the outside:
    # both come back 206 with "Generic Errors API". Try the variants a real
    # app sends so the answer is measured rather than assumed.
    for label, extra in [
        ("+ origin/referer", {"Origin": "https://www.homedepot.com",
                              "Referer": "https://www.homedepot.com/"}),
        ("+ desktop UA", {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"}),
        ("+ apollo hdrs", {"apollographql-client-name": "general-merchandise",
                           "apollographql-client-version": "0.0.0",
                           "x-hd-dc": "origin", "Accept": "*/*"}),
    ]:
        h = {"Content-Type": "application/json",
             "x-experience-name": "general-merchandise", "User-Agent": UA}
        h.update(extra)
        try:
            with urllib.request.urlopen(
                    urllib.request.Request(API, data=body, headers=h), timeout=30) as r:
                say("  api %-16s HTTP %s  %s" % (label, r.status,
                    r.read(150).decode("utf-8", "replace").replace("\n", " ")))
        except Exception as e:
            say("  api %-16s FAILED  %s" % (label, e))

    bad = 0
    for name, req in checks:
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                text = r.read(400).decode("utf-8", "replace").replace("\n", " ")
                say("  %-12s HTTP %s  %s" % (name, r.status, text[:220]))
        except urllib.error.HTTPError as e:
            bad += 1
            say("  %-12s HTTP %s  %s" % (name, e.code,
                                         e.read(220).decode("utf-8", "replace").replace("\n", " ")))
        except Exception as e:
            bad += 1
            say("  %-12s FAILED  %s" % (name, e))
    if bad:
        die("%d of %d hosts refused us from this machine" % (bad, len(checks)))


STAGES = {"harvest": harvest, "discover": discover, "scan": scan, "probe": probe}

if __name__ == "__main__":
    wanted = sys.argv[1:] or ["scan"]
    for name in wanted:
        if name not in STAGES:
            die("unknown stage %r (want %s)" % (name, ", ".join(STAGES)))
    for name in wanted:
        STAGES[name]()
