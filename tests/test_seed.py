import json
import os
from datetime import timedelta
from decimal import Decimal

import pytest
from db import migrate, seed

pytestmark = pytest.mark.skipif(
    not os.environ.get("PENNYRUN_DB_URL"),
    reason="needs PENNYRUN_DB_URL pointing at a scratch database")

# `conn` (shared session schema) and `fresh_conn` (private, per-test schema)
# come from tests/conftest.py -- see the docstrings there.

STORES_PATH = "pennyrun/hd-stores.json"
CLEARANCE_PATH = "pennyrun/clearance.json"


def test_seeds_all_stores(conn):
    migrate.apply(conn)
    n = seed.stores(conn, STORES_PATH)
    assert n == 2021
    with conn.cursor() as cur:
        cur.execute("select store_id, name, street, city, state, zip "
                    "from store where store_id = '2502'")
        row = cur.fetchone()
        assert row == ('2502', 'White Marsh', '9955 Pulaski Hwy (white Marsh)',
                        'Baltimore', 'MD', '21220')


def test_seeding_stores_twice_does_not_duplicate(conn):
    migrate.apply(conn)
    seed.stores(conn, STORES_PATH)
    seed.stores(conn, STORES_PATH)
    with conn.cursor() as cur:
        cur.execute("select count(*) from store")
        assert cur.fetchone()[0] == 2021


def test_seeds_products_from_the_existing_list(conn):
    migrate.apply(conn)
    seed.stores(conn, STORES_PATH)
    n = seed.products(conn, CLEARANCE_PATH)
    assert n == 811, "3,000 rows deduplicate to 811 distinct items"
    with conn.cursor() as cur:
        cur.execute("select count(*) from product")
        assert cur.fetchone()[0] == 811


def test_seeding_products_twice_does_not_duplicate(conn):
    migrate.apply(conn)
    seed.stores(conn, STORES_PATH)
    seed.products(conn, CLEARANCE_PATH)
    seed.products(conn, CLEARANCE_PATH)
    with conn.cursor() as cur:
        cur.execute("select count(*) from product")
        assert cur.fetchone()[0] == 811


def test_product_fields_land_in_the_right_columns(conn):
    # Field [1] is itemId, not field [0] -- a swapped index produces a
    # plausible-looking but wrong catalogue. Assert on a known row's real
    # values, not just the overall count.
    migrate.apply(conn)
    seed.stores(conn, STORES_PATH)
    seed.products(conn, CLEARANCE_PATH)
    with conn.cursor() as cur:
        cur.execute(
            "select name, category, upc, store_sku, model_number, canonical_url "
            "from product where item_id = '204767783'")
        row = cur.fetchone()
        assert row == (
            '3 ft. x 33.3 ft. (100 sq. ft. coverage area) Black Mineral Surface Roll Low Sl',
            'Building',
            '791308000105',
            '418625',
            '4305036',
            '/p/3-ft-x-33-3-ft-100-sq-ft-coverage-area-Black-Mineral-Surface-Roll-Low-Slope-Roofing-4305036/204767783',
        )


def test_product_first_seen_does_not_move_but_last_seen_does(fresh_conn):
    migrate.apply(fresh_conn)
    seed.stores(fresh_conn, STORES_PATH)
    seed.products(fresh_conn, CLEARANCE_PATH)
    with fresh_conn.cursor() as cur:
        cur.execute("select first_seen, last_seen from product "
                    "where item_id = '204767783'")
        first_seen, last_seen = cur.fetchone()

        # Move first_seen artificially into the past so we can prove a
        # second seed run does not touch it, only last_seen.
        cur.execute("update product set first_seen = first_seen - 30, "
                    "last_seen = last_seen - 30 where item_id = '204767783'")
    fresh_conn.commit()

    seed.products(fresh_conn, CLEARANCE_PATH)

    with fresh_conn.cursor() as cur:
        cur.execute("select first_seen, last_seen from product "
                    "where item_id = '204767783'")
        new_first_seen, new_last_seen = cur.fetchone()
        assert new_first_seen == first_seen - timedelta(days=30), \
            "first_seen must never move on conflict"
        assert new_last_seen == last_seen, \
            "last_seen should be bumped back up to today"


def test_reseed_heals_a_placeholder_name(fresh_conn):
    """`db/seed.py`'s catalogue re-seed is the authoritative source of
    real product names -- the one place a discovery-written placeholder
    (`api/ingest.py`: `"(discovered) <item_id>"`) is ever meant to get
    fixed. `on conflict do update set last_seen = current_date` alone
    never touches `name`, so a placeholder would otherwise survive every
    future re-seed forever -- the same bug `api/ingest.py`'s own upsert
    had, on the one path that actually has the real name to fix it with.
    """
    migrate.apply(fresh_conn)
    seed.stores(fresh_conn, STORES_PATH)

    with fresh_conn.cursor() as cur:
        cur.execute(
            "insert into product (item_id, name) values (%s, %s)",
            ("204767783", "(discovered) 204767783"))
    fresh_conn.commit()

    seed.products(fresh_conn, CLEARANCE_PATH)

    with fresh_conn.cursor() as cur:
        cur.execute("select name from product where item_id = '204767783'")
        assert cur.fetchone()[0] == (
            '3 ft. x 33.3 ft. (100 sq. ft. coverage area) Black Mineral Surface Roll Low Sl'
        ), "a real name from the catalogue must heal a discovery placeholder"


def test_reseed_never_clobbers_a_real_name(fresh_conn):
    migrate.apply(fresh_conn)
    seed.stores(fresh_conn, STORES_PATH)
    seed.products(fresh_conn, CLEARANCE_PATH)

    with fresh_conn.cursor() as cur:
        cur.execute("update product set name = %s where item_id = '204767783'",
                    ("A Human Corrected This Name",))
    fresh_conn.commit()

    seed.products(fresh_conn, CLEARANCE_PATH)

    with fresh_conn.cursor() as cur:
        cur.execute("select name from product where item_id = '204767783'")
        assert cur.fetchone()[0] == "A Human Corrected This Name"


def test_seeds_real_coordinates_by_zip_match(fresh_conn):
    """Important 2 of the review: `GET /api/v1/stores?lat=&lon=` filters
    on `lat is not null and lon is not null`, and nothing wrote those
    columns -- the geo branch returned `[]` for every request. `stores()`
    now backfills lat/lon from `pennyrun/stores.json` (which carries
    `[lat, lon, name, street, zip]`) by matching on zip, for the zips
    that are unambiguous on both sides. Store 2502 (White Marsh, zip
    21220) is one -- assert it against the file's actual value rather
    than a rounded guess, so a change to either input file's ordering or
    contents fails this test instead of silently drifting.
    """
    migrate.apply(fresh_conn)
    n = seed.stores(fresh_conn, STORES_PATH, "pennyrun/stores.json")
    assert n == 2021
    with fresh_conn.cursor() as cur:
        cur.execute("select lat, lon from store where store_id = '2502'")
        lat, lon = cur.fetchone()
        assert (float(lat), float(lon)) == (39.35902, -76.44301)


def test_ambiguous_zip_is_left_unmatched_not_guessed(fresh_conn, tmp_path):
    """A zip shared by more than one store on either side is genuinely
    ambiguous -- `_coords_by_zip` and the `hd_zip_counts` check in
    `stores()` both refuse to pick one, rather than planting a
    plausible-looking but potentially wrong coordinate that nothing
    would ever notice."""
    migrate.apply(fresh_conn)
    hd_path = tmp_path / "hd-stores-dup-zip.json"
    hd_path.write_text(json.dumps([
        ['1001', 'Store A', '1 Main St', 'Anytown', 'CA', '90000'],
        ['1002', 'Store B', '2 Main St', 'Anytown', 'CA', '90000'],  # shares 1001's zip
    ]))
    coords_path = tmp_path / "stores-coords.json"
    coords_path.write_text(json.dumps([
        [34.0, -118.0, 'Anytown', '1 Main St', '90000'],
    ]))
    seed.stores(fresh_conn, str(hd_path), str(coords_path))
    with fresh_conn.cursor() as cur:
        cur.execute("select lat, lon from store where store_id = '1001'")
        assert cur.fetchone() == (None, None)


def test_seeds_replacement_id_flag(conn):
    """Field [13] of a `clearance.json` hit ("has a replacement SKU") is
    a boolean marker, not an actual SKU -- stored as `"1"`/`None`, never
    `"0"`, so `coalesce(excluded.replacement_id, product.replacement_id)`
    in a later upsert can never blank it out with a false negative."""
    migrate.apply(conn)
    seed.stores(conn, STORES_PATH)
    seed.products(conn, CLEARANCE_PATH)
    with conn.cursor() as cur:
        cur.execute("select replacement_id from product where item_id = '204767783'")
        assert cur.fetchone()[0] is None, \
            "the seeded fixture item has replacementOMSID=null -- replacement_id must be NULL, not '0'"


def test_products_coalesce_never_blanks_a_field_with_null(fresh_conn):
    """A later seed run (or, in production, a later discovery upload)
    that happens not to carry a catalogue field must never blank out a
    value already on file -- `coalesce(excluded.x, product.x)` is the
    same-spirit fix as the pre-existing name-healing CASE, just in the
    other direction (fill a gap, never erase one)."""
    migrate.apply(fresh_conn)
    seed.stores(fresh_conn, STORES_PATH)
    seed.products(fresh_conn, CLEARANCE_PATH)

    with fresh_conn.cursor() as cur:
        cur.execute(
            "insert into product (item_id, name, category, upc) "
            "values ('204767783', 'placeholder', null, null) "
            "on conflict (item_id) do update set category = null, upc = null")
    fresh_conn.commit()

    with fresh_conn.cursor() as cur:
        cur.execute("select category, upc from product where item_id = '204767783'")
        assert cur.fetchone() == (None, None), "test setup must actually null the fields first"

    seed.products(fresh_conn, CLEARANCE_PATH)

    with fresh_conn.cursor() as cur:
        cur.execute("select category, upc from product where item_id = '204767783'")
        assert cur.fetchone() == ('Building', '791308000105')


def test_skips_malformed_product_rows_without_crashing(fresh_conn, tmp_path):
    """A `clearance.json` hit with fewer than 14 fields used to raise
    `IndexError` at `r[10]`, `r[11]`, `r[12]`, `r[13]` -- and under
    `set -euo pipefail`, `deploy/setup.sh` runs `db.seed` *before*
    installing the API service, so a short row would abort the whole
    deploy, not just the load. `stores()`, just above, already had this
    guard; `products()` didn't."""
    migrate.apply(fresh_conn)
    seed.stores(fresh_conn, STORES_PATH)
    bad_path = tmp_path / "clearance-bad.json"
    bad_path.write_text(json.dumps({"hits": [
        ["Good Item", "1001", "Tools", 1.0, 2.0, 50, "2502", 1, 0,
         "/p/x/1001", "000000000001", "sku1", "model1", 0, None],
        ["Too Short", "1002", "Tools"],  # fewer than 14 fields
    ]}))
    n = seed.products(fresh_conn, str(bad_path))
    assert n == 1
    with fresh_conn.cursor() as cur:
        cur.execute("select count(*) from product where item_id in ('1001','1002')")
        assert cur.fetchone()[0] == 1


def test_skips_malformed_store_rows_without_crashing(fresh_conn, tmp_path):
    migrate.apply(fresh_conn)
    bad_path = tmp_path / "hd-stores-bad.json"
    bad_path.write_text(json.dumps([
        ['1001', 'Good Store', '1 Main St', 'Anytown', 'CA', '90000'],
        ['1002', 'Too Short', 'Only Street'],           # fewer than 6 fields
        ['', 'No Id', '2 Main St', 'Anytown', 'CA', '90001'],  # empty store_id
        ['1003', 'Also Good', '3 Main St', 'Anytown', 'CA', '90002'],
    ]))
    n = seed.stores(fresh_conn, str(bad_path))
    assert n == 2
    with fresh_conn.cursor() as cur:
        cur.execute("select count(*) from store")
        assert cur.fetchone()[0] == 2
