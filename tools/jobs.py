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
import urllib.request
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import hdclient, sweep, upload

# Requests allowed in any rolling window. The wall was ~2,350 in one window;
# this leaves roughly half of that as headroom, and at ~81 requests per store
# still allows ~14 stores an hour -- more than a metro's worth of demand.
BUDGET = int(os.environ.get("PENNYRUN_JOB_BUDGET", "1200"))
WINDOW = int(os.environ.get("PENNYRUN_JOB_WINDOW", "3600"))
POLL_SECONDS = int(os.environ.get("PENNYRUN_JOB_POLL", "20"))


class Budget:
    """A rolling cap on requests, shared across every job this process runs.

    Deliberately not a token bucket: the thing being modelled is "how many
    requests has this address made recently", which is a count over a window,
    and a bucket's refill rate would let a long quiet spell bank enough credit
    to fire a burst that looks exactly like the one that got us blocked.
    """

    def __init__(self, cap=BUDGET, window=WINDOW, clock=time.time):
        self.cap = cap
        self.window = window
        self._clock = clock
        self._spends = deque()          # (timestamp, count)

    def _prune(self, now):
        while self._spends and self._spends[0][0] <= now - self.window:
            self._spends.popleft()

    def spent(self):
        now = self._clock()
        self._prune(now)
        return sum(n for _, n in self._spends)

    def remaining(self):
        return max(0, self.cap - self.spent())

    def record(self, n):
        if n > 0:
            self._spends.append((self._clock(), n))

    def wait_for(self, n):
        """Seconds until `n` more requests would fit under the cap. 0 if now.

        A job larger than the whole cap would wait forever, so it is allowed
        through once the window is empty rather than deadlocking the queue --
        with the cap at 1,200 and a store at ~81 this cannot happen today, but
        a future caller with a bigger unit of work should not silently hang.
        """
        now = self._clock()
        self._prune(now)
        if n >= self.cap:
            return 0.0 if not self._spends else max(
                0.0, self._spends[0][0] + self.window - now)
        running = sum(c for _, c in self._spends)
        if running + n <= self.cap:
            return 0.0
        # drop the oldest spends until the new request fits
        freed, wait = 0, 0.0
        for ts, c in self._spends:
            freed += c
            wait = ts + self.window - now
            if running - freed + n <= self.cap:
                break
        return max(0.0, wait)


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
    url = f"{base.rstrip('/')}/api/v1/checks/next?collector={collector}"
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
    """Price one store. Returns True if the loop should keep going."""
    sid = job["store_id"]
    hot_ids, meta = candidates()
    if not hot_ids:
        report(base, token, job["job_id"], 0, 0, failed=True,
               note="hot list is empty -- run a nightly scan first")
        say("hot list is empty; nothing to price. Reported as failed.")
        return False

    need = sweep.chunks_for(len(hot_ids))
    wait = budget.wait_for(need)
    if wait > 0:
        say("holding %.0fs before store %s -- %d/%d requests used in the last %dm"
            % (wait, sid, budget.spent(), budget.cap, budget.window // 60))
        time.sleep(wait)

    before = sweep.previous_prices()
    try:
        rows, refused_rate = sweep.price_at(sid, sid, hot_ids, meta, before)
    except hdclient.Unreachable as e:
        budget.record(need)
        report(base, token, job["job_id"], 0, 0, failed=True, note="unreachable: %s" % e)
        say("store %s unreachable (%s) -- our network, not a refusal. Continuing." % (sid, e))
        return True

    budget.record(need)

    if refused_rate >= sweep.SCAN_ABORT_THRESHOLD:
        report(base, token, job["job_id"], len(rows), int(refused_rate * need),
               failed=True, note="refused %.0f%% of chunks" % (100 * refused_rate))
        say("store %s refused %.0f%% of its chunks -- stopping the loop rather than "
            "walking into the wall one job at a time." % (sid, 100 * refused_rate))
        return False

    if rows and os.environ.get("PENNYRUN_API") and os.environ.get("PENNYRUN_INGEST_TOKEN"):
        try:
            got = upload.send(rows, base, token)
            say("store %s: %d hits, uploaded %d" % (sid, len(rows), got["accepted"]))
        except upload.UploadError as e:
            # Collection succeeded; delivery did not. Report the job done
            # anyway -- the store WAS priced, and marking it failed would make
            # the next asker pay for a sweep that already happened.
            say("store %s priced but upload stopped partway (%s)" % (sid, e.cause))

    report(base, token, job["job_id"], len(rows), int(refused_rate * need))
    return True


def loop(base, token, once=False, budget=None):
    budget = budget or Budget()
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
