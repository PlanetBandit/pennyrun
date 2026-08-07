# Queued area check — design

**Date:** 2026-08-07
**Status:** Draft for review
**Supersedes:** `2026-08-07-in-browser-checker-design.md`
**Goal:** a user picks their area, taps "check my stores", and gets fresh
clearance prices for those stores — usually instantly, and within minutes when
not.

---

## 1. Why the phone cannot do this

The previous design had the phone make the requests. It cannot. Measured in a
real browser from our own origin:

```
Cross-Origin Request Blocked ... (Reason: CORS header
'Access-Control-Allow-Origin' missing). Status code: 200.
```

The gateway requires `Content-Type: application/json` → forces a preflight →
Akamai's edge answers `OPTIONS` with **no CORS headers at all**. `GET` with query
params is 403'd at the edge; `text/plain` POSTs are 415'd by the gateway. There
is no preflight-free path.

**Consequence, and it is a good one:** every iOS constraint that shaped the
previous design disappears. No background-execution problem, no Auto-Lock
interruption, no wake-lock version floors, no IndexedDB checkpointing, no
resume-from-server reconciliation. The phone only ever talks to our own droplet,
which it can do freely.

It also removes the three decisions that were blocking: **the collector is
already trusted**, so there is no device identity to authenticate for writes, no
Sybil resistance to design, and no untrusted-row promotion path to build.

---

## 2. The idea that makes it cheap

**A job is per-store, not per-user.**

Two users in Baltimore who both pick Towson do not cause two sweeps. And a store
priced twenty minutes ago is not re-priced at all — the answer is already in
Postgres.

```
user asks for 5 stores
  → for each store, is its newest observation younger than FRESH_FOR?
      yes → answer immediately, zero requests
      no  → enqueue that store (or join the job already queued for it)
```

So cost scales with **distinct stale stores**, not with users. Once a metro is
covered, every additional user there is free.

```
81 requests per store   (1,288 candidates ÷ 16 per batch)
measured wall  ~2,350 requests per window
              →  ~29 distinct stores per window
```

Baltimore is 8 stores. One collector can cover roughly a metro and a half before
the address becomes the constraint.

---

## 3. Flow

```
1. app     POST /api/v1/checks {store_ids[], device_id}
2. droplet for each store: fresh? → return it now.
                           stale? → upsert a queued `check_job` row (one per store)
           responds { ready: [...], queued: [{store_id, job_id, position}] }
3. collector  GET /api/v1/checks/next        (bearer token, same as /discovery)
              claims one job, atomically
4. collector  prices the candidate list at that store, at its measured-safe pace,
              with the existing refusal circuit breaker
5. collector  POST /api/v1/discovery         (existing endpoint, trusted=true)
6. collector  POST /api/v1/checks/{id}/done  {hits, refused, duration}
7. droplet    fires web push to every device that asked for that store
8. app        shows results from GET /store/{id}/clearance  (existing endpoint)
```

Steps 4 and 5 are `tools/sweep.py` and `tools/upload.py` as they already exist —
including the pacing, the `SCAN_ABORT_THRESHOLD` circuit breaker, and the
Refused-vs-Unreachable distinction. **The collector needs a job-runner loop
around them, not new collection logic.**

---

## 4. Schema

One new table. `device` gains a purpose at last.

```sql
create table check_job (
  job_id       bigserial primary key,
  store_id     text not null references store,
  state        text not null default 'queued'
               check (state in ('queued','running','done','failed')),
  requested_at timestamptz not null default now(),
  claimed_at   timestamptz,
  finished_at  timestamptz,
  claimed_by   text,                  -- collector hostname, for debugging
  hits         integer,
  refused      integer,
  note         text
);
-- at most one live job per store: this is what coalesces two users into one sweep
create unique index check_job_live on check_job (store_id)
  where state in ('queued','running');
create index check_job_queue on check_job (state, requested_at);

create table check_watcher (          -- who to notify when a job finishes
  job_id    bigint references check_job,
  device_id uuid references device,
  primary key (job_id, device_id)
);
```

`observation` is unchanged — the collector writes through `/discovery` exactly as
it does tonight, `trusted=true`, append-only.

---

## 5. Endpoints

### `POST /api/v1/checks` — the app asks

Body `{store_ids: [...], device_id}`. Max 10 stores per request.

For each store, read `max(observed_at)` from `observation`. Fresh → include in
`ready`. Stale → `insert ... on conflict do nothing` against the partial unique
index, which makes coalescing atomic and race-free, then register the device in
`check_watcher`.

Response carries an **honest estimate**, or none at all:

```json
{ "ready":  ["2577","2504"],
  "queued": [{"store_id":"2565","job_id":91,"position":2}],
  "collector_seen": "2026-08-07T05:12:00Z" }
```

`collector_seen` is load-bearing. If the collector has not checked in recently,
the app must say *"the collector is offline — queued for when it's back"*, not
show a countdown that will not run down. **Never show a time estimate the system
cannot honour** — that is the same failure as the old code reporting
`stores_n` it had not covered.

### `GET /api/v1/checks/next` — the collector asks

Bearer token, the same `PENNYRUN_INGEST_TOKEN` already used by `/discovery`.
Claims one queued job atomically:

```sql
update check_job set state='running', claimed_at=now(), claimed_by=%s
 where job_id = (select job_id from check_job where state='queued'
                 order by requested_at limit 1 for update skip locked)
returning job_id, store_id;
```

`for update skip locked` makes it safe if a second collector is ever added.
Doubles as the heartbeat that feeds `collector_seen`.

### `POST /api/v1/checks/{id}/done`

Marks the job, records counts, and triggers push to every `check_watcher` device.
A job that reports a tripped circuit breaker is `failed`, not `done`, and its
store is **not** marked fresh.

### `POST /api/v1/push/subscribe`

Stores `{endpoint, p256dh, auth}` per device. Prune on `404`/`410` — iOS does not
implement `pushsubscriptionchange`, so a dead subscription is only discoverable
on send.

---

## 6. The collector's job loop

A new stage in `tools/sweep.py`, or a thin sibling: poll `/checks/next` on an
interval, run the store, report, repeat. Constraints, all of them already learned
the hard way:

- **One job at a time.** The existing `flock` in `deploy/setup.sh` and
  `collect.sh` must cover it — two sweeps at once from one address is the exact
  traffic that gets an address refused.
- **A global request budget across jobs, not per job.** Five back-to-back jobs is
  ~2,025 requests, close to the measured wall. The loop tracks a rolling window
  and pauses between jobs when it is running hot. This is new and is the single
  most important safety property of the design.
- **Nightly sweep takes priority.** It keeps the candidate list fresh, which is
  what makes a per-store job 81 requests instead of 587.
- **A tripped breaker stops the loop**, not just the job, and marks remaining
  queued jobs as still queued. Do not walk into a wall one job at a time.

---

## 7. What the user sees

| state | copy |
|---|---|
| all fresh | "Prices are current as of 20 minutes ago" — no job, instant |
| queued | "Checking Timonium — about 2 minutes. You can close this." |
| queued behind others | "2 stores ahead of you — about 5 minutes." |
| collector offline | "Queued. Your collector is offline — this will run when it's back." |
| done | push notification + results in the existing Checked tab |

**"You can close this" is now true**, which it never could have been in the
previous design. That single sentence is the whole reason this architecture is
better rather than merely possible.

---

## 8. Honest limits

- **One residential address is the ceiling.** ~29 distinct stores per window. Fine
  for a metro and a half; not a public national product on one home connection.
  When that binds, the options are more collector boxes, residential proxies
  (~$50–300/mo, lets the droplet collect directly), or a native app (no CORS, but
  Xcode, App Store, and $99/yr).
- **A home box that is asleep is a queue that does not drain.** The UI must say so
  rather than spin. A heartbeat is not optional.
- **Freshness is a product decision, not a technical one.** `FRESH_FOR` trades
  request budget against staleness. Markdowns cascade daily, so hours — not
  minutes — is probably right. Needs a number from a human.
- **Push still requires the PWA installed to the home screen** (iOS 16.4+), and
  the service worker's handler must wrap `showNotification()` in
  `event.waitUntil()` or iOS revokes the subscription after ~3 silent pushes.
  Pair it with an in-app status check on open so a missed push never loses a
  result.

---

## 9. What this deletes from the previous plan

Struck entirely: the IndexedDB checkpoint layer, resume-from-server
reconciliation, wake-lock handling, the paused/resume UI, per-item retry state,
client-side pacing and backoff, the browser HD client, `POST /observations`,
device authentication, Sybil resistance, and untrusted-row promotion.

That is most of the previous spec. The feature got simpler by moving one box on
the diagram.

---

## 10. Build order

1. `check_job` + `check_watcher` migration (`003_check_jobs.sql`).
2. `POST /checks`, `GET /checks/next`, `POST /checks/{id}/done` — with the
   coalescing unique index and `skip locked` claim.
3. Collector job loop with the **global rolling request budget**.
4. App: store picker → request → poll → results. No push yet; poll on open.
5. Push, once the rest is working end to end.

Steps 1–3 are testable without touching the app at all, against the existing
`pennyrun_test` database.

---

## 11. Open decision

**`FRESH_FOR`** — how old is too old? This sets the whole economics. My
suggestion is 6 hours during the day, on the grounds that Home Depot marks down
in the morning and the app's value is same-day; but this is a product call about
what "current" means to someone standing in an aisle.
