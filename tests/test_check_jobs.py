"""The queue between a phone and the residential collector.

The two properties worth protecting with tests, because both are the kind that
look fine in a happy-path demo and cost real money later:

  Coalescing is atomic. Two users asking for the same store must produce ONE
  job, even racing, because the alternative is paying 81 requests twice for
  the same answer against an address that gets cut off at ~2,350.

  A failed job does not make its store fresh. Otherwise a blocked run marks the
  store done, everyone is served stale prices for FRESH_FOR, and nothing
  retries.
"""
import json
import os
import uuid

import pytest
from fastapi.testclient import TestClient

pytestmark = pytest.mark.skipif(not os.environ.get("PENNYRUN_DB_URL"),
                                reason="needs the test database")

TOKEN = "checks-test-token"
# S_FRESH gets a real observation written to it. `observation` is append-only,
# so that store is fresh for the rest of the session -- it must not be reused.
S1, S2, S3, S_FRESH = "2502", "2504", "2577", "2565"


@pytest.fixture(scope="module")
def queue_schema():
    """A schema of this file's own.

    These tests turn on whether a store is stale, and `observation` is
    append-only -- so one row written by any other test against the shared
    session schema makes that store fresh for the rest of the run and every
    assertion here fails for a reason that has nothing to do with the queue.
    """
    from tests.conftest import _create_schema, _drop_schema
    name = "pennyrun_checks_" + str(os.getpid())
    _create_schema(name)
    try:
        yield name
    finally:
        _drop_schema(name)


@pytest.fixture
def api(queue_schema, monkeypatch):
    """API client on this file's schema, with an empty queue every test.

    monkeypatch.setenv rather than os.environ -- pytest restores it, so this
    cannot clobber the module-scoped env another test file set up.
    """
    from db import migrate, seed
    conn = migrate.connect()
    with conn.cursor() as cur:
        cur.execute(f"set search_path to {queue_schema}")
    conn.commit()
    migrate.apply(conn)
    seed.stores(conn, "pennyrun/hd-stores.json")
    with conn.cursor() as cur:
        cur.execute("truncate check_watcher, collector_heartbeat")
        cur.execute("delete from check_job")
    conn.commit()

    monkeypatch.setenv("PENNYRUN_DB_SCHEMA", queue_schema)
    monkeypatch.setenv("PENNYRUN_INGEST_TOKEN", TOKEN)
    from api.main import app
    try:
        yield TestClient(app)
    finally:
        conn.close()


@pytest.fixture
def queue_conn(queue_schema):
    from db import migrate
    c = migrate.connect()
    with c.cursor() as cur:
        cur.execute(f"set search_path to {queue_schema}")
    c.commit()
    try:
        yield c
    finally:
        c.close()


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def ask(api, stores, device=None):
    body = {"store_ids": stores}
    if device:
        body["device_id"] = device
    return api.post("/api/v1/checks", json=body)


# ---------------------------------------------------------------- asking

def test_an_unknown_store_is_rejected(api):
    assert ask(api, ["NOPE"]).status_code == 400


def test_empty_or_oversized_asks_are_rejected(api):
    assert api.post("/api/v1/checks", json={"store_ids": []}).status_code == 400
    assert api.post("/api/v1/checks", json={}).status_code == 400
    assert ask(api, [S1] * 3 + [str(i) for i in range(2500, 2520)]).status_code == 400


def test_a_stale_store_is_queued(api):
    r = ask(api, [S1])
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] == []
    assert len(body["queued"]) == 1
    assert body["queued"][0]["store_id"] == S1
    assert body["queued"][0]["position"] == 1


def test_two_users_asking_for_one_store_share_a_single_job(api):
    """The whole economics of the design. Two asks, one sweep."""
    a = ask(api, [S1], device=str(uuid.uuid4())).json()
    b = ask(api, [S1], device=str(uuid.uuid4())).json()
    assert a["queued"][0]["job_id"] == b["queued"][0]["job_id"], \
        "a second ask for the same store created a second job"


def test_both_askers_are_recorded_as_watchers(api, queue_conn):
    d1, d2 = str(uuid.uuid4()), str(uuid.uuid4())
    job = ask(api, [S1], device=d1).json()["queued"][0]["job_id"]
    ask(api, [S1], device=d2)
    with queue_conn.cursor() as cur:
        cur.execute("select count(*) from check_watcher where job_id = %s", (job,))
        assert cur.fetchone()[0] == 2, "coalesced ask lost a watcher"


def test_a_fresh_store_is_answered_without_queueing(api, queue_conn):
    """A store the nightly sweep already covered must cost nothing."""
    with queue_conn.cursor() as cur:
        cur.execute("insert into product (item_id, name) values ('fresh1','x') "
                    "on conflict do nothing")
        cur.execute("insert into observation (item_id, store_id, clearance_price,"
                    " source, trusted) values ('fresh1', %s, 1.00, 'discovery', true)",
                    (S_FRESH,))
    queue_conn.commit()
    body = ask(api, [S_FRESH]).json()
    assert body["queued"] == []
    assert body["ready"] and body["ready"][0]["store_id"] == S_FRESH


def test_a_bad_device_id_is_rejected(api):
    r = api.post("/api/v1/checks", json={"store_ids": [S1], "device_id": "not-a-uuid"})
    assert r.status_code == 400


# ---------------------------------------------------------------- claiming

def test_claiming_requires_the_collector_token(api):
    assert api.get("/api/v1/checks/next").status_code == 401
    assert api.get("/api/v1/checks/next",
                   headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_a_claim_hands_out_each_job_once(api):
    ask(api, [S1])
    ask(api, [S2])
    first = api.get("/api/v1/checks/next", headers=AUTH).json()["job"]
    second = api.get("/api/v1/checks/next", headers=AUTH).json()["job"]
    third = api.get("/api/v1/checks/next", headers=AUTH).json()["job"]
    assert first and second
    assert first["job_id"] != second["job_id"], "the same job was claimed twice"
    assert third is None, "claimed a job that does not exist"


def test_a_running_job_is_not_re_queued_by_a_later_ask(api):
    """Coalescing has to cover running jobs too, not just queued ones --
    otherwise an ask during a 90-second sweep starts a second one."""
    job = ask(api, [S1]).json()["queued"][0]["job_id"]
    api.get("/api/v1/checks/next", headers=AUTH)          # now running
    again = ask(api, [S1]).json()
    assert again["queued"][0]["job_id"] == job


def test_polling_updates_the_heartbeat_even_with_an_empty_queue(api):
    """Silence has to be distinguishable from 'nothing to do'.

    The claim below happens with NOTHING queued -- an earlier version of this
    test queued a job first, so the claim took it and the empty-queue path
    (the entire reason collector_heartbeat exists) was never exercised.
    """
    empty = api.get("/api/v1/checks/next", headers=AUTH).json()
    assert empty["job"] is None, "the queue was not actually empty"
    assert ask(api, [S1]).json()["collector_online"] is True


# ---------------------------------------------------------------- finishing

def test_a_finished_job_makes_its_store_fresh(api):
    job = ask(api, [S1]).json()["queued"][0]["job_id"]
    api.get("/api/v1/checks/next", headers=AUTH)
    r = api.post(f"/api/v1/checks/{job}/done", json={"hits": 118, "refused": 0}, headers=AUTH)
    assert r.status_code == 200 and r.json()["state"] == "done"
    assert ask(api, [S1]).json()["queued"] == [], "a done store was queued again"


def test_a_failed_job_does_NOT_make_its_store_fresh(api):
    """Otherwise a blocked run serves stale prices for FRESH_FOR and never retries."""
    job = ask(api, [S1]).json()["queued"][0]["job_id"]
    api.get("/api/v1/checks/next", headers=AUTH)
    r = api.post(f"/api/v1/checks/{job}/done",
                 json={"failed": True, "hits": 0, "refused": 81,
                       "note": "circuit breaker"}, headers=AUTH)
    assert r.json()["state"] == "failed"
    again = ask(api, [S1]).json()
    assert again["queued"], "a failed job marked its store fresh"
    assert again["queued"][0]["job_id"] != job


def test_finishing_needs_the_token_and_a_running_job(api):
    job = ask(api, [S1]).json()["queued"][0]["job_id"]
    assert api.post(f"/api/v1/checks/{job}/done", json={}).status_code == 401
    # still queued, never claimed
    assert api.post(f"/api/v1/checks/{job}/done", json={}, headers=AUTH).status_code == 404


def test_the_report_says_how_many_devices_would_be_told(api):
    d1, d2 = str(uuid.uuid4()), str(uuid.uuid4())
    job = ask(api, [S1], device=d1).json()["queued"][0]["job_id"]
    ask(api, [S1], device=d2)
    api.get("/api/v1/checks/next", headers=AUTH)
    r = api.post(f"/api/v1/checks/{job}/done", json={"hits": 5}, headers=AUTH).json()
    assert r["watchers"] == 2


# ---------------------------------------------------------------- polling

def test_status_is_readable_without_a_token(api):
    """The app polls this; it must work whether or not push ever fires."""
    job = ask(api, [S1]).json()["queued"][0]["job_id"]
    r = api.get(f"/api/v1/checks/{job}")
    assert r.status_code == 200
    assert r.json()["state"] == "queued"
    assert "collector_online" in r.json()


def test_status_404s_on_an_unknown_job(api):
    assert api.get("/api/v1/checks/999999").status_code == 404
