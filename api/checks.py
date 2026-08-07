"""On-demand area checks: the queue between a phone and the collector.

The phone cannot ask Home Depot itself. Their edge answers the CORS preflight
with 200 and no `Access-Control-Allow-Origin` header at all, so a browser is
refused before it ever sends the real request. The droplet cannot ask either --
wrong address range. Only the residential collector can, so the phone asks us,
we queue, and the collector works the queue.

Three rules shape everything here:

  A job belongs to a STORE, not a user. `check_job_live` (a partial unique
  index on store_id where the job is queued or running) makes that structural:
  two users racing for the same store both run `insert ... on conflict do
  nothing` and exactly one job survives. No application locking, no
  read-then-write window.

  A store priced recently is not priced again. Freshness is the newest of
  (last successful job, newest observation) -- the second half matters because
  the nightly sweep also covers stores, and a store it swept an hour ago should
  cost a user nothing.

  Never promise what we cannot deliver. If the collector has not checked in,
  the response says so instead of implying a queue that is draining. A
  countdown that never runs down is the same lie as reporting stores that were
  never priced.
"""
import os
import uuid

from fastapi import APIRouter, Header, HTTPException

from api.db import rows
from api.ingest import authorise

router = APIRouter()
V = "/api/v1"

# How old a store's prices may be before an ask re-queues it. This single
# number sets the whole economics: tighter and every ask costs 81 requests,
# looser and "current" becomes a lie to someone standing in an aisle.
# Markdowns cascade daily, so hours rather than minutes.
FRESH_FOR_MIN = int(os.environ.get("PENNYRUN_FRESH_FOR_MIN", "360"))

# A collector quieter than this is treated as away. It polls on a much shorter
# interval than this, so silence here is real.
HEARTBEAT_STALE_MIN = int(os.environ.get("PENNYRUN_HEARTBEAT_STALE_MIN", "10"))

MAX_STORES_PER_ASK = 10


def _freshness(cur, store_ids):
    """{store_id: newest data timestamp or None} across both sources."""
    cur.execute(
        "select s.store_id,"
        "       greatest("
        "         (select max(j.finished_at) from check_job j"
        "           where j.store_id = s.store_id and j.state = 'done'),"
        "         (select max(o.observed_at) from observation o"
        "           where o.store_id = s.store_id)"
        "       ) as fresh_at"
        "  from unnest(%s::text[]) as s(store_id)",
        (list(store_ids),))
    return {r["store_id"]: r["fresh_at"] for r in cur.fetchall()}


def _collector_seen(cur):
    cur.execute("select max(last_seen) as seen from collector_heartbeat")
    return (cur.fetchone() or {}).get("seen")


@router.post(V + "/checks")
def ask(payload: dict):
    """A phone asks for its stores. Answers immediately where it can."""
    store_ids = payload.get("store_ids")
    if not isinstance(store_ids, list) or not store_ids:
        raise HTTPException(400, "store_ids must be a non-empty list")
    if len(store_ids) > MAX_STORES_PER_ASK:
        raise HTTPException(400, f"at most {MAX_STORES_PER_ASK} stores per ask")
    store_ids = list(dict.fromkeys(str(s) for s in store_ids))

    device_id = payload.get("device_id")
    if device_id is not None:
        try:
            device_id = str(uuid.UUID(str(device_id)))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "device_id must be a uuid")

    with rows() as cur:
        cur.execute("select store_id from store where store_id = any(%s)", (store_ids,))
        known = {r["store_id"] for r in cur.fetchall()}
        unknown = [s for s in store_ids if s not in known]
        if unknown:
            raise HTTPException(400, f"unknown store_id: {unknown[:3]}")

        if device_id:
            # A watcher row is low-stakes -- it only decides who gets told. It
            # is deliberately not an identity claim, and nothing here trusts it.
            cur.execute("insert into device (device_id) values (%s) "
                        "on conflict (device_id) do update set last_seen = now()",
                        (device_id,))

        fresh_at = _freshness(cur, store_ids)
        cur.execute("select now() - make_interval(mins => %s) as cutoff", (FRESH_FOR_MIN,))
        cutoff = cur.fetchone()["cutoff"]

        ready, queued = [], []
        for sid in store_ids:
            at = fresh_at.get(sid)
            if at is not None and at >= cutoff:
                ready.append({"store_id": sid, "as_of": at})
                continue

            # on conflict does nothing when a live job already exists for this
            # store -- that IS the coalescing, and it is atomic.
            cur.execute(
                "insert into check_job (store_id) values (%s) "
                "on conflict do nothing returning job_id", (sid,))
            got = cur.fetchone()
            if got:
                job_id = got["job_id"]
            else:
                cur.execute("select job_id from check_job where store_id = %s "
                            "and state in ('queued','running')", (sid,))
                existing = cur.fetchone()
                if not existing:
                    # the live job finished between our insert and this read;
                    # the store is fresh now, so say so rather than looping
                    ready.append({"store_id": sid, "as_of": None})
                    continue
                job_id = existing["job_id"]

            if device_id:
                cur.execute("insert into check_watcher (job_id, device_id) values (%s,%s) "
                            "on conflict do nothing", (job_id, device_id))
            queued.append({"store_id": sid, "job_id": job_id})

        # Position is over the whole queue, not this ask -- what the user waits
        # on is everyone ahead of them, including other people's stores.
        if queued:
            cur.execute(
                "select job_id, row_number() over (order by requested_at) as pos"
                "  from check_job where state = 'queued'")
            pos = {r["job_id"]: r["pos"] for r in cur.fetchall()}
            for q in queued:
                q["position"] = pos.get(q["job_id"])

        seen = _collector_seen(cur)

    return {"ready": ready, "queued": queued,
            "collector_seen": seen,
            "collector_online": _online(seen),
            "fresh_for_minutes": FRESH_FOR_MIN}


def _online(seen):
    if seen is None:
        return False
    import datetime
    now = datetime.datetime.now(seen.tzinfo) if seen.tzinfo else datetime.datetime.now()
    return (now - seen).total_seconds() <= HEARTBEAT_STALE_MIN * 60


@router.get(V + "/checks/next")
def claim(authorization: str = Header(None), collector: str = "unknown"):
    """The collector asks for work. Claims exactly one job, atomically.

    `for update skip locked` means a second collector can be added later
    without either of them handing out the same job twice.
    """
    authorise(authorization)
    with rows() as cur:
        cur.execute(
            "update check_job set state = 'running', claimed_at = now(), claimed_by = %s"
            " where job_id = (select job_id from check_job where state = 'queued'"
            "                  order by requested_at limit 1 for update skip locked)"
            " returning job_id, store_id", (collector,))
        job = cur.fetchone()
        cur.execute(
            "insert into collector_heartbeat (collector, last_seen, last_job_id)"
            " values (%s, now(), %s)"
            " on conflict (collector) do update set last_seen = now(),"
            "   last_job_id = coalesce(excluded.last_job_id, collector_heartbeat.last_job_id)",
            (collector, job["job_id"] if job else None))
        cur.execute("select count(*) as n from check_job where state = 'queued'")
        waiting = cur.fetchone()["n"]

    return {"job": job, "queued_behind": waiting}


@router.post(V + "/checks/{job_id}/done")
def finish(job_id: int, payload: dict, authorization: str = Header(None)):
    """The collector reports. A failed job does NOT make its store fresh."""
    authorise(authorization)
    failed = bool(payload.get("failed"))
    state = "failed" if failed else "done"
    with rows() as cur:
        cur.execute(
            "update check_job set state = %s, finished_at = now(),"
            "  hits = %s, refused = %s, note = %s"
            " where job_id = %s and state = 'running'"
            " returning job_id, store_id, state",
            (state, payload.get("hits"), payload.get("refused"),
             (payload.get("note") or None), job_id))
        got = cur.fetchone()
        if not got:
            raise HTTPException(404, "no running job with that id")
        cur.execute("select device_id from check_watcher where job_id = %s", (job_id,))
        watchers = [r["device_id"] for r in cur.fetchall()]

    # Push lands here once it exists. Returning the list now means the
    # collector's report already carries who would have been told, which makes
    # the wiring testable before any notification is sent.
    return {"job_id": got["job_id"], "store_id": got["store_id"],
            "state": got["state"], "watchers": len(watchers)}


@router.get(V + "/checks/{job_id}")
def status(job_id: int):
    """What the app polls. Works whether or not push ever fires."""
    with rows() as cur:
        cur.execute(
            "select job_id, store_id, state, requested_at, claimed_at, finished_at,"
            "       hits, refused, note from check_job where job_id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            raise HTTPException(404, "unknown job")
        seen = _collector_seen(cur)
    job["collector_online"] = _online(seen)
    return job
