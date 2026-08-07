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
