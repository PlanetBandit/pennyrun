from fastapi import FastAPI, HTTPException, Query
from api.db import rows

app = FastAPI(title="Penny Run", docs_url="/api/v1/docs")
V = "/api/v1"

from api.ingest import router as ingest_router
from api.checks import router as checks_router
from api.push import router as push_router
app.include_router(ingest_router)
app.include_router(checks_router)
app.include_router(push_router)

# The newest row per item and store, whatever it says. Since observations
# now include pairs with no clearance price, "newest" can mean "we looked
# and it was not on clearance any more" -- and every clearance read filters
# on `clearance_price is not null`, so those pairs correctly drop out
# instead of the old markdown standing forever as the last thing we knew.
LATEST = """
select distinct on (o.item_id, o.store_id)
       o.item_id, o.store_id, o.list_price, o.clearance_price,
       o.pct_off, o.quantity, o.store_only, o.observed_at, o.anchor_status
  from observation o
 where o.trusted
 order by o.item_id, o.store_id, o.observed_at desc
"""


@app.get(V + "/health")
def health():
    with rows() as cur:
        cur.execute("select (select count(*) from store) as stores, "
                    "(select count(*) from observation) as observations")
        r = cur.fetchone()
    return {"ok": True, **r}


@app.get(V + "/stores")
def stores(zip: str | None = None, lat: float | None = None,
           lon: float | None = None, n: int = Query(10, le=50)):
    with rows() as cur:
        if zip:
            cur.execute("select store_id, name, street, city, state, zip "
                        "from store where zip = %s limit %s", (zip, n))
        elif lat is not None and lon is not None:
            cur.execute(
                "select store_id, name, street, city, state, zip, "
                "  3959 * acos(greatest(-1, least(1, "
                "    cos(radians(%s))*cos(radians(lat))*cos(radians(lon)-radians(%s))"
                "    + sin(radians(%s))*sin(radians(lat))))) as miles "
                "from store where lat is not null and lon is not null "
                "order by miles limit %s",
                (lat, lon, lat, n))
        else:
            raise HTTPException(400, "give a zip, or lat and lon")
        return cur.fetchall()


def _excluded(exclude: str) -> list:
    """Comma-separated departments to leave out, normalised the way the
    counts endpoint reports them so the two always agree."""
    return [c.strip() for c in (exclude or "").split(",") if c.strip()][:80]


# Ordering has to be server-side for the same reason the department filter
# is: the app fetches a page, and a page is chosen by whatever the server
# ordered by. Sorting that page in the browser sorts the wrong 400 rows --
# "the cheapest of the 400 deepest markdowns" is not "the cheapest".
#
# Whitelisted, never interpolated from the request. `dir` picks a clause
# from this table; it never reaches the query as text.
_ORDER = {
    "best": {
        # The curated walk: shelves we know are empty sink, then deepest cut.
        # `coalesce(quantity, 1)` keeps unknown stock in the running -- we
        # have no count for it, which is not the same as knowing it is gone.
        "desc": "(coalesce(l.quantity, 1) = 0), c.penny_score desc nulls last, l.pct_off desc",
        "asc":  "(coalesce(l.quantity, 1) = 0), c.penny_score desc nulls last, l.pct_off asc",
    },
    "pct":   {"desc": "l.pct_off desc nulls last", "asc": "l.pct_off asc nulls last"},
    "price": {"desc": "l.clearance_price desc nulls last",
              "asc":  "l.clearance_price asc nulls last"},
    "qty":   {"desc": "l.quantity desc nulls last", "asc": "l.quantity asc nulls last"},
    "seen":  {"desc": "l.observed_at desc nulls last", "asc": "l.observed_at asc nulls last"},
}


def _order_by(sort: str, direction: str) -> str:
    """An unknown sort or direction falls back to the default rather than
    erroring: a stale bookmark should show the list, not a 422."""
    by = _ORDER.get(sort, _ORDER["best"])
    clause = by.get(direction, by["desc"])
    # item_id last, always. Without a total order, rows with equal pct_off
    # come back in whatever order the plan produced, so which ones survive
    # `limit` changes between two identical requests -- and the app would
    # show a different 400 each time it refetched.
    return clause + ", l.item_id"


@app.get(V + "/store/{store_id}/clearance")
def store_clearance(store_id: str, limit: int = Query(200, le=1000),
                    min_pct: float = 0, exclude: str = "",
                    sort: str = "best", dir: str = "desc",
                    min_qty: int = Query(0, ge=0)):
    """Rows ordered deepest-first, minus any departments the caller excludes.

    The exclusion has to happen HERE, not in the app. A typical store is
    ~85% paint, and paint is discounted hardest, so it owns the whole top
    of this ordering. A client that fetched `limit` rows and then dropped
    paint locally would be filtering a paint-saturated prefix: at limit=400
    that left 7 non-paint rows out of the ~105 the store actually had.
    Spending the row budget after the exclusion is the difference between
    a filter and the appearance of one.
    """
    with rows() as cur:
        cur.execute(
            f"with latest as ({LATEST}) "
            "select l.item_id, p.name, p.category, p.upc, p.canonical_url, "
            "       l.list_price, l.clearance_price, l.pct_off, l.quantity, "
            "       l.store_only, l.observed_at, c.penny_score "
            "  from latest l join product p using (item_id) "
            "  left join candidate c using (item_id) "
            " where l.store_id = %s and l.clearance_price is not null "
            "   and coalesce(l.pct_off, 0) >= %s "
            "   and coalesce(p.category, 'Other') <> all(%s) "
            # A stock floor of 1 means "on the shelf", and an unknown count
            # is not evidence of that -- 845 of ~22,000 rows have no count
            # at all. They are excluded here rather than assumed present,
            # because the whole value of this number is deciding whether the
            # drive is wasted.
            "   and (%s = 0 or l.quantity >= %s) "
            f" order by {_order_by(sort, dir)} limit %s",
            (store_id, min_pct, _excluded(exclude), min_qty, min_qty, limit))
        return cur.fetchall()


@app.get(V + "/store/{store_id}/flagged")
def store_flagged(store_id: str, limit: int = Query(100, le=500)):
    """Items this store calls CLEARANCE while showing no clearance price.

    Measured at roughly one pair in 192 when the signal was first checked.
    They are worth surfacing separately rather than mixing into the
    clearance list, because there is no price to sort or filter them by --
    the claim is only "the store's own system says this is clearance and
    the price feed does not agree yet". Sometimes that is a markdown about
    to appear; the point of recording them is to find out which.
    """
    with rows() as cur:
        cur.execute(
            f"with latest as ({LATEST}) "
            "select l.item_id, p.name, p.category, p.upc, p.canonical_url, "
            "       l.anchor_status, l.observed_at "
            "  from latest l join product p using (item_id) "
            " where l.store_id = %s and l.clearance_price is null "
            "   and l.anchor_status = 'CLEARANCE' "
            " order by l.observed_at desc, l.item_id limit %s",
            (store_id, limit))
        return cur.fetchall()


@app.get(V + "/store/{store_id}/categories")
def store_categories(store_id: str, min_pct: float = 0,
                     min_qty: int = Query(0, ge=0)):
    """Every department at this store with its true row count.

    Deliberately independent of `limit`: the chips in the app must count
    what the store has, not what one page of results happened to contain,
    or switching a department off would promise rows that were never
    fetched -- and switching it on would appear to lose some.

    It does honour `min_qty`, though, for the same reason: with a stock
    floor on, a chip reading "Tools 18" when only 15 are actually on a
    shelf is the same lie pointing the other way.
    """
    with rows() as cur:
        cur.execute(
            f"with latest as ({LATEST}) "
            "select coalesce(p.category, 'Other') as category, count(*) as n "
            "  from latest l join product p using (item_id) "
            " where l.store_id = %s and l.clearance_price is not null "
            "   and coalesce(l.pct_off, 0) >= %s "
            "   and (%s = 0 or l.quantity >= %s) "
            " group by 1 order by n desc, category",
            (store_id, min_pct, min_qty, min_qty))
        return cur.fetchall()


@app.get(V + "/item/{item_id}")
def item(item_id: str, stores: str | None = None):
    ids = [s for s in (stores or "").split(",") if s]
    with rows() as cur:
        cur.execute("select item_id, name, category, upc, store_sku, "
                    "model_number, canonical_url from product where item_id = %s",
                    (item_id,))
        head = cur.fetchone()
        if not head:
            raise HTTPException(404, "unknown item")
        if ids:
            cur.execute(f"with latest as ({LATEST}) select * from latest "
                        "where item_id = %s and store_id = any(%s)", (item_id, ids))
        else:
            cur.execute(f"with latest as ({LATEST}) select * from latest "
                        "where item_id = %s", (item_id,))
        head["prices"] = cur.fetchall()
    return head


@app.get(V + "/lookup")
def lookup(upc: str):
    with rows() as cur:
        cur.execute("select item_id from product where upc = %s", (upc,))
        r = cur.fetchone()
    if not r:
        raise HTTPException(404, "no product with that barcode")
    return item(r["item_id"])
