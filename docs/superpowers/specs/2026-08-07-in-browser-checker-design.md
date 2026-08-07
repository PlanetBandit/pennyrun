# In-browser price checker — design

**Date:** 2026-08-07
**Status:** Draft. **Blocked on one measurement — see §0.**
**Goal:** a user picks their area, taps "check my stores", and their own phone
prices the candidate list at those stores against Home Depot, then contributes
the results to everyone.

---

## 0. The gate — verify before building anything

**Can a real browser call `apionline.homedepot.com` cross-origin?**

Evidence conflicts:

- **For:** a preflight `OPTIONS` returns `200` with `access-control-allow-origin: *`
  and `access-control-allow-headers` including `X-Experience-Name`; a `POST`
  carrying `Origin: https://penny.premofusa.shop` returns real clearance data.
- **Against:** `pennyrun/index.html:1942-1944` carries a comment from whoever
  wrote `loadHdStores()` stating that CORS preflight blocks the browser from
  calling Home Depot, which is why store lookup reads a bundled JSON file.

The measurement was made with `curl_cffi` impersonating Safari — **not a browser
enforcing CORS**. The comment may be stale, may refer to a different endpoint, or
may be right.

**Nothing in this document is worth building until a real browser console has
returned a price.** If it cannot, the phone cannot be the collector and the
architecture needs rethinking, not patching.

A second, softer gate: **`hdQuery`, `hdStores`, `hdClearance` and `stockAt` in
`index.html` have zero call sites.** They are unwired scaffolding, never invoked.
This is build-from-scratch, not extend-what-works.

---

## 1. Constraints, all measured

### iOS will not run this in the background. At all.

- No Background Sync and no Periodic Background Sync in WebKit.
- Service Workers are terminated within seconds of going idle; they cannot host
  a standing loop.
- A hidden page has its JS execution context **suspended**, not throttled.
  In-flight `fetch()` calls are not guaranteed to complete.
- **Auto-Lock interrupts a run even when the user does nothing wrong** — they
  watch the progress bar, the screen locks at 30 s–2 min, the job halts.
- Screen Wake Lock: iOS **16.4+** in a Safari tab, iOS **18.4+** in an installed
  PWA (WebKit bug 254545). It is released the instant the page goes hidden and
  cannot survive a manual lock-button press.

**Therefore "start it, close the window, we'll notify you" is not achievable on
iPhone.** The job runs only while foregrounded and visible. Interruption is the
common case, not the edge case.

### Home Depot

- Requires a browser-grade TLS fingerprint **and** a non-datacentre address.
  The phone satisfies both; the droplet satisfies neither.
- `products(itemIds:)` caps at **16** ids per request. `index.html`'s
  `hdClearance` uses the **singular** `product(itemId:)` query — at one item per
  request, 1,288 candidates × 5 stores is **6,440 requests ≈ 107 minutes**. The
  plural query is not an optimisation, it is the difference between a feature
  and a non-starter.
- A single address was cut off at roughly **2,350 requests** in one window, at a
  bulk pace of ~39 req/s.

### The app today

- No IndexedDB anywhere. `localStorage["pennyrun.v2"]` is the only store.
- No progress bar, spinner, or meter component exists. No fifth pane.
- `sw.js` ignores non-`GET` entirely — the paced POSTs pass through untouched.
  No interference, and no background-sync help either.

---

## 2. The job model — per-store chunks

At 16 ids per request and ~1 request/second:

```
1,288 candidates ÷ 16  =    81 requests per store  ≈ 81 seconds
5 stores               =   405 requests            ≈ 6.8 minutes
```

**The unit of work is one store, not one job.** ~81 seconds fits inside a single
foreground session, completes atomically, and an interruption costs at most one
store's partial progress.

The UI says *"Checking Towson — store 2 of 5"*, never a seven-minute bar. A user
may do one store and stop; coverage accumulates across sessions.

### Pacing and failure

- **1 request/second**, serial. Not concurrent. The pace *is* the rate limiting:
  405 requests at 1/s is 5.8× fewer requests than the measured wall, at 39× the
  politeness.
- Every request wrapped in `AbortController` with a **10–15 s timeout**. One hung
  request on in-store cellular would otherwise stall the remaining 400.
- Per-item state is `pending | done | failed`, not a single "last index" —
  retries need to be addressable individually.
- Retry with backoff, capped. A chunk refused twice is marked failed and skipped;
  the run continues.
- **If the refusal rate crosses ~25%, stop the run and say so.** Same reasoning
  as `SCAN_ABORT_THRESHOLD` in `tools/sweep.py`: continuing into a wall only
  deepens it. Distinguish refused (they said no) from unreachable (we have no
  signal) — the second is common in a steel-racked store and must not read as a
  block.

---

## 3. Checkpointing

**Write after every response, before starting the next request.** Never
accumulate in memory and flush at the end — the process can vanish between any
two lines with no warning, and Safari gives no reliable `pagehide` grace period.

IndexedDB, not `localStorage`: `localStorage` is synchronous and would block the
main thread on every write in a 1/s loop.

```
db: pennyrun-jobs
  store: job      { job_id, created_at, store_ids[], candidate_count, state }
  store: progress { job_id+store_id+chunk_index, state, attempts, updated_at }
  store: result   { job_id+item_id+store_id, row, posted }
```

Keep records small and flat. iOS's IndexedDB has a documented history of stalled
and silently-failing writes; do not build multi-store transactions that
correctness depends on.

**On resume, rebuild state from IndexedDB — never from in-memory variables.**
The WebProcess may have been discarded; iOS has no swap and reclaims aggressively.

**The server is the second checkpoint.** On resume the phone asks the droplet
what it has already received for this job and skips it. That survives total
IndexedDB loss, which is the one failure IndexedDB cannot self-recover from.

Installed PWAs are exempt from Safari's 7-day storage eviction; a Safari tab is
not. One more reason to steer users to Add to Home Screen.

---

## 4. UI

Lives as a state **within `pane-checked`**, which is already thematically
"prices at my stores". No new tab.

| state | copy |
|---|---|
| ready | "Check prices at your 5 stores — about 7 minutes" |
| running | "Checking Towson — store 2 of 5 · 41 of 81" + bar |
| paused | **"Paused — tap to resume"**, progress preserved |
| blocked | "Home Depot stopped answering. Try again in an hour." |
| done | "412 on clearance · 14 worth a look" → existing `.read` card pattern |

**"Paused — tap to resume" is the expected steady state, not an error path.** On
any iOS before 18.4, an installed PWA cannot hold a wake lock, so most runs will
pause at least once.

- Acquire Wake Lock on start; re-acquire on every `visibilitychange` → visible.
- Show a **count**, not a time estimate — retries and resume gaps make any ETA a
  lie.
- Reuse `.btn`, `.scan__hint`, `.pane__title`, and the `.read`/`showRead()` card.
  The one animation precedent is `.scan__laser`.

---

## 5. Server-side

### `GET /api/v1/candidates`

**`candidate` has no writer anywhere in the repo** — `penny_score` is null for
every row, so the API must not sort by it or pretend it exists. Derive live from
`observation` (aggregate `LATEST` by item, `max(pct_off)`, `count(distinct
store_id)`, `max(observed_at)`), ~180–220 bytes/row, **30–50 KB gzipped** for
today's catalogue.

**This is explicitly a stopgap.** Unlike `/store/{id}/clearance`, it has no store
filter to prune the index, so it walks the whole `obs_item_store_idx` per call —
fine today, a full-index scan at the projected 2.4M rows/year. It retires when
the nightly rollup that populates `candidate` gets built. Shipping it without
that follow-up is scaling debt with no ticket.

### `POST /api/v1/observations`

Separate from `/discovery`. Reusing `/discovery` is the natural shortcut and the
wrong one — it would put the trusted collector's shared secret into a PWA served
to every browser.

- **`source='phone'`, `trusted=false`, hardcoded server-side.** Never read from
  the client body. `LATEST` already filters `where o.trusted`, so phone rows are
  invisible to every read endpoint with **no other code change**.
- **Reject unknown `item_id` outright (400).** Phones verify; they do not
  discover. This one rule closes the worst abuse vector below.
- Reuse `api/validate.py:check()` unchanged — its bounds already cover both
  sources by design.
- Insert-only. `observation` is append-only by DB trigger.

### The catalog-poisoning vector — the sharpest risk in the design

`api/ingest.py:110-124`'s `INSERT_PRODUCT` uses `coalesce(excluded.x, product.x)`,
which is **"last non-null write wins", permanently, for every product** — not
"fill in if missing". `product` has **no trust column at all**; the trust model
only ever gated `observation`.

If `/observations` reuses that statement, an unauthenticated client can rewrite
any real product's `canonical_url` to a phishing link, or garbage its category
and model number, and it is served to every user by `/item/{id}` and
`/store/{id}/clearance`, which join `product` with no trust filter.

**`/observations` must not write catalog fields at all.** At most bump
`last_seen` on an already-known item.

### Promotion must be an INSERT, never an UPDATE

`002_append_only.sql` blocks `UPDATE` on `observation` unconditionally. Any future
corroboration cannot flip `trusted` in place — it inserts a **new** trusted row,
and `LATEST`'s `order by observed_at desc` surfaces it. The untrusted original
stays in the log as history. A naive `UPDATE ... SET trusted` will hard-fail.

### Rate limiting

None exists at either layer. Caddy's stock build has no `ratelimit` module —
adding it means an `xcaddy` build, an infra change to `deploy/setup.sh`, not a
config tweak. Limit on **`device_id`, not IP**: carrier NAT puts many phones
behind one address and one phone roams across several during a 7-minute run.

---

## 6. Push notifications — rescoped

Web Push works on iOS 16.4+, installed-PWA only, **no Apple Developer account or
APNs certificate** — plain VAPID via `pywebpush` from the existing FastAPI app.

**But it should not be used for job completion.** The job can only finish while
the user is watching the screen, so a "your check is done" push arrives at
someone already looking at the result.

Push's real value is the thing the user genuinely cannot be present for:
**"an item you're watching just dropped at your store"**, fired by the nightly
collector or by another user's check. That is the payoff of the flywheel. Build
it for that.

Non-negotiable when it is built:

- `Notification.requestPermission()` **must** be called synchronously from a tap.
  A denial is permanent and cannot be re-prompted from JavaScript.
- The service worker's `push` handler **must** wrap `showNotification()` in
  `event.waitUntil()`. Without it iOS counts a silent push and revokes the
  subscription after ~3.
- iOS does not implement `pushsubscriptionchange`. Dead subscriptions are only
  discoverable via `404`/`410` on send — prune then, and always pair push with an
  in-app status check on open so a missed push never loses a result.

---

## 7. Decisions needed from a human

These are product and security posture, not engineering detail. I am not
inventing answers.

1. **Device identity.** A `device_id` is a client-generated UUID with no
   proof-of-possession. Minting a million costs a loop. This means the spec's
   corroboration rule ("a second independent device agrees") is **not a security
   boundary** — an attacker satisfies it by running the same script twice.
   Options: accept corroboration as a soft weighted signal rather than a gate;
   proof-of-work or CAPTCHA on device creation; IP rate-limiting on device
   creation; or platform attestation (heavy, and in tension with the no-accounts,
   offline-first posture). **Pick one before corroboration is built.**
2. **Wire shape:** one batched POST per store (~81 rows), or streamed per chunk?
   The rate-limit design differs between them.
3. **Does the future `candidate` rollup read untrusted rows?** If it does, phone
   data distorts national ranking even while correctly staying off the per-store
   list. Deliberate decision, not an accident of whoever writes the query first.

---

## 8. Out of scope

- Penny prediction / `penny_score` — needs the nightly rollup and weeks of history.
- Corroboration, spot re-verification, trust decay — gated on decision 1.
- Android specifics. The constraints above are iOS; Android is more permissive
  and will be satisfied by the same design.
- Any change to the nightly home collector. It keeps the candidate list fresh,
  which is what makes a per-user check 7 minutes instead of 49.

---

## 9. Build order

1. **Verify §0.** Nothing else starts until a browser returns a price.
2. `GET /candidates` (live query, documented as a stopgap).
3. `POST /observations` — `trusted=false`, unknown `item_id` rejected, no catalog
   writes. Rate limit by device.
4. Client HD module: plural query, 16/batch, `AbortController`, pacing, backoff,
   refusal threshold.
5. IndexedDB checkpoint layer + resume-from-server reconciliation.
6. UI: per-store progress, paused/resume, wake lock.
7. Ship. Push and corroboration follow separately.
