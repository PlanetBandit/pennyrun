#!/usr/bin/env python3
"""Work the on-demand check queue from a residential connection.

    python3 -m tools.jobs          # poll forever
    python3 -m tools.jobs --once   # take at most one job, then stop

The droplet cannot ask Home Depot (wrong address range) and neither can a
browser (their edge refuses the CORS preflight). So the phone asks the droplet,
the droplet queues, and this works the queue from a connection Home Depot will
answer.

The safety-critical part of this file is the budget, not the loop. A single
job is ~81 requests, which is nothing -- but five back-to-back is ~2,025, and
this address was cut off at roughly 2,350 in one window on 2026-08-07. Pacing
each job on its own is not enough: the cap has to be shared across jobs, or a
busy hour walks into the wall one polite job at a time.
"""
import argparse
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import gate, hdclient, sweep, upload

POLL_SECONDS = int(os.environ.get("PENNYRUN_JOB_POLL", "20"))


# The budget and the lock both live in tools/gate.py now, because a
# per-feature cap is not a cap: the nightly scan spends ~1,155 requests and
# knew nothing about this loop's, and between them they could reach the wall.
Budget = gate.Budget

def say(msg):
    print("jobs: " + msg, flush=True)


def _post(url, body, token, timeout=60):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout, context=upload._TLS) as r:
        return json.loads(r.read())


def _get(url, token, timeout=60):
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=timeout, context=upload._TLS) as r:
        return json.loads(r.read())


def claim(base, token, collector):
    # quoted: a Mac hostname can contain a space or an apostrophe, which
    # would otherwise produce a malformed request
    url = (f"{base.rstrip('/')}/api/v1/checks/next"
           f"?collector={urllib.parse.quote(collector, safe='')}")
    return _get(url, token)


def report(base, token, job_id, hits, refused, failed=False, note=None):
    url = f"{base.rstrip('/')}/api/v1/checks/{job_id}/done"
    return _post(url, {"hits": hits, "refused": refused,
                       "failed": failed, "note": note}, token)


def candidates():
    """The hot list, plus the metadata rows() needs. Same source the nightly
    scan uses -- a job prices what is already known to be marked down, which
    is why a store costs ~81 requests instead of ~587."""
    pool = sweep.load_pool()
    hot = sweep.read(sweep.HOT, {})
    meta = {p[1]: [p[0], p[2]] for p in pool}
    for pid, v in hot.items():
        meta.setdefault(pid, [v.get("name", ""), v.get("cat", "")])
    return sorted(hot), meta


def run_one(job, budget, base, token):
    """Price one store. Returns True if the loop should keep going.

    Every exit path reports the job. A job left `running` is a permanent
    tombstone -- `check_job_live` is partial on ('queued','running'), so the
    store can never be queued again and every future ask returns the same
    stranded job forever.
    """
    sid = job["store_id"]
    job_id = job["job_id"]
    reported = False

    def tell(hits, refused, failed=False, note=None):
        nonlocal reported
        try:
            report(base, token, job_id, hits, refused, failed=failed, note=note)
            reported = True
        except Exception as e:                       # noqa: BLE001 -- see below
            # If we cannot report, the job stays `running` and the reaper on
            # the droplet side has to free it. Say so loudly rather than
            # exiting quietly with the store locked.
            say("COULD NOT REPORT job %s (%s) -- store %s stays locked until the "
                "droplet's reaper frees it" % (job_id, e, sid))

    try:
        hot_ids, meta = candidates()
        if not hot_ids:
            tell(0, 0, failed=True, note="hot list is empty -- run a nightly scan first")
            say("hot list is empty; nothing to price.")
            return False

        need = sweep.chunks_for(len(hot_ids))
        wait = budget.wait_for(need)
        if wait > 0:
            say("holding %.0fs before store %s -- %d/%d requests used in the last %dm"
                % (wait, sid, budget.spent(), budget.cap, budget.window // 60))
            time.sleep(wait)
            # sleep can return marginally early; do not proceed on a stale check
            while budget.wait_for(need) > 0:
                time.sleep(1)

        # Recorded BEFORE the call, not after. An exception mid-store still
        # spent those requests against the address, and a budget that only
        # counts successful stores is not a budget.
        budget.record(need)

        before = sweep.previous_prices()
        # One caller at a time. Blocking on purpose: a job that arrives during
        # the nightly scan should wait its turn rather than run alongside it --
        # two callers at twenty workers each from one address is the traffic
        # that got this address cut off. Held around the pricing only, so a
        # long-running loop never starves the nightly sweep.
        with gate.exclusive("jobs.store." + str(sid)):
            rows, refused_rate, unreachable_rate = sweep.price_at(
                sid, sid, hot_ids, meta, before)

        # "They refused us" and "we had no network" need different answers, and
        # collapsing them is what let an all-unreachable job report `done` and
        # mark a store fresh for six hours that was never contacted.
        if unreachable_rate >= sweep.SCAN_ABORT_THRESHOLD:
            tell(len(rows), 0, failed=True,
                 note="unreachable on %.0f%% of chunks" % (100 * unreachable_rate))
            say("store %s: %.0f%% of chunks unreachable -- that is our network, not "
                "a refusal. Not marking the store checked." % (sid, 100 * unreachable_rate))
            return True

        if refused_rate >= sweep.SCAN_ABORT_THRESHOLD:
            tell(len(rows), int(refused_rate * need), failed=True,
                 note="refused %.0f%% of chunks" % (100 * refused_rate))
            say("store %s refused %.0f%% of its chunks -- stopping the loop rather "
                "than walking into the wall one job at a time."
                % (sid, 100 * refused_rate))
            return False

        # A store is only "checked" once its prices are actually in the
        # database. Reporting `done` on a failed upload marks it fresh for
        # FRESH_FOR with nothing behind the claim -- the user is told their
        # prices are current and served the old ones.
        if rows:
            try:
                got = upload.send(rows, base, token)
                say("store %s: %d hits, uploaded %d" % (sid, len(rows), got["accepted"]))
            except upload.UploadError as e:
                tell(e.partial.get("accepted", 0), 0, failed=True,
                     note="priced but upload stopped: %s" % e.cause)
                say("store %s priced but only %d rows landed -- NOT marking it "
                    "checked, because the data is not there."
                    % (sid, e.partial.get("accepted", 0)))
                return True

        tell(len(rows), int(refused_rate * need))
        return True

    except Exception as e:                           # noqa: BLE001
        # Anything unexpected: still report, so the store is not stranded.
        if not reported:
            tell(0, 0, failed=True, note="collector error: %s: %s" % (type(e).__name__, e))
        say("store %s failed unexpectedly (%s: %s)" % (sid, type(e).__name__, e))
        return False


def loop(base, token, once=False, budget=None):
    budget = budget or gate.shared_budget()
    collector = socket.gethostname()
    say("working the queue at %s as %s (cap %d req / %dm)"
        % (base, collector, budget.cap, budget.window // 60))
    while True:
        try:
            got = claim(base, token, collector)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            say("could not reach the droplet (%s); retrying" % e)
            if once:
                return 1
            time.sleep(POLL_SECONDS)
            continue

        job = got.get("job")
        if not job:
            if once:
                say("nothing queued")
                return 0
            time.sleep(POLL_SECONDS)
            continue

        say("claimed job %s for store %s (%d behind)"
            % (job["job_id"], job["store_id"], got.get("queued_behind", 0)))
        keep_going = run_one(job, budget, base, token)
        if once or not keep_going:
            return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--once", action="store_true", help="take at most one job")
    args = ap.parse_args(argv)

    base = os.environ.get("PENNYRUN_API")
    token = os.environ.get("PENNYRUN_INGEST_TOKEN")
    if not base or not token:
        print("jobs: set PENNYRUN_API and PENNYRUN_INGEST_TOKEN "
              "(see .env.collector)", file=sys.stderr)
        return 2
    return loop(base, token, once=args.once)


if __name__ == "__main__":
    sys.exit(main())
