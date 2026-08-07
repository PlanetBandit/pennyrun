# The sweep

`pennyrun/clearance.json` is not hand-written. It is rebuilt by
`sweep.py` from Home Depot's own pricing API, nightly, run directly on a
home box (see "Where it can run" below) rather than by a GitHub Actions
workflow — there used to be one, but every hosted runner is refused (see
the measurements below), and a self-hosted runner on a home network
turned out to be more moving parts than just running `tools.sweep` there
directly and uploading the result. `tools/upload.py` ships each run's
rows to the droplet's `POST /api/v1/discovery` when `PENNYRUN_API` and
`PENNYRUN_INGEST_TOKEN` are set — see `deploy/README.md` for the serving
side.

## Why it has to be built at all

Home Depot has no browsable list of in-store clearance. Ask their API
for a price with no store attached and it returns `null` — a price only
exists once the request is localised to one building, and the deep
markdowns belong to that building alone. The same bag of mulch:

```
no store       value=null    clearance=null
store 2504     value=3.33    clearance=$0.10  (97% off)
store 2577     value=3.33    clearance=none
```

So the only way to get a list is to ask, product by product, store by
store. Their sitemap carries about **4.2 million products**, which is far
more than can be priced at eight stores in a night. Hence three stages.

## The three stages

```bash
python3 -m tools.sweep harvest    # tools/pool.json          (weekly)
python3 -m tools.sweep discover   # tools/hot.json           (nightly)
python3 -m tools.sweep scan       # pennyrun/clearance.json  (nightly)
```

**harvest** scrapes product ids out of Home Depot's clearance search
pages. About 8,000 products. Worth keeping, but see the note below — it
is not the good source it looks like.

**discover** walks the sitemap a slice at a time, pricing each product at
**one rotating store**. Anything on clearance is promoted to
`tools/hot.json`. One store instead of eight buys eight times the breadth
for the same budget, and a promoted item gets priced everywhere from then
on. The cursor in `tools/cursor.json` remembers the shard, the offset and
which store's turn it is.

**scan** prices the **hot list at every store**, and the cold remainder of
the pool at **one rotating store**, then writes the app's list.

That split is measured, not assumed. On the run of 2026-08-07, across the
four stores that answered before this address was cut off:

| tier | items | on clearance somewhere |
|---|---|---|
| hot list | 1,288 | 1,173 (**91.1%**) |
| cold pool | 8,097 | 0 (**0.00%**) |

Pricing the cold pool everywhere was 86% of the night's requests for none of
the hits — and that is structural, not luck: anything that goes on clearance
is promoted to the hot list and priced everywhere from then on, so what is
left in the cold pool has already been mined. A full sweep went from 4,696
requests a night to 1,155, which matters because the wall this address hit
that night was measured at roughly **2,350 requests in one window**.

`scan` also stops when a store refuses a quarter of its chunks. That run kept
going through four fully-refused stores, spending 2,348 requests that could
not succeed and only deepened the block.

Discovery is broad and shallow, verification is narrow and deep, and
anything ever found on clearance keeps being checked until it stops
showing up (`HOT_DAYS`, 45).

No key, no cookie, no login. The API is public; it just won't quote a
price until you name a store.

## The keyword harvest is worse than random

Measured over the same store on the same night:

| source | hit rate |
|---|---|
| `"<term> clearance"` search pages | 1.5% |
| random products from the sitemap | **2.9%** |

Searching for the word "clearance" surfaces what Home Depot *markets* as
clearance, which is the shallow national stuff. The deep store-level
markdowns are on things nobody searches for — which is why they got
marked down. `discover` exists because of this.

## Where it can run — this one matters

**GitHub's hosted runners cannot do this job, and neither can most other
datacentre boxes.** Measured 2026-08-05, from `tools/hdclient.py`:

| client | residential | droplet |
|---|---|---|
| `urllib` / plain `curl` | **206 refused** | **206 refused** |
| `curl_cffi` (`safari17_0`, browser-grade TLS fingerprint) | 200 (4/4) | **206 refused** (0/4) |

Two independent checks guard the pricing gateway, and missing either one
is fatal: a browser-grade TLS fingerprint (`urllib` never presents one,
and is refused **even from a residential connection**) **and** a
non-datacentre address (`curl_cffi`, with the fingerprint right, is
still refused 0/4 from the droplet). It is not simply "the address
range" — a plain client fails everywhere regardless of where it runs,
and the right client still fails from a datacentre range. Both have to
be true for a request to succeed: the right fingerprint, from the right
kind of address. Neither is a user-agent or header fix; the fingerprint
comes from `curl_cffi`'s TLS/HTTP2 handshake, and the address is about
where the box physically is.

`tools/sweep.py`'s `call()` no longer swallows exceptions into an empty
list, either — a refused or unreachable chunk is counted and surfaced
(`in_batches`, the `BatchRun` it returns), and `discover()`/`scan()` die
loudly the moment a run is mostly refused, rather than quietly reporting
an empty catalogue as "nothing on clearance tonight." There is also no
separate `probe` stage that runs before them anymore — `discover()` and
`scan()` each check for themselves and fail fast the moment Home Depot
refuses a request, on the line where it actually happened.

### Check any machine before trusting it

Datacentre ranges vary — a VPS may or may not be refused, and guessing
wastes an evening. `curl | python3 -` cannot check this: `checkhost.py`
does `from tools import hdclient`, a package-relative import that needs
the repo's directory layout on disk, and needs `curl_cffi` installed to
actually reach Home Depot. Clone and install it instead:

```bash
git clone --depth 1 https://github.com/PlanetBandit/pennyrun.git /tmp/pennyrun-check
cd /tmp/pennyrun-check
python3 -m venv .venv && .venv/bin/pip install -q -r tools/requirements.txt
.venv/bin/python -m tools.checkhost
```

It prints the status of all three hosts and exits non-zero if Home Depot
won't quote a price.

### Running it there

There is no GitHub Actions runner to register anymore — collection runs
directly on whatever box passed the check above, on its own systemd
timer (`deploy/setup.sh` installs `pennyrun-discover.timer` and
`pennyrun-scan.timer` for exactly this). `scan()` uploads every run's
hits to the droplet when `PENNYRUN_API` and `PENNYRUN_INGEST_TOKEN` are
both set, and just writes `clearance.json` locally when they aren't —
but a systemd unit does not inherit anyone's login shell, so setting
those two in `~/.bashrc` or an interactive `export` does nothing for a
timer-triggered run. `deploy/setup.sh` wires both into
`pennyrun-scan.service` itself (`PENNYRUN_API` defaults to the host the
box serves; the token comes from `/etc/pennyrun/ingest.env` via
`EnvironmentFile=`) — if you're running the scan another way (a hand-rolled
unit, plain cron), set them the same way: `Environment=`/`EnvironmentFile=`
in the unit, or an explicit `env PENNYRUN_API=... PENNYRUN_INGEST_TOKEN=...`
in the crontab line, not a shell profile.

## Which stores get swept

Edit `tools/stores.json` — `[storeId, name]` pairs. Store ids come from
`pennyrun/hd-stores.json`, which carries all 2,021 US stores. Adding a
store costs a full pass of the scan stage; it does not slow discovery,
which only ever prices one store per night.

## Tuning

| knob | default | what it costs |
|---|---|---|
| `SWEEP_SLICE` | 60,000 | products priced per discover run (~330/sec) |
| `SWEEP_SHIP` | 3,000 | rows written to `clearance.json` (~700 KB) |
| `WORKERS` | 20 | measured clean to 40; 20 leaves headroom |
| `BATCH` | 16 | **do not raise** — the API caps `itemIds` here |

`SWEEP_SLICE` reads from the environment (see `tools/sweep.py`), so it
can be changed per machine without touching code.

## Things that will bite you

**Batch size is capped at 16.** `products(itemIds:)` silently returns
fewer results above that.

**`mediaPriceInventory` looks like a faster batch query. It is a trap** —
it returns no clearance block at all.

**Quantity has to come from the anchor location.** Every product returns
several pickup locations: your store plus nearby ones that could fill the
order. Taking any but the anchor reports a neighbour's pile as stock on
your shelf. This was live for a while and was wrong on 24% of rows.

**The "was" price is `clearance.value + dollarOff`, not `pricing.value`.**
`pricing.value` can itself be a promotion, which makes the discount read
smaller than Home Depot's own.

**There is no store-scoped search.** `searchModel` is not on this gateway
under any `x-experience-name`, `endCap` returns an empty shell, and
`savingsDepartment` returns null. Asking product by product is not a
workaround for something better — it is the only door.

## Safety rails

`scan` refuses to overwrite `clearance.json` when a run comes back empty
or below 40% of the previous run's total, and `harvest` keeps the
existing pool if a run collapses below 60%. A night where Home Depot
changes their markup or rate-limits us leaves yesterday's list in place
rather than shipping an empty app.

## Row format

`clearance.json` hits are arrays, not objects, to keep the file small:

```
[0]  name              [8]  store only (availabilityType.type)
[1]  itemId            [9]  canonical URL path
[2]  category          [10] UPC
[3]  clearance price   [11] store SKU
[4]  was price         [12] model number
[5]  percent off       [13] has a replacement SKU (being phased out)
[6]  storeId           [14] price at the last sweep, if it fell since
[7]  quantity at that store (null = not reported)
```

Field 14 comes from `tools/prices.json`, a ledger of every price from the
previous run — kept separately because `clearance.json` only ships the
deepest few thousand rows, and a price that fell out of the list is
exactly the one worth noticing when it drops again. Markdowns cascade, so
a price that moved last night is a price still on its way down; those
rows sort to the top of the app.

Field 13 runs about ten times richer among clearance items than among
everything else. Fields that looked promising and turned out to be dead
across 636 products: `availabilityType.discontinued`, `.obsolete`, and
`info.isBuryProduct` are always false or null.
