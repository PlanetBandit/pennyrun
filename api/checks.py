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
import logging
import os
import uuid

from fastapi import APIRouter, Header, HTTPException

from api.db import rows
from api.ingest import authorise

log = logging.getLogger("pennyrun.checks")
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

# A job claimed but never reported is a permanent tombstone: check_job_live is
# partial on ('queued','running'), so that store can never be queued again and
# every future ask returns the same stranded job forever. The collector can
# crash, lose its network mid-report, or simply have its lid closed -- the spec
# calls a sleeping home box the expected steady state -- so this is a routine
# event, not an exotic one. Generous enough that a slow job is never stolen
# from a working collector.
STUCK_AFTER_MIN = int(os.environ.get("PENNYRUN_STUCK_AFTER_MIN", "30"))

# The whole country is 2,021 stores. Unauthenticated, uncapped, ~203 POSTs
# would queue every one of them: 2,021 x 81 = ~164,000 requests, about 137
# hours of the collector's entire budget, with real users behind it forever.
# Coalescing does not help -- each store is a distinct job.
MAX_QUEUE_DEPTH = int(os.environ.get("PENNYRUN_MAX_QUEUE_DEPTH", "60"))


def _reap(cur):
    """Free jobs whose collector never came back. Returns how many."""
    cur.execute(
        "update check_job set state = 'queued', claimed_at = null, claimed_by = null,"
        "  note = coalesce(note,'') || ' [reaped: collector never reported]'"
        " where state = 'running'"
        "   and claimed_at < now() - make_interval(mins => %s)"
        " returning job_id", (STUCK_AFTER_MIN,))
    return len(cur.fetchall())


def _freshness(cur, store_ids):
    """{store_id: newest data timestamp or None} across both sources."""
    cur.execute(
        "select s.store_id,"
        "       greatest("
        "         (select max(j.finished_at) from check_job j"
        "           where j.store_id = s.store_id and j.state = 'done'),"
        "         (select max(o.observed_at) from observation o"
        "           where o.store_id = s.store_id"
        "             and o.clearance_price is not null)"
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
    # Sorted, not just deduplicated: the whole ask is one transaction, and
    # two clients sending overlapping stores in opposite order take the
    # check_job_live locks in opposite order and deadlock. A store picker sorts
    # by distance from each user, so overlapping-but-differently-ordered asks
    # are the normal case, not an exotic one. A global lock order removes it.
    store_ids = sorted(dict.fromkeys(str(s) for s in store_ids))
    if len(store_ids) > MAX_STORES_PER_ASK:
        raise HTTPException(400, f"at most {MAX_STORES_PER_ASK} stores per ask")

    device_id = payload.get("device_id")
    if device_id is not None:
        try:
            device_id = str(uuid.UUID(str(device_id)))
        except (ValueError, AttributeError, TypeError):
            raise HTTPException(400, "device_id must be a uuid")

    with rows() as cur:
        _reap(cur)
        cur.execute("select count(*) as n from check_job where state = 'queued'")
        depth = cur.fetchone()["n"]
        if depth >= MAX_QUEUE_DEPTH:
            raise HTTPException(
                503, f"the queue is full ({depth} stores waiting); try again later")

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
                " on conflict (store_id) where state in ('queued','running')"
                " do nothing returning job_id", (sid,))
            got = cur.fetchone()
            if got:
                job_id = got["job_id"]
            else:
                cur.execute("select job_id from check_job where store_id = %s "
                            "and state in ('queued','running')", (sid,))
                existing = cur.fetchone()
                if existing:
                    job_id = existing["job_id"]
                else:
                    # The live job left ('queued','running') between our insert
                    # and this read. It may have finished -- or it may have
                    # FAILED, in which case calling the store fresh would tell
                    # the user their prices are current when the check just
                    # failed. Retry the insert instead of guessing; no live job
                    # exists now, so it succeeds.
                    cur.execute(
                        "insert into check_job (store_id) values (%s)"
                        " on conflict (store_id) where state in ('queued','running')"
                        " do nothing returning job_id", (sid,))
                    retry = cur.fetchone()
                    if not retry:
                        cur.execute("select job_id from check_job where store_id = %s"
                                    " and state in ('queued','running')", (sid,))
                        retry = cur.fetchone()
                    if not retry:
                        raise HTTPException(503, "could not queue %s, try again" % sid)
                    job_id = retry["job_id"]

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
                # 0, not None: a job already running has nobody ahead of it
                q["position"] = pos.get(q["job_id"], 0)

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
        freed = _reap(cur)
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

    def _count(key):
        v = payload.get(key)
        if v is None:
            return None
        try:
            n = int(v)
        except (TypeError, ValueError):
            raise HTTPException(400, f"{key} must be a whole number")
        if n < 0:
            raise HTTPException(400, f"{key} cannot be negative")
        return n

    hits, refused = _count("hits"), _count("refused")
    with rows() as cur:
        cur.execute(
            "update check_job set state = %s, finished_at = now(),"
            "  hits = %s, refused = %s, note = %s"
            " where job_id = %s and state = 'running'"
            " returning job_id, store_id, state",
            (state, hits, refused, (payload.get("note") or None), job_id))
        got = cur.fetchone()
        if not got:
            raise HTTPException(404, "no running job with that id")
        cur.execute("select device_id from check_watcher where job_id = %s", (job_id,))
        watchers = [r["device_id"] for r in cur.fetchall()]

    # Best effort, and deliberately after the transaction: a push that fails
    # must never fail the report, or a collector that priced a store perfectly
    # well would be told its job did not land. GET /checks/{id} is the durable
    # path; this is the courtesy on top.
    sent = pruned = 0
    if watchers:
        from api.push import send_to_devices
        name = got["store_id"]
        if got["state"] == "done":
            title, body = "Prices updated", f"{name} is current — {got['hits'] or 0} on clearance"
        else:
            title, body = "Check didn't finish", f"Home Depot didn't answer for {name}"
        try:
            sent, pruned = send_to_devices(watchers, title, body, url="/#checked")
        except Exception:                            # noqa: BLE001
            log.warning("notifying watchers of job %s failed", job_id)

    return {"job_id": got["job_id"], "store_id": got["store_id"],
            "state": got["state"], "watchers": len(watchers),
            "notified": sent, "pruned": pruned}


@router.get(V + "/checks/{job_id}")
def status(job_id: int):
    """What the app polls. Works whether or not push ever fires."""
    with rows() as cur:
        cur.execute(
            "select job_id, store_id, state, requested_at, claimed_at, finished_at,"
            "       hits, refused from check_job where job_id = %s", (job_id,))
        job = cur.fetchone()
        if not job:
            raise HTTPException(404, "unknown job")
        seen = _collector_seen(cur)
    job["collector_online"] = _online(seen)
    return job
