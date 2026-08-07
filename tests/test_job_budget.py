"""The rolling request budget shared across jobs.

This is the safety-critical piece of the collector loop. One job is ~81
requests, which is nothing. Five back-to-back is ~2,025, and this address was
cut off at roughly 2,350 in one window. Pacing each job on its own does not
help -- the cap has to be shared, or a busy hour walks into the wall one polite
job at a time.
"""
import pytest

# Budget lives in tools/gate.py now -- shared by sweep and jobs, because a
# per-feature cap is not a cap. Imported via jobs to also pin the alias.
from tools.gate import Budget
from tools import jobs


class Clock:
    """A hand-cranked clock, so these tests are exact rather than slow."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def budget(cap=1200, window=3600):
    c = Clock()
    return Budget(cap=cap, window=window, clock=c), c


def test_an_empty_budget_allows_work_immediately():
    b, _ = budget()
    assert b.wait_for(81) == 0.0
    assert b.remaining() == 1200


def test_spending_reduces_what_is_left():
    b, _ = budget()
    b.record(81)
    assert b.spent() == 81
    assert b.remaining() == 1119


def test_the_cap_is_shared_across_jobs_not_per_job():
    """The whole point. Fourteen stores fit; the fifteenth waits."""
    b, _ = budget()
    for _ in range(14):
        assert b.wait_for(81) == 0.0
        b.record(81)
    assert b.spent() == 1134
    assert b.wait_for(81) > 0.0, "a 15th job was allowed past the shared cap"


def test_spend_falls_out_of_the_window_and_frees_capacity():
    b, clock = budget()
    for _ in range(14):
        b.record(81)
    assert b.wait_for(81) > 0
    clock.advance(3601)
    assert b.spent() == 0
    assert b.wait_for(81) == 0.0


def test_the_wait_is_only_as_long_as_it_needs_to_be():
    """Waiting for the whole window when one old spend would do is a bug that
    looks like caution."""
    b, clock = budget(cap=100, window=100)
    b.record(60)            # at t=0
    clock.advance(50)
    b.record(30)            # at t=50, total 90
    # 20 more would be 110 > 100. The t=0 spend of 60 expires at t=100, i.e.
    # 50s from now -- and dropping it alone is enough.
    w = b.wait_for(20)
    assert 49.0 <= w <= 51.0, f"expected ~50s, got {w}"


def test_a_partial_expiry_is_enough_when_it_is_enough():
    b, clock = budget(cap=100, window=100)
    b.record(50)
    clock.advance(10)
    b.record(50)
    clock.advance(91)       # the first 50 has expired, the second has not
    assert b.spent() == 50
    assert b.wait_for(50) == 0.0


def test_a_job_bigger_than_the_whole_cap_does_not_deadlock_the_queue():
    """Cannot happen at 81 vs 1200, but a future caller with a bigger unit of
    work must not hang forever with no diagnosis."""
    b, clock = budget(cap=50, window=100)
    assert b.wait_for(500) == 0.0, "an oversized job deadlocked on an empty window"
    b.record(10)
    w = b.wait_for(500)
    assert 0 < w <= 100, "an oversized job should wait out the window, not forever"


def test_recording_zero_is_a_no_op():
    b, _ = budget()
    b.record(0)
    assert b.spent() == 0


# Measured 2026-08-07: this address was refused after roughly this many
# requests in one window. Named here so the assertion below is about the
# measurement rather than restating the constant it is checking.
MEASURED_WALL = 2350
REQUESTS_PER_STORE = 81


def test_the_default_cap_leaves_real_headroom_under_the_measured_wall():
    b = Budget(cap=Budget.__init__.__defaults__[0])   # the code's default, not the env's
    assert b.cap <= MEASURED_WALL * 0.7, (
        f"cap {b.cap} is within 30% of the wall at {MEASURED_WALL}")
    assert b.cap // REQUESTS_PER_STORE >= 10, "too tight to serve a metro"


def test_jobs_still_exposes_Budget_for_callers_that_import_it_there():
    assert jobs.Budget is Budget
