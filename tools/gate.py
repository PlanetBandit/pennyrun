"""One door to Home Depot, and one budget behind it.

Everything that talks to their gateway from this address goes through here:
`tools/sweep.py`'s three stages and `tools/jobs.py`'s per-store jobs. Two
things have to be true and neither was:

  **Only one caller at a time.** Two sweeps at twenty workers each from one
  address is precisely the traffic that got this address cut off on
  2026-08-07. `collect.sh` had a lock and the systemd units had a lock, but
  they were different locks and `tools/jobs.py` had none, so a nightly scan
  and an on-demand job could run simultaneously. Putting the gate *inside*
  sweep and jobs means every invocation path is covered -- cron, systemd,
  collect.sh, a hand-run module -- without anyone having to remember.

  **One shared budget.** A per-feature cap is not a cap. The nightly scan
  spends ~1,155 requests and knew nothing about the job loop's 1,200; between
  them they could reach 2,355 against a wall measured at ~2,350.

`fcntl.flock` rather than a lock directory: it is in Python's stdlib on both
macOS and Linux (unlike the `flock(1)` binary, which macOS does not ship), the
kernel releases it if the process dies, and it needs no stale-lock cleanup.
"""
import errno
import fcntl
import json
import os
import time
from collections import deque
from contextlib import contextmanager

# Requests allowed in any rolling window, across every caller. The wall was
# ~2,350 in one window; this leaves roughly half as headroom.
BUDGET = int(os.environ.get("PENNYRUN_BUDGET", "1500"))
WINDOW = int(os.environ.get("PENNYRUN_BUDGET_WINDOW", "3600"))


def _data_dir():
    # imported lazily: tools.sweep imports this module, so a top-level import
    # of sweep here would be circular
    from tools import sweep
    return sweep.DATA


def lock_path():
    return os.path.join(_data_dir(), "homedepot.lock")


def budget_path():
    return os.path.join(_data_dir(), "request_budget.json")


class Budget:
    """A rolling cap on requests to Home Depot, shared by every caller.

    Deliberately not a token bucket: what is being modelled is "how many
    requests has this address made recently", which is a count over a window.
    A bucket's refill would let a long quiet spell bank enough credit to fire
    exactly the burst that got us blocked.

    Persisted, because an in-memory window is not a budget -- `--once` from
    cron would start empty every invocation, and the crashes that reset it are
    the ones that follow heavy traffic.
    """

    def __init__(self, cap=BUDGET, window=WINDOW, clock=time.time, path=None):
        self.cap = cap
        self.window = window
        self._clock = clock
        self._spends = deque()
        self.path = path
        self._load()

    def _load(self):
        if not self.path or not os.path.exists(self.path):
            return
        try:
            with open(self.path) as f:
                for ts, n in json.load(f):
                    self._spends.append((float(ts), int(n)))
        except Exception:
            pass                       # a corrupt ledger must not stop a run
        self._prune(self._clock())

    def _save(self):
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump([[ts, n] for ts, n in self._spends], f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def _prune(self, now):
        while self._spends and self._spends[0][0] <= now - self.window:
            self._spends.popleft()

    def spent(self):
        now = self._clock()
        # re-read before answering: another caller may have spent while we
        # were doing something else, and a stale view of a shared budget is
        # the same as not sharing it
        if self.path:
            self._spends.clear()
            self._load()
        self._prune(now)
        return sum(n for _, n in self._spends)

    def remaining(self):
        return max(0, self.cap - self.spent())

    def record(self, n):
        if n <= 0:
            return
        if self.path:
            self._spends.clear()
            self._load()               # merge with whatever others recorded
        self._spends.append((self._clock(), n))
        self._save()

    def wait_for(self, n):
        """Seconds until `n` more requests fit under the cap. 0.0 if now."""
        self.spent()                   # refresh from disk and prune
        now = self._clock()
        if n >= self.cap:
            # the LAST spend, not the first: waiting only for the oldest would
            # admit an oversized job on top of nearly a full window
            return 0.0 if not self._spends else max(
                0.0, self._spends[-1][0] + self.window - now)
        running = sum(c for _, c in self._spends)
        if running + n <= self.cap:
            return 0.0
        freed, wait = 0, 0.0
        for ts, c in self._spends:
            freed += c
            wait = ts + self.window - now
            if running - freed + n <= self.cap:
                break
        return max(0.0, wait)


def shared_budget():
    return Budget(path=budget_path())


@contextmanager
def exclusive(what, blocking=True, timeout=None):
    """Hold the one Home Depot door for the duration of `what`.

    Blocking by default: a job that arrives during the nightly scan should
    wait its turn, not be dropped. Pass blocking=False where being second is
    a reason to give up rather than queue.
    """
    path = lock_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        if blocking and timeout is None:
            fcntl.flock(fh, fcntl.LOCK_EX)
        else:
            deadline = time.time() + (timeout or 0)
            while True:
                try:
                    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EAGAIN, errno.EACCES):
                        raise
                    if not blocking or time.time() >= deadline:
                        raise Busy("another Home Depot caller holds the gate")
                    time.sleep(0.5)
        fh.seek(0)
        fh.truncate()
        fh.write("%s pid=%d at=%d\n" % (what, os.getpid(), int(time.time())))
        fh.flush()
        yield
    finally:
        try:
            fcntl.flock(fh, fcntl.LOCK_UN)
        finally:
            fh.close()


class Busy(RuntimeError):
    """Someone else is talking to Home Depot right now."""
