# The sweep

`pennyrun/clearance.json` is not hand-written. It is rebuilt by
`sweep.py` from Home Depot's own pricing API, nightly, by the
`Nightly clearance sweep` workflow.

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
python3 tools/sweep.py harvest    # tools/pool.json          (weekly)
python3 tools/sweep.py discover   # tools/hot.json           (nightly)
python3 tools/sweep.py scan       # pennyrun/clearance.json  (nightly)
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

**scan** prices the pool plus the whole hot list at **every** store and
writes the app's list.

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

**GitHub's hosted runners cannot do this job.** Measured, not assumed:

| host | from here | from a GitHub runner |
|---|---|---|
| `apionline.homedepot.com` (pricing) | 200 | **206 "Generic Errors API"** |
| `www.homedepot.com/sitemap/…` | 200 | 200 |
| `www.homedepot.com/s/…` (search) | 200 | **403** |

Four header variants were tried from the runner — origin/referer, a
desktop user-agent, apollo client headers, and the plain request. All
four came back 206. The same four from a residential address all return
200. It is the address range, so no user-agent or header fixes it.

The failure mode is nasty: `call()` swallows exceptions and returns an
empty list, so a blocked runner looks exactly like eight stores with
nothing on clearance. The first real run reported `0 hits` at all eight
stores in 6–9 seconds each, against 34–69 seconds for a working scan.
The scan's safety rail refused to overwrite the list, and the `probe`
stage now runs first so the reason is on the line that matters.

**The fix is a self-hosted runner on a home network.** Register one
against the repo, then set the repository variable `SWEEP_RUNNER` to
`self-hosted` — the workflow reads it, so no code changes. An old
laptop, a Pi, or anything that stays on overnight is enough; the job is
~7 minutes of mostly waiting on the network.

Until then the nightly run fails on the probe step every night, which is
the honest signal that the list is not refreshing. Disable the workflow
in the Actions tab if the noise is worse than the reminder.

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

`SWEEP_SLICE` is set from a repository variable in the workflow, so it
can be changed without touching code.

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
