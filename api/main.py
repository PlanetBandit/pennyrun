from fastapi import FastAPI, HTTPException, Query
from api.db import rows

app = FastAPI(title="Penny Run", docs_url="/api/v1/docs")
V = "/api/v1"

LATEST = """
select distinct on (o.item_id, o.store_id)
       o.item_id, o.store_id, o.list_price, o.clearance_price,
       o.pct_off, o.quantity, o.store_only, o.observed_at
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


@app.get(V + "/store/{store_id}/clearance")
def store_clearance(store_id: str, limit: int = Query(200, le=1000),
                    min_pct: float = 0):
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
            " order by c.penny_score desc nulls last, l.pct_off desc limit %s",
            (store_id, min_pct, limit))
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
