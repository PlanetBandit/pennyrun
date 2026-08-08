#!/usr/bin/env python3
"""One-time load of the catalogue facts we already have on disk.

`hd-stores.json` is a flat list of `[store_id, name, street, city, state,
zip]` rows -- 2,021 Home Depot stores, no Home Depot API call involved.
`clearance.json` is the array-shaped, index-addressed feed the sweep
writes (see `tools/README.md`, "Row format"); a *pair* of stores and item
prices per row, so the same item shows up once per store it was seen on
clearance at. Seeding a catalogue means collapsing that down to one row
per distinct item.

Both loads are idempotent: re-running against rows already in the table
updates the mutable fields (an address changed, a name changed) without
duplicating rows or moving `product.first_seen` backwards.
"""
import collections
import json
import pathlib
import sys

from db.categories import canonical

HERE = pathlib.Path(__file__).parent.resolve()
ROOT = HERE.parent

# Same bootstrap as db/migrate.py -- `python3 db/seed.py` only puts db/ on
# sys.path, not the repo root, so `from db import migrate` below would
# otherwise fail. `python3 -m db.seed` doesn't have this problem, but the
# direct-script form is what people reach for, so make both work.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _coords_by_zip(path):
    """{zip: (lat, lon)} from `pennyrun/stores.json`, which carries
    `[lat, lon, name, street, zip]` per row for Home Depot's own store
    locator data -- unlike `hd-stores.json`, no store_id, so the only
    join key the two files share is zip.

    Only kept for a zip that maps to exactly one row *here*. `stores()`
    additionally requires the zip to be unique on the `hd-stores.json`
    side before using it -- a zip shared by more than one store on
    either side is genuinely ambiguous (about 2% of zips, mostly dense
    urban areas with more than one Home Depot), and guessing which
    coordinate belongs to which store_id would plant a wrong location
    that nothing here would ever notice or correct.
    """
    rows = json.load(open(path))
    counts = collections.Counter(r[4] for r in rows if len(r) >= 5 and r[4])
    return {r[4]: (r[0], r[1]) for r in rows
            if len(r) >= 5 and r[4] and counts[r[4]] == 1}


def stores(conn, path, coords_path=None):
    """Load Home Depot stores from `hd-stores.json` into `store`.

    Each row is `[store_id, name, street, city, state, zip]`. A row with
    fewer than 6 fields or an empty store_id can't be seeded -- rather
    than crash the whole load over one bad row, skip it and count it so
    the caller can see what was dropped. Returns the number of rows
    actually inserted/updated.

    `coords_path`, when given, is `pennyrun/stores.json` -- see
    `_coords_by_zip`. `GET /api/v1/stores?lat=&lon=` filters on
    `lat is not null and lon is not null`, and nothing else in this
    codebase ever writes those columns, so leaving this unset is exactly
    "every geo lookup returns []" (the review's Important 2). Backfilling
    from a zip match here gets real coordinates onto roughly 91% of
    stores (the rest are the ambiguous-zip cases `_coords_by_zip` and the
    `hd_zip_counts` check below both refuse to guess at) without ever
    calling Home Depot -- this is a one-time join of two files already
    shipped in the repo, not a collection concern.
    """
    rows = json.load(open(path))
    coords = _coords_by_zip(coords_path) if coords_path else {}
    hd_zip_counts = collections.Counter(r[5] for r in rows if len(r) >= 6 and r[0])

    good = []
    skipped = 0
    for r in rows:
        if len(r) < 6 or not r[0]:
            skipped += 1
            continue
        lat, lon = (None, None)
        if coords and hd_zip_counts.get(r[5], 0) == 1:
            lat, lon = coords.get(r[5], (None, None))
        good.append((r[0], r[1], r[2], r[3], r[4], r[5], lat, lon))

    with conn.cursor() as cur:
        cur.executemany(
            "insert into store (store_id, name, street, city, state, zip, lat, lon) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s) on conflict (store_id) do update set "
            "name = excluded.name, street = excluded.street, city = excluded.city, "
            "state = excluded.state, zip = excluded.zip, "
            "lat = coalesce(excluded.lat, store.lat), "
            "lon = coalesce(excluded.lon, store.lon)",
            good)
    conn.commit()

    if skipped:
        print(f"seed.stores: skipped {skipped} malformed row(s) in {path}",
              file=sys.stderr)

    return len(good)


def products(conn, path):
    """Load the product catalogue from the existing clearance list.

    `clearance.json`'s `hits` are index-addressed rows, one per
    item/store observation -- the same item appears once per store it
    was seen at. Field [1] is itemId (not [0], which is the name), [0]
    is name, [2] category, [9] canonical URL, [10] UPC, [11] store SKU,
    [12] model number, [13] the "has a replacement SKU" flag (see
    `tools/upload.py`'s `to_observation` for why that's stored as
    `"1"`/`None`, not a real SKU). Collapsing to one row per item_id
    (first hit wins; the fields we keep don't vary meaningfully by
    store) is what turns ~3,000 rows into the catalogue's distinct
    product count.

    A row with fewer than 14 fields can't be indexed this way -- skip
    and count it rather than raising `IndexError` and taking the whole
    load down with it (`stores()`, just above, already does this for its
    own rows; this is the same guard on the other seed).

    On conflict, `last_seen` always moves, `name` heals a discovery
    placeholder without ever clobbering a real name (see the module
    docstring), and the rest of the catalogue fields
    (`category`/`upc`/`store_sku`/`model_number`/`canonical_url`/
    `replacement_id`) use `coalesce(excluded.x, product.x)` -- a later
    seed run only ever fills a gap, never blanks out a value already on
    file with a `NULL` from a row that happened not to carry it.
    """
    hits = json.load(open(path))["hits"]
    seen = {}
    skipped = 0
    for r in hits:
        if len(r) < 14:
            skipped += 1
            continue
        seen.setdefault(r[1], (
            r[1], r[0], canonical(r[2]), r[10] or None, r[11] or None,
            r[12] or None, r[9] or None, "1" if r[13] else None))

    with conn.cursor() as cur:
        cur.executemany(
            "insert into product (item_id, name, category, upc, store_sku, "
            "model_number, canonical_url, replacement_id) "
            "values (%s,%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (item_id) do update set "
            "last_seen = current_date, "
            "name = case when product.name like '(discovered) %%' "
            "            then excluded.name else product.name end, "
            "category = coalesce(excluded.category, product.category), "
            "upc = coalesce(excluded.upc, product.upc), "
            "store_sku = coalesce(excluded.store_sku, product.store_sku), "
            "model_number = coalesce(excluded.model_number, product.model_number), "
            "canonical_url = coalesce(excluded.canonical_url, product.canonical_url), "
            "replacement_id = coalesce(excluded.replacement_id, product.replacement_id)",
            list(seen.values()))
    conn.commit()

    if skipped:
        print(f"seed.products: skipped {skipped} malformed row(s) in {path}",
              file=sys.stderr)

    return len(seen)


if __name__ == "__main__":
    from db import migrate
    with migrate.connect() as c:
        migrate.apply(c)
        print("stores  ", stores(c, "pennyrun/hd-stores.json", "pennyrun/stores.json"))
        print("products", products(c, "pennyrun/clearance.json"))
