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
import json
import pathlib
import sys

HERE = pathlib.Path(__file__).parent.resolve()
ROOT = HERE.parent

# Same bootstrap as db/migrate.py -- `python3 db/seed.py` only puts db/ on
# sys.path, not the repo root, so `from db import migrate` below would
# otherwise fail. `python3 -m db.seed` doesn't have this problem, but the
# direct-script form is what people reach for, so make both work.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def stores(conn, path):
    """Load Home Depot stores from `hd-stores.json` into `store`.

    Each row is `[store_id, name, street, city, state, zip]`. A row with
    fewer than 6 fields or an empty store_id can't be seeded -- rather
    than crash the whole load over one bad row, skip it and count it so
    the caller can see what was dropped. Returns the number of rows
    actually inserted/updated.
    """
    rows = json.load(open(path))
    good = []
    skipped = 0
    for r in rows:
        if len(r) < 6 or not r[0]:
            skipped += 1
            continue
        good.append((r[0], r[1], r[2], r[3], r[4], r[5]))

    with conn.cursor() as cur:
        cur.executemany(
            "insert into store (store_id, name, street, city, state, zip) "
            "values (%s,%s,%s,%s,%s,%s) on conflict (store_id) do update set "
            "name = excluded.name, street = excluded.street, city = excluded.city, "
            "state = excluded.state, zip = excluded.zip",
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
    [12] model number. Collapsing to one row per item_id (first hit
    wins; the fields we keep don't vary meaningfully by store) is what
    turns ~3,000 rows into the catalogue's distinct product count.

    On conflict, only `last_seen` moves -- `first_seen` defaults to
    `current_date` on first insert and must never shift again, forwards
    or backwards, once a product has been seen. `name` is the one
    exception: this catalogue load is the authoritative source of real
    product names, and `api/ingest.py`'s discovery upsert writes a
    placeholder (`"(discovered) <item_id>"`) for any item it finds
    without one. A conflict here heals that placeholder with the real
    name from this run, but -- like `api/ingest.py`'s own upsert -- never
    overwrites a name that's already real, so a re-seed can't clobber a
    hand-corrected one.
    """
    hits = json.load(open(path))["hits"]
    seen = {}
    for r in hits:
        seen.setdefault(r[1], (r[1], r[0], r[2], r[10], r[11], r[12], r[9]))

    with conn.cursor() as cur:
        cur.executemany(
            "insert into product (item_id, name, category, upc, store_sku, "
            "model_number, canonical_url) values (%s,%s,%s,%s,%s,%s,%s) "
            "on conflict (item_id) do update set "
            "last_seen = current_date, "
            "name = case when product.name like '(discovered) %%' "
            "            then excluded.name else product.name end",
            list(seen.values()))
    conn.commit()
    return len(seen)


if __name__ == "__main__":
    from db import migrate
    with migrate.connect() as c:
        migrate.apply(c)
        print("stores  ", stores(c, "pennyrun/hd-stores.json"))
        print("products", products(c, "pennyrun/clearance.json"))
