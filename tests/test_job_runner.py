"""The collector loop, which had no coverage and held every serious defect.

The design's honesty rule is "never claim a store is fresh unless it is". That
rule is enforced in SQL and was then broken in three separate places here: an
all-unreachable job reported `done`, a failed upload reported `done`, and any
unexpected exception left the job `running` forever with the store locked out.

Every test in this file pins one of those.
"""
import pytest

from tools import hdclient, jobs, sweep


class Recorder:
    """Stands in for the droplet. Records what the collector reported."""

    def __init__(self):
        self.reports = []
        self.uploaded = []

    def report(self, base, token, job_id, hits, refused, failed=False, note=None):
        self.reports.append({"job_id": job_id, "hits": hits, "refused": refused,
                             "failed": failed, "note": note})
        return {"ok": True}

    def send(self, rows, base, token):
        self.uploaded.append(len(rows))
        return {"accepted": len(rows), "rejected": 0}


@pytest.fixture
def rig(monkeypatch, tmp_path):
    r = Recorder()
    monkeypatch.setattr(jobs, "report", r.report)
    monkeypatch.setattr(jobs.upload, "send", r.send)
    monkeypatch.setattr(jobs, "candidates",
                        lambda: ([f"i{n}" for n in range(32)], {}))
    monkeypatch.setattr(jobs.sweep, "previous_prices", lambda: {})
    return r


JOB = {"job_id": 7, "store_id": "2502"}


def last(r):
    assert r.reports, "the collector never reported -- the job is stranded"
    return r.reports[-1]


# -------------------------------------------------- C1: unreachable != refused

def test_an_all_unreachable_store_is_NOT_reported_done(rig, monkeypatch):
    """The defect: refused_rate is 0.0 when every chunk is unreachable, so the
    breaker never tripped and the job reported done -- marking a store fresh
    for six hours that was never contacted."""
    monkeypatch.setattr(jobs.sweep, "price_at",
                        lambda *a, **k: ([], 0.0, 1.0))   # all unreachable
    keep = jobs.run_one(JOB, jobs.Budget(), "http://x", "t")
    assert last(rig)["failed"] is True, "an unreachable store was marked checked"
    assert "unreachable" in (last(rig)["note"] or "")
    assert keep is True, "our own network dropping should not stop the loop"


def test_a_refused_store_is_reported_failed_and_stops_the_loop(rig, monkeypatch):
    monkeypatch.setattr(jobs.sweep, "price_at",
                        lambda *a, **k: ([], 1.0, 0.0))   # all refused
    keep = jobs.run_one(JOB, jobs.Budget(), "http://x", "t")
    assert last(rig)["failed"] is True
    assert keep is False, "kept going after being refused -- that deepens a block"


# ---------------------------------------------- C2: done means the data landed

def test_a_failed_upload_does_NOT_mark_the_store_checked(rig, monkeypatch):
    """Otherwise the user is told their prices are current and served the old
    ones -- a freshness claim with nothing behind it."""
    rows = [["n", "i1", "c", 1.0, 2.0, 50, "2502", 1, 0, "/p", "u", "s", "m", 0, None]]
    monkeypatch.setattr(jobs.sweep, "price_at", lambda *a, **k: (rows, 0.0, 0.0))

    def boom(rows_, base, token):
        raise jobs.upload.UploadError({"accepted": 3, "rejected": 0}, "connection reset")
    monkeypatch.setattr(jobs.upload, "send", boom)

    jobs.run_one(JOB, jobs.Budget(), "http://x", "t")
    assert last(rig)["failed"] is True, "a store whose prices never landed was marked checked"


def test_a_clean_run_is_reported_done(rig, monkeypatch):
    rows = [["n", "i1", "c", 1.0, 2.0, 50, "2502", 1, 0, "/p", "u", "s", "m", 0, None]]
    monkeypatch.setattr(jobs.sweep, "price_at", lambda *a, **k: (rows, 0.0, 0.0))
    keep = jobs.run_one(JOB, jobs.Budget(), "http://x", "t")
    assert last(rig)["failed"] is False
    assert last(rig)["hits"] == 1
    assert rig.uploaded == [1]
    assert keep is True


def test_a_store_with_nothing_on_clearance_is_still_a_real_check(rig, monkeypatch):
    """Zero hits is a legitimate answer, not a failure -- do not re-queue it."""
    monkeypatch.setattr(jobs.sweep, "price_at", lambda *a, **k: ([], 0.0, 0.0))
    jobs.run_one(JOB, jobs.Budget(), "http://x", "t")
    assert last(rig)["failed"] is False
    assert last(rig)["hits"] == 0


# ------------------------------------------- C4: never strand a running job

def test_an_unexpected_exception_still_reports_so_the_store_is_not_stranded(rig, monkeypatch):
    """check_job_live is partial on ('queued','running'), so a job left running
    locks its store out of the queue permanently."""
    def explode(*a, **k):
        raise KeyError("something nobody anticipated")
    monkeypatch.setattr(jobs.sweep, "price_at", explode)
    keep = jobs.run_one(JOB, jobs.Budget(), "http://x", "t")
    assert last(rig)["failed"] is True, "an unexpected error stranded the job"
    assert "KeyError" in (last(rig)["note"] or "")
    assert keep is False


def test_an_empty_hot_list_reports_rather_than_stranding(rig, monkeypatch):
    monkeypatch.setattr(jobs, "candidates", lambda: ([], {}))
    jobs.run_one(JOB, jobs.Budget(), "http://x", "t")
    assert last(rig)["failed"] is True
    assert "hot list" in (last(rig)["note"] or "")


# ------------------------------------------------- I2: always spend the budget

def test_requests_are_charged_even_when_the_store_fails(rig, monkeypatch):
    """They were made against the address whether or not the job succeeded, and
    a budget that only counts successes is not a budget."""
    def explode(*a, **k):
        raise RuntimeError("mid-store")
    monkeypatch.setattr(jobs.sweep, "price_at", explode)
    b = jobs.Budget()
    jobs.run_one(JOB, b, "http://x", "t")
    assert b.spent() == sweep.chunks_for(32), "requests were made and not charged"


def test_requests_are_charged_on_a_clean_run(rig, monkeypatch):
    monkeypatch.setattr(jobs.sweep, "price_at", lambda *a, **k: ([], 0.0, 0.0))
    b = jobs.Budget()
    jobs.run_one(JOB, b, "http://x", "t")
    assert b.spent() == sweep.chunks_for(32)


# ------------------------------------------------- I1: the budget survives exit

def test_the_budget_survives_a_restart(tmp_path):
    """--once from cron, and every crash, used to start from an empty window --
    and the crashes follow heavy traffic."""
    path = str(tmp_path / "budget.json")
    a = jobs.Budget(path=path)
    a.record(400)
    b = jobs.Budget(path=path)
    assert b.spent() == 400, "a restarted collector forgot what it had spent"


def test_a_corrupt_budget_ledger_does_not_stop_the_run(tmp_path):
    path = tmp_path / "budget.json"
    path.write_text("{not json at all")
    b = jobs.Budget(path=str(path))
    assert b.spent() == 0
    assert b.wait_for(81) == 0.0
