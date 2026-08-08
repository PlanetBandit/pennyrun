"""One spelling per department.

Two spellings of one aisle is two chips in the app, and switching one off
leaves the other behind -- a filter that visibly does not do what it says.
These cover the normaliser, both write paths that use it, and the backfill
for rows written before it existed.
"""
import os
import pathlib
import re

import pytest

from db.categories import ALIASES, CANONICAL, canonical

MIGRATION = pathlib.Path("db/migrations/005_category_aliases.sql")


# --------------------------------------------------------------- normaliser

def test_the_reported_duplicate_collapses():
    assert canonical("Outdoor") == canonical("Outdoors") == "Outdoors"


@pytest.mark.parametrize("variant", ["Outdoor", "OUTDOOR", "outdoor", "  Outdoor  "])
def test_case_and_whitespace_variants_all_land_on_one_spelling(variant):
    assert canonical(variant) == "Outdoors"


def test_the_other_split_departments_collapse_too():
    assert canonical("Storage & Organization") == "Storage"
    assert canonical("Doors") == canonical("Windows") == "Doors & Windows"


def test_a_case_variant_of_a_known_department_is_not_a_second_chip():
    """The map is keyed casefolded, so this holds for every canonical name
    without an entry per spelling."""
    for name in CANONICAL:
        assert canonical(name.upper()) == name
        assert canonical(name.lower()) == name


def test_adjacent_departments_are_left_alone():
    """This file fixes spelling, not taxonomy. Patio really is under
    Outdoors at Home Depot, and Bath under Plumbing -- merging them would
    be an opinion about how the store is laid out, and would quietly make
    departments the user had chosen to see disappear into another chip."""
    for name in ("Patio", "Pools", "Garage", "Bath", "Building",
                 "Lumber & Composites", "Garden"):
        assert canonical(name) == name


def test_an_unknown_department_passes_through_rather_than_becoming_Other():
    assert canonical("Seasonal Fireplaces") == "Seasonal Fireplaces"


def test_blank_becomes_none_so_it_cannot_overwrite_a_real_category():
    """Both writers use `coalesce(excluded.category, product.category)`.
    A category of "" is not null, so it would win that coalesce and erase a
    department we already knew."""
    assert canonical(None) is None
    assert canonical("") is None
    assert canonical("   ") is None


def test_normalising_twice_changes_nothing():
    for name in list(CANONICAL) + list(ALIASES) + ["Unknown Aisle"]:
        assert canonical(canonical(name)) == canonical(name)


def test_no_alias_target_is_itself_an_alias():
    """A -> B -> C would need two passes to settle, and only one is run."""
    for src, dst in ALIASES.items():
        assert dst.casefold() not in ALIASES, f"{src} -> {dst} chains"
        assert dst in CANONICAL, f"{dst} is not a canonical department"


# ------------------------------------------------------- migration in step

def test_the_migration_backfills_every_alias_in_the_python_map():
    """The SQL and the Python map are two statements of the same fact, so
    they can drift. If they do, rows written before the normaliser existed
    keep a spelling that new rows no longer use -- and the duplicate chip
    comes back for exactly the old data nobody thinks to re-check."""
    sql = MIGRATION.read_text()
    for src, dst in ALIASES.items():
        pattern = re.compile(
            r"set category = '" + re.escape(dst) + r"'[\s\S]*?'" + re.escape(src) + r"'",
            re.IGNORECASE)
        assert pattern.search(sql), f"migration does not backfill {src!r} -> {dst!r}"


def test_the_migration_knows_every_canonical_department():
    """The case-folding statement lists the canonical names inline. A
    department added to CANONICAL and not to the migration would leave old
    rows in whatever case they arrived in."""
    sql = MIGRATION.read_text()
    for name in CANONICAL:
        assert f"('{name}')" in sql, f"migration's canonical list is missing {name!r}"


# ---------------------------------------------------------- the write paths

@pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")
def test_the_collector_path_normalises_before_writing():
    from api.validate import check

    got = check({"item_id": "100", "store_id": "2565", "name": "Hose",
                 "category": "Outdoor", "clearance_price": 1.0})
    assert got["category"] == "Outdoors"


@pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"), reason="needs a database")
def test_the_backfill_settles_rows_written_before_the_normaliser(conn):
    """Applied against rows already in the table, which is the only case
    the migration exists for."""
    from db import migrate

    migrate.apply(conn)
    with conn.cursor() as cur:
        for i, cat in enumerate(["Outdoor", "Outdoors", "Storage & Organization",
                                 "Doors", "Windows", "outdoors", "Paint  Supplies",
                                 "  Tools  "]):
            cur.execute("insert into product (item_id, name, category)"
                        " values (%s,%s,%s) on conflict (item_id) do update"
                        " set category = excluded.category",
                        (f"cat{i}", f"Item {i}", cat))
        conn.commit()
        # re-run just the backfill; apply() has already recorded it
        cur.execute(MIGRATION.read_text())
        conn.commit()

        cur.execute("select item_id, category from product"
                    " where item_id like 'cat%' order by item_id")
        got = dict(cur.fetchall())

    assert got["cat0"] == got["cat1"] == got["cat5"] == "Outdoors"
    assert got["cat2"] == "Storage"
    assert got["cat3"] == got["cat4"] == "Doors & Windows"
    assert got["cat6"] == "Paint Supplies", "whitespace collapsed, not renamed"
    assert got["cat7"] == "Tools"

    # and the whole point: one row per department, not two
    with conn.cursor() as cur:
        cur.execute("select count(distinct category) from product"
                    " where item_id like 'cat%'")
        n = cur.fetchone()[0]
        # `test_schema` is shared across the whole session, so these rows
        # would otherwise sit in `product` for every later module --
        # test_seed.py counts it ("3,000 rows deduplicate to 811 distinct
        # items") and would read 819. Safe to delete: no observation
        # references them, which is what the append-only trigger protects.
        cur.execute("delete from product where item_id like 'cat%'")
    conn.commit()
    assert n == 5
