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

  scan       price everything on the hot list at EVERY store, plus the
             cold remainder of the pool at ONE rotating store, and write
             pennyrun/clearance.json. Nightly. The split is measured: the
             hot list runs 91% hits, the cold remainder 0% -- pricing it
             everywhere was 86% of the requests for none of the results.

Discovery is broad and shallow, verification is narrow and deep, and
anything ever found on clearance keeps getting checked until it stops
showing up. Run them in that order:

  python3 -m tools.sweep discover scan
  python3 -m tools.sweep harvest discover scan
"""

import datetime
import json
import os
import re
import sys
import time
import urllib.parse
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# `python3 tools/sweep.py ...` puts tools/ on sys.path, not the repo root, so
# `from tools import hdclient` can't resolve -- Python never adds the parent
# of the script's own directory. `python3 -m tools.sweep` doesn't have this
# problem (that's the documented invocation below), but the direct form is
# what every existing caller -- the workflow, the systemd units, muscle
# memory -- actually runs, so make it work too rather than break them all.
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import hdclient

# On a server the checkout has to stay pristine or every deploy turns into
# a merge conflict with last night's sweep. Point PENNYRUN_DATA somewhere
# writable outside git and the state files follow; the repo copies are the
# seed, read once if the data directory is still empty.
DATA = os.environ.get("PENNYRUN_DATA") or HERE
POOL = os.path.join(DATA, "pool.json")
HOT = os.path.join(DATA, "hot.json")
CURSOR = os.path.join(DATA, "cursor.json")
PRICES = os.path.join(DATA, "prices.json")
SEED = HERE  # shipped starting state, never written to

# Likewise the app's list: served from the data directory on a server, and
# written straight into the site folder when running from a checkout.
OUT = os.environ.get("PENNYRUN_OUT") or os.path.join(ROOT, "pennyrun", "clearance.json")

STORES = os.environ.get("PENNYRUN_STORES") or os.path.join(HERE, "stores.json")

# tools/hdclient.py is the only module that hardcodes a Home Depot host --
# everything here that needs one asks it for the host rather than repeating it.
SITEMAP = hdclient.SITEMAP_HOST + "/sitemap/P/PIPs.xml"

BATCH = hdclient.BATCH  # the cap the products() query enforces on itemIds
WORKERS = 20        # measured clean to 40; 20 leaves headroom and stays polite
PAGES = [0, 24, 48, 72]

# A discover() slice where at least this fraction of chunks came back refused
# or unreachable isn't a real result -- it's Home Depot declining to answer,
# shaped like an empty catalogue. Below this, a handful of bad chunks in a
# slice of thousands is normal and the run is still trusted. At or above it,
# discover() dies before writing HOT or advancing CURSOR: a slice that was
# mostly never priced must be priced again, not marked done and skipped for
# weeks. (scan()'s analogous guard compares total hits to the last run's;
# discover() has no "last run" to compare a single slice against, so this
# looks at the run's own chunks instead.)
DISCOVER_ABORT_THRESHOLD = 0.5

# scan()'s circuit breaker. A store that comes back this refused is not a
# store with nothing on clearance -- it is Home Depot declining to talk to
# this address, and every further request deepens that. Measured on the run
# of 2026-08-07: the refusal rate climbed 7% -> 8% -> 7% -> 10% across four
# stores and then went to 100%, and the scan spent 2,348 more requests --
# half the night's traffic -- on four stores that had already stopped
# answering. Stop at the wall instead of leaning on it.
#
# 0.25, not the 0.5 discover() uses: discover's unit is one slice of tens of
# thousands, a store here is ~81 chunks, and the wall is a cumulative request
# budget so it lands mid-store. Exhaustion at chunk 42 of 81 reads 48% and
# would not trip at 0.5 -- the next store then reads 100% and trips anyway,
# having spent a whole store's requests to learn it. 0.25 still leaves 2.5x
# headroom over the highest rate ever measured on a healthy run (10%).
SCAN_ABORT_THRESHOLD = 0.25

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

def call(ids, sid, lite=False):
    """Kept for call-site compatibility. Refusals now surface instead of
    turning into an empty list that reads as 'nothing on clearance'."""
    return hdclient.products(ids, sid, lite=lite)


def get(url, timeout=60, expect_xml=True):
    """expect_xml=True (the default, used for the sitemap in discover()) makes
    a 200 whose body isn't actually XML -- an Akamai challenge page included
    -- raise instead of being parsed as an empty sitemap. harvest() fetches
    plain HTML search pages on purpose and passes expect_xml=False."""
    return hdclient.sitemap(url, timeout=timeout, expect_xml=expect_xml)


# "They answered and said no" and "we never got an answer" call for different
# responses at 5am -- keep them apart rather than folding both into one count.
BatchRun = namedtuple(
    "BatchRun",
    "out refused unreachable chunks priced first_refused first_unreachable")


def in_batches(ids, fn):
    """Run fn over every batch of ids concurrently and flatten the results.

    Real sitemap slices can repeat an id across shards, and hdclient.products()
    rejects a whole batch outright if it contains a duplicate -- so ids are
    de-duplicated here before chunking, once, up front.

    A batch that gets refused or is simply unreachable no longer disappears
    into an empty list the way the old call() made it look: both are counted
    separately (plus the first message seen for each) and returned alongside
    the hits, so a fully-refused run is visibly a wall, not a night with
    nothing on clearance -- and "they said no" is never confused with "we
    never got an answer". One bad chunk still doesn't abort the rest -- these
    can be thousands of chunks and Home Depot's block is applied per
    connection, not per id, so pressing on and reporting the counts is more
    useful than stopping at the first one. Callers that need to distrust a
    mostly-bad run decide that themselves from the counts returned here.
    """
    seen, deduped = set(), []
    for i in ids:
        if i not in seen:
            seen.add(i)
            deduped.append(i)
    chunks = [deduped[i:i + BATCH] for i in range(0, len(deduped), BATCH)]

    def safe(chunk):
        try:
            return "ok", fn(chunk), len(chunk)
        except hdclient.Refused as e:
            return "refused", str(e), len(chunk)
        except hdclient.Unreachable as e:
            return "unreachable", str(e), len(chunk)

    out, refused, unreachable, priced = [], 0, 0, 0
    first_refused, first_unreachable = None, None
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        for kind, payload, n in ex.map(safe, chunks):
            if kind == "ok":
                out += payload
                priced += n
            elif kind == "refused":
                refused += 1
                first_refused = first_refused or payload
            else:
                unreachable += 1
                first_unreachable = first_unreachable or payload

    return BatchRun(out, refused, unreachable, len(chunks), priced,
                     first_refused, first_unreachable)


# ---------------------------------------------------------------- harvest

def harvest():
    jobs = [(cat, term + " clearance", nao)
            for cat, terms in CATS.items() for term in terms for nao in PAGES]
    say("harvest: %d search pages" % len(jobs))

    def fetch(job):
        cat, q, nao = job
        url = (hdclient.SITEMAP_HOST + "/s/" + urllib.parse.quote(q)
               + "?Nao=%d&hv=%d" % (nao, int(time.time())))
        try:
            html = get(url, timeout=35, expect_xml=False)
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

    # A shard fetch can itself be challenged even when the index just above
    # answered fine -- catch that here rather than let it crash the run or,
    # worse, let a challenge page's empty <loc> count read as an empty shard.
    try:
        shard_body = get(shards[shard], timeout=120)
    except Exception as e:
        die("discover: shard %d was refused (%s). Home Depot challenged this "
            "request -- the catalogue slice is not actually empty." % (shard, e))
    urls = LOC.findall(shard_body)
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
        for p in call(chunk, sid, lite=True):
            label = (p.get("identifiers") or {}).get("productLabel") or ""
            if label:
                names[p["itemId"]] = (label, crumb(p))
            cl = (p.get("pricing") or {}).get("clearance")
            if cl and cl.get("value") is not None:
                found.append(p["itemId"])
        return found

    t0 = time.time()
    run = in_batches(window, work)
    bad = run.refused + run.unreachable

    # Gate on refusal BEFORE any state write. A total (or near-total) refusal
    # must not age hot-list entries out and must not advance the cursor past
    # a slice that was never actually priced -- at the real SLICE size that's
    # tens of thousands of products burned for weeks, not an empty night.
    if run.chunks and bad / run.chunks >= DISCOVER_ABORT_THRESHOLD:
        detail = ""
        if run.first_refused:
            detail += "\n  refused: %s" % run.first_refused
        if run.first_unreachable:
            detail += "\n  unreachable: %s" % run.first_unreachable
        die("discover: %d/%d chunks refused, %d/%d unreachable (%.0f%% of the slice, at or "
            "above the %.0f%% abort threshold) -- Home Depot is blocking this run, not "
            "returning an empty slice. Leaving the hot list and cursor untouched.%s"
            % (run.refused, run.chunks, run.unreachable, run.chunks,
               100 * bad / run.chunks, 100 * DISCOVER_ABORT_THRESHOLD, detail))

    if bad:
        say("discover: %d of %d chunks refused, %d unreachable -- this run is partial, not empty"
            % (run.refused, run.chunks, run.unreachable))
    say("discover: %d on clearance out of %d priced in %.1f min"
        % (len(run.out), run.priced, (time.time() - t0) / 60))

    hot = read(HOT, {})
    for pid in run.out:
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
    # Merge, don't replace. A literal here silently dropped scan()'s
    # `scan_store` field every night -- and since discover runs first in both
    # collect.sh and the systemd timers, scan's cold-pool rotation was reset
    # to 0 before every run, so seven of eight stores never had their cold
    # pool priced at all. Anything else that lands on this cursor later would
    # have been eaten the same way.
    write(CURSOR, dict(cur, shard=shard, offset=nxt,
                       store=(cur.get("store", 0) + 1) % len(stores)))
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
    prev = before.get(p["itemId"] + "@" + sid)
    dropped = round(prev, 2) if prev is not None and prev > value + 0.004 else None

    return [(ident.get("productLabel") or m[0])[:78], p["itemId"], m[1],
            round(value, 2), was, round(float(cl.get("percentageOff") or 0)),
            sid, qty, store_only, ident.get("canonicalUrl") or "",
            ident.get("upc") or "", ident.get("storeSkuNumber") or "",
            ident.get("modelNumber") or "", superseded, dropped]


def chunks_for(n):
    """How many API requests pricing n products takes."""
    return -(-n // BATCH)


def price_at(sid, name, ids, meta, before, note=""):
    """Price `ids` at one store. Returns (rows, refusal_rate)."""
    s0 = time.time()
    run = in_batches(ids, lambda chunk, s=sid:
                     [r for r in (row(p, s, meta, before) for p in call(chunk, s)) if r])
    # Refused and unreachable are deliberately NOT summed. "They said no" is
    # evidence about this address; "we never got an answer" is evidence about
    # our own network, and a thirty-second DHCP blip must not be read as Home
    # Depot blocking us and abort the night. hdclient defines two exception
    # types for exactly this reason; collapsing them here would throw that
    # away at the one place it decides something.
    refused_rate = run.refused / run.chunks if run.chunks else 0.0
    unreachable_rate = run.unreachable / run.chunks if run.chunks else 0.0
    if run.refused or run.unreachable:
        say("  %-16s %5d hits in %5.1fs  (%d/%d chunks refused, %d unreachable)%s"
            % (name, len(run.out), time.time() - s0, run.refused, run.chunks,
               run.unreachable, note))
    else:
        say("  %-16s %5d hits in %5.1fs%s"
            % (name, len(run.out), time.time() - s0, note))
    return run.out, refused_rate, unreachable_rate


def scan():
    pool = load_pool()
    hot = read(HOT, {})
    meta = {p[1]: [p[0], p[2]] for p in pool}
    for pid, v in hot.items():
        meta.setdefault(pid, [v.get("name", ""), v.get("cat", "")])

    # Two tiers, because they earn their requests at wildly different rates.
    # Measured 2026-08-07 across the four stores that answered before the
    # address was cut off:
    #
    #     hot list    1,288 items -> 1,173 on clearance somewhere  (91.1%)
    #     cold pool   8,097 items ->     0 on clearance anywhere   ( 0.00%)
    #
    # Pricing the cold pool at every store spent 86% of the run's requests to
    # find nothing at all. That is not bad luck: anything that goes on
    # clearance is promoted to the hot list and priced everywhere from then
    # on, so what is left in the cold pool has already been mined. It is
    # worth re-checking slowly, not eight times a night.
    hot_ids = sorted(hot)
    cold_ids = sorted({p[1] for p in pool} - set(hot))
    if not hot_ids and not cold_ids:
        die("nothing to scan -- run the harvest stage first")

    stores = json.load(open(STORES))
    if not stores:
        die("no stores configured -- %s is empty" % STORES)
    before = previous_prices()
    cur = read(CURSOR, {})
    turn = cur.get("scan_store", 0) % len(stores)

    # Tiering only saves anything when the hot list is the cheap tier. On a
    # fresh install, or after a long block ages the hot list out (HOT_DAYS),
    # hot is empty or tiny and tiering degenerates into "price the cold pool
    # at one store" -- a one-store scan that also never bootstraps a hot list
    # to price everywhere later. There is no budget argument for tiering when
    # the expensive tier is the only tier, so fall back to the flat sweep.
    tiered = len(hot_ids) >= max(1, len(cold_ids) // 20)
    if not tiered:
        hot_ids, cold_ids = sorted(set(hot_ids) | set(cold_ids)), []
        say("scan: hot list is too small to tier (%d) -- pricing everything "
            "at every store to rebuild it" % len(hot))

    planned = chunks_for(len(hot_ids)) * len(stores) + chunks_for(len(cold_ids))
    flat = chunks_for(len(hot_ids) + len(cold_ids)) * len(stores)
    say("scan: %d hot x %d stores, then %d cold at %s only"
        % (len(hot_ids), len(stores), len(cold_ids), stores[turn][1]))
    say("scan: ~%d requests (pricing everything everywhere would be ~%d)"
        % (planned, flat))

    hits, t0, blocked = [], time.time(), False

    # Tier 1: the hot list at every store. This is the tier that produces the
    # list, so it runs first -- if the address gets cut off mid-run, the
    # speculative half is what gets lost, not the useful half.
    for i, (sid, name) in enumerate(stores):
        got, rate, _unreach = price_at(sid, name, hot_ids, meta, before)
        hits += got
        if rate >= SCAN_ABORT_THRESHOLD:
            skipped = chunks_for(len(hot_ids)) * (len(stores) - i - 1) \
                + chunks_for(len(cold_ids))
            say("scan: %s refused %.0f%% of its chunks. That is this address "
                "being blocked, not stores with nothing on clearance -- "
                "stopping rather than firing %d more requests that cannot "
                "succeed and only deepen it." % (name, 100 * rate, skipped))
            blocked = True
            break

    # Tier 2: the cold pool at one rotating store, so every product still gets
    # re-checked -- just over N nights instead of every night.
    advance = False
    if cold_ids and not blocked:
        sid, name = stores[turn]
        got, rate, _unreach = price_at(sid, name, cold_ids, meta, before, "  [cold pool]")
        hits += got
        if rate >= SCAN_ABORT_THRESHOLD:
            say("scan: the cold sweep at %s was refused; leaving the rotation "
                "where it is so this slice is retried, not skipped." % name)
        else:
            advance = True

    # A run the circuit breaker stopped covered only some of the stores. It
    # must not overwrite a fuller list: the 40%-of-last-time rail compares
    # against a total that may ITSELF have been truncated, so without this a
    # blocked night ratchets the list down -- 8 stores, then 4, then 2, then
    # 1, with the rail never firing because each drop is under 60%. And
    # save_prices() replaces prices.json wholesale, so the stores that were
    # skipped lose their baseline and the "price dropped overnight" signal --
    # the one the app leads with -- goes null for them on the next good night.
    if blocked:
        die("scan stopped early: only %d of %d stores were priced before this "
            "address started being refused. Leaving the list and the price "
            "ledger alone rather than replacing a full night with a partial "
            "one." % (len({r[6] for r in hits}), len(stores)))

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

    # Advance the cold rotation only now, past the rails. A run the rails
    # discard has not really priced its slice, and marking it done would skip
    # that slice for a full rotation.
    if advance:
        write(CURSOR, dict(read(CURSOR, {}), scan_store=(turn + 1) % len(stores)),
              small=True)

    shipped = pick(hits)
    if len(hits) > len(shipped):
        say("scan: shipping %d of %d hits, spread across store and department"
            % (len(shipped), len(hits)))
    # stores_n is how many stores this run actually priced, not how many are
    # configured -- a run stopped by the circuit breaker covered fewer, and
    # the app should not be told otherwise.
    covered = len({r[6] for r in hits})
    write(OUT, {"harvested": TODAY, "pool_n": len(hot_ids) + len(cold_ids),
                "stores_n": covered, "total_hits": len(hits), "hits": shipped},
          small=True)
    save_prices(hits)
    # No "stopped early" case to report here: a run the breaker stopped die()s
    # above rather than reaching a write, so anything getting this far covered
    # every store it set out to.
    say("scan: %d hits across %d of %d stores in %.1f min (%d dropped since the last sweep)"
        % (len(hits), covered, len(stores), (time.time() - t0) / 60,
           sum(1 for r in hits if r[14] is not None)))

    # clearance.json is already written above -- collection succeeding and
    # delivery to the droplet succeeding are different events, so an upload
    # failure here must never look like the scan itself failed. Only
    # attempted when both are set: a home box with no PENNYRUN_API just
    # writes clearance.json and stops, same as before this existed.
    base = os.environ.get("PENNYRUN_API")
    token = os.environ.get("PENNYRUN_INGEST_TOKEN")
    if base and token:
        from tools import upload
        try:
            got = upload.send(hits, base, token)
        except upload.UploadError as e:
            # Whatever chunks got through before the failure are already
            # permanent (observation is append-only) -- say so, so a human
            # re-running the scan knows those rows will be resent (and
            # duplicated) rather than assuming nothing landed.
            say("upload stopped partway: %d accepted, %d rejected before it failed (%s) "
                "-- clearance.json was still written"
                % (e.partial["accepted"], e.partial["rejected"], e.cause))
        except Exception as e:
            say("upload failed: %s -- clearance.json was still written" % e)
        else:
            say("uploaded: %d accepted, %d rejected" % (got["accepted"], got["rejected"]))


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

    # A price that fell last night is the single strongest lead, and the
    # round-robin below can crowd one out if its department is busy. Give
    # drops first claim on up to half the slots, then deal out the rest.
    fell = sorted((r for r in hits if r[14] is not None), key=order)[:SHIP // 2]
    claimed = set(id(r) for r in fell)
    rest = [r for r in hits if id(r) not in claimed]

    groups = {}
    for r in rest:
        groups.setdefault((r[6], r[2] or "?"), []).append(r)
    for g in groups.values():
        g.sort(key=order)

    out, rank = list(fell), 0
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
    """{"itemId@storeId": clearance price} from the sweep before this one,
    the same key shape save_prices() writes.

    Kept in its own ledger rather than read back out of clearance.json,
    because that file only ships the deepest few thousand rows and a price
    that fell out of the top of the list is exactly the one worth noticing
    when it drops again."""
    out = {}
    for key, price in read(PRICES, {}).items():
        try:
            out[key] = float(price)
        except Exception:
            pass
    return out


def save_prices(hits):
    write(PRICES, {r[1] + "@" + r[6]: r[3] for r in hits}, small=True)


# --------------------------------------------------------------- plumbing

def load_pool():
    return read(POOL, {}).get("pool", [])


def read(path, default):
    """Read a state file, falling back to the copy shipped in the repo the
       first time a server runs with an empty data directory."""
    for candidate in (path, os.path.join(SEED, os.path.basename(path))):
        try:
            return json.load(open(candidate))
        except Exception:
            continue
    return default


def write(path, data, small=False):
    d = os.path.dirname(path)
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, separators=(",", ":") if small else None,
                  indent=None if small else 0)
    # the web server may read clearance.json at any moment; swap it in whole
    # rather than let a phone catch a half-written file
    os.replace(tmp, path)


def say(msg):
    print(msg, flush=True)


def die(msg):
    print("sweep: " + msg, file=sys.stderr, flush=True)
    sys.exit(1)


def probe():
    """Say exactly what each host does when we knock, with no retries and no
       swallowed exceptions. The scan hides failures behind empty results on
       purpose -- this is the stage that refuses to."""
    from tools import checkhost
    if checkhost.main() != 0:
        die("this machine cannot run the sweep")

    # checkhost's exit code is 0 as soon as the pricing gateway answers --
    # scan doesn't need anything else. But discover() also needs the sitemap
    # host, which checkhost.verdict() never looks at, so a blocked sitemap
    # passes probe silently otherwise. Warn, don't die: scan must still run.
    sitemap_state = hdclient.probe().get("sitemap", "")
    if sitemap_state and sitemap_state != "ok":
        say("WARNING: sitemap host is %s -- discover() will not be able to walk "
            "the catalogue from this machine right now. scan() is unaffected."
            % sitemap_state)


STAGES = {"harvest": harvest, "discover": discover, "scan": scan, "probe": probe}

if __name__ == "__main__":
    wanted = sys.argv[1:] or ["scan"]
    for name in wanted:
        if name not in STAGES:
            die("unknown stage %r (want %s)" % (name, ", ".join(STAGES)))
    for name in wanted:
        STAGES[name]()
