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
store. That is what the scan stage does.

## The two stages

```bash
python3 tools/sweep.py harvest   # rebuild tools/pool.json (weekly)
python3 tools/sweep.py scan      # rebuild pennyrun/clearance.json (nightly)
```

**harvest** scrapes product ids out of Home Depot's clearance search
pages into `tools/pool.json`. The pool moves slowly, so it runs on
Sundays. Roughly 8,000 products from 30 departments.

**scan** asks `products(itemIds:)` for every product in the pool, once
per store in `tools/stores.json`, and keeps whatever comes back with an
in-store clearance price. About 14 minutes for 8 stores.

No key, no cookie, no login. The API is public; it just won't quote a
price until you name a store.

## Which stores get swept

Edit `tools/stores.json` — `[storeId, name]` pairs. Store ids come from
`pennyrun/hd-stores.json`, which carries all 2,021 US stores. Adding
stores costs roughly 1.7 minutes of scan time each.

## Things that will bite you

**Batch size is capped at 16.** `products(itemIds:)` silently returns
fewer results above that. Don't raise `BATCH`.

**`mediaPriceInventory` looks like a faster batch query. It is a trap** —
it returns no clearance block at all.

**Quantity has to come from the anchor location.** Every product returns
several pickup locations: your store plus nearby ones that could fill
the order. Taking any but the anchor reports a neighbour's pile as stock
on your shelf. This was live for a while and was wrong on 24% of rows.

**The "was" price is `clearance.value + dollarOff`, not `pricing.value`.**
`pricing.value` can itself be a promotion, which makes the discount read
smaller than Home Depot's own.

## Safety rails

`scan` refuses to overwrite `clearance.json` when a run comes back empty
or below 40% of the previous run's hit count, and `harvest` keeps the
existing pool if a run collapses below 60%. A night where Home Depot
changes their markup or rate-limits us leaves yesterday's list in place
rather than shipping an empty app.

## Row format

`clearance.json` hits are arrays, not objects, to keep the file small:

```
[0]  name              [7]  quantity at that store (null = not reported)
[1]  itemId            [8]  store only (availabilityType.type)
[2]  category          [9]  canonical URL path
[3]  clearance price   [10] UPC
[4]  was price         [11] store SKU
[5]  percent off       [12] model number
[6]  storeId           [13] has a replacement SKU (being phased out)
```

Field 13 runs about ten times richer among clearance items than among
everything else, which makes it a genuine phase-out signal. Fields that
looked promising and turned out to be dead across 636 products:
`availabilityType.discontinued`, `.obsolete`, and `info.isBuryProduct`
are always false or null.
