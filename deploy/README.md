# Running Penny Run on your own droplet

**Two boxes, two jobs, and they are not interchangeable.** Home Depot's
pricing gateway only answers a request that has both a browser-grade TLS
fingerprint *and* a non-datacentre IP address (`tools/hdclient.py`) —
measured 4/4 from a residential connection and 0/4 from a DigitalOcean
droplet, even with the fingerprint right. So:

- **The droplet serves.** It runs the app, the read/write API, and
  Postgres, always. It **cannot collect** — Home Depot refuses it.
- **A home box collects.** It runs the sweep (`tools/sweep.py`) on a
  residential connection and uploads each run's rows to the droplet's
  `POST /api/v1/discovery`. It does not need to serve anything.

`deploy/setup.sh` provisions **the droplet's half** of this — the app,
the API, Postgres, and (only where `checkhost` finds Home Depot
reachable) the sweep timers pointed at whatever box runs them. It is the
one script for that job; there is no separate manual step for the API.
Which box you're running it on changes *how* you invoke it, not which
script you use for the serving side — see "Running the collector on a
home box" below for the other half.

## Before anything else: can this box talk to Home Depot?

Ask, on whichever box you're about to configure:

```bash
git clone --depth 1 https://github.com/PlanetBandit/pennyrun.git /tmp/pennyrun-check
cd /tmp/pennyrun-check
python3 -m venv .venv && .venv/bin/pip install -q -r tools/requirements.txt
.venv/bin/python -m tools.checkhost
```

(`curl ... | python3 -` cannot work here: `tools/checkhost.py` does
`from tools import hdclient`, a package-relative import that needs the
repo's directory layout on disk, and needs `curl_cffi` installed to speak
to Home Depot's pricing API at all. Piping the one file's text into a
stdin interpreter gives it neither.)

Three outcomes, not two — `checkhost` exits 0/1/2 and prints which:

- **`GOOD` (exit 0)** — Home Depot quotes prices from here. This is what
  a home box on a residential connection should see.
- **`BLOCKED` (exit 1)** — they answered and refused this address. Every
  datacentre range measured so far (GitHub Actions, DigitalOcean) gets
  this, unconditionally, no matter what the client looks like. It is not
  fixable from software on this box — the sweep has to run somewhere
  else. **This is the expected, permanent result on the droplet.**
- **`UNKNOWN` (exit 2)** — no clean answer came back at all. This is
  almost always a local problem on this box (missing root certificates on
  macOS, no network) — not a Home Depot refusal — and worth fixing and
  re-checking before assuming anything about whether the sweep can run
  here.

The site serves fine regardless of which of the three you get — only the
sweep timers care, and `deploy/setup.sh` installs them disabled (not
skipped) on anything but `GOOD`, printing the exact command to enable
them later if the situation changes. See "Can it?" below.

## HTTPS is not optional

iOS will not hand the camera to a page that is not a secure context. An
IP address alone will not do — you need a hostname with a certificate.
Caddy handles the certificate; you need to supply the name.

DigitalOcean gives you free **DNS hosting**, not a free domain. For a
genuinely free hostname:

- **DuckDNS** — `yourname.duckdns.org`, free, points at your droplet IP,
  and Let's Encrypt issues for it without complaint. The easy answer.
- `<ip>.nip.io` / `sslip.io` work with no signup, but the certificate
  authority rate-limits per registered domain and those two are heavily
  shared, so issuance can fail at the wrong moment.
- A real domain is a few dollars a year and avoids both caveats. Point
  its A record at the droplet and let DigitalOcean host the DNS.

Whatever you pick, its A record must resolve to the droplet **before**
you run setup, or the certificate request fails.

## Setup

```bash
ssh root@your-droplet
git clone https://github.com/PlanetBandit/pennyrun.git /tmp/pennyrun
sudo PENNYRUN_HOST=pennyrun.example.duckdns.org bash /tmp/pennyrun/deploy/setup.sh
```

It, in order:

1. Installs Caddy, Python, `python3-venv` and **Postgres**, and clones the
   repo to `/opt/pennyrun`.
2. Creates a virtualenv and installs `api/requirements.txt` plus
   `curl_cffi` into it (not `tools/requirements.txt` wholesale — that
   would also pull in `pytest`/`httpx`, which nothing on a deploy box
   ever runs).
3. Retires `pennyrun-sweep.{service,timer}` if this box was set up by an
   older version of this script — that combined unit predates the venv
   and points at `/usr/bin/python3`, which never had `curl_cffi`.
4. Checks whether the host can reach Home Depot (`GOOD`/`BLOCKED`/
   `UNKNOWN` — the site and API come up regardless; see above).
5. **Creates the `pennyrun` Postgres role and database** (a random
   password, generated once and left alone on every re-run) and writes
   `/root/.pennyrun-db.env`, mode `600`. Runs `db.migrate` then `db.seed`
   — both idempotent, safe on every run.
6. Writes `/etc/pennyrun/ingest.env` with a random
   `PENNYRUN_INGEST_TOKEN` (once; left alone after), mode `600`.
7. Configures Caddy: `/api/*` reverse-proxies to the API,
   `/clearance.json` still serves the live file from `$PENNYRUN_DATA` (the
   app fetches it directly today — see "Why data lives outside the
   checkout" below), everything else is the static app.
8. Installs and starts `pennyrun-api.service` (its unit file is
   generated fresh by the script each run, not the committed
   `deploy/pennyrun-api.service` — that file no longer exists; it was an
   orphan nothing kept in sync with `$DIR`/`$RUN_AS`).
9. Writes three sweep timers — catalogue discovery nightly at 05:00, the
   clearance scan nightly at 05:10 (which uploads to `PENNYRUN_API`, over
   `https://$PENNYRUN_HOST` by default — see "Running the collector on a
   home box" for pointing it elsewhere — when a token is present), and
   the pool harvest on Sundays at 04:10. Discovery and the scan are
   separate systemd units so a sitemap Home Depot is challenging tonight
   can't take the scan down with it, and both run under a shared `flock`
   (`/run/lock/pennyrun-sweep.lock`) so they still can't overlap — two
   sweeps hitting Home Depot from the same IP at once looks exactly like
   the burst that gets an address range refused. **Discovery and the scan
   are only *enabled* when step 4 found `GOOD`** — installed either way,
   so turning them on later (the check result changed, or you're pointing
   this run's units at a different box's collection some other way) is
   `sudo systemctl enable --now pennyrun-discover.timer
   pennyrun-scan.timer`, not a re-run of this script. The harvest timer
   is unaffected either way; it isn't gated.

Re-running the whole script is safe: packages, the venv, the database
role/credential, the ingest token, and every systemd unit are all
written idempotently or left alone if already present.

Knobs, all optional:

| variable | default | |
|---|---|---|
| `PENNYRUN_HOST` | — | required, the hostname to serve |
| `PENNYRUN_DIR` | `/opt/pennyrun` | where the checkout lives |
| `PENNYRUN_DATA` | `/var/lib/pennyrun` | where sweep state lives |
| `PENNYRUN_HOUR` | `05` | local hour discover/scan run |
| `PENNYRUN_BRANCH` | `main` | branch to deploy |
| `PENNYRUN_USER` | `pennyrun` | the service account everything runs as |
| `PENNYRUN_DB_NAME` | `pennyrun` | Postgres database name |
| `PENNYRUN_DB_ROLE` | `pennyrun` | Postgres role name |
| `PENNYRUN_API` | `https://$PENNYRUN_HOST` | where the scan timer uploads to — override to point a collector at a *different* droplet |
| `PENNYRUN_INGEST_TOKEN` | this run's generated token | the token the scan timer authenticates with — override with a token copied from the target droplet's `/etc/pennyrun/ingest.env` |

`PENNYRUN_API`/`PENNYRUN_INGEST_TOKEN` are the two knobs that make the
collector/server split possible without a second script — see the next
section.

## Running the collector on a home box

Home Depot's pricing gateway is only ever reachable from a residential
connection ("Before anything else", above) — the droplet can serve but
can never collect, so a second, physically different machine has to run
`tools.sweep` and upload what it finds. There are two ways to set that
box up, in order of how much it's worth building:

**The lighter path (recommended).** The collector doesn't need Postgres,
Caddy, or the API — it only needs `tools/sweep.py` and a way to run it
nightly with `PENNYRUN_API`/`PENNYRUN_INGEST_TOKEN` set:

```bash
git clone https://github.com/PlanetBandit/pennyrun.git ~/pennyrun
cd ~/pennyrun
python3 -m venv .venv && .venv/bin/pip install -q -r tools/requirements.txt
.venv/bin/python -m tools.checkhost   # confirm GOOD before scheduling anything

export PENNYRUN_API=https://pennyrun.example.duckdns.org
export PENNYRUN_INGEST_TOKEN=$(cat /path/to/copied/ingest.env | cut -d= -f2)  # from the droplet, see below
.venv/bin/python -m tools.sweep discover scan
```

Copy the token off the droplet once (`sudo cat /etc/pennyrun/ingest.env`
over SSH, or `scp`) — it's the same token `pennyrun-api.service` there
requires of every ingest request. Wire the two `export`s and the sweep
command into a cron entry or a small systemd user timer; a systemd unit
does not inherit a login shell, so if you use one, set
`Environment=PENNYRUN_API=...` / `Environment=PENNYRUN_INGEST_TOKEN=...`
in the unit itself (`env PENNYRUN_API=... PENNYRUN_INGEST_TOKEN=...` in a
crontab line works the same way) rather than relying on `~/.bashrc`.

**The full path.** Run `deploy/setup.sh` on the home box too, with
`PENNYRUN_API` and `PENNYRUN_INGEST_TOKEN` set in its own environment:

```bash
sudo PENNYRUN_HOST=whatever-this-box-calls-itself \
     PENNYRUN_API=https://pennyrun.example.duckdns.org \
     PENNYRUN_INGEST_TOKEN=<copied from the droplet's /etc/pennyrun/ingest.env> \
     bash deploy/setup.sh
```

This also stands up a local Postgres, API and Caddy site on the home
box — none of which the collector role needs — so it's more moving parts
than the lighter path above for the same result. It exists because
`setup.sh` is still the one place that installs the venv, the systemd
timers, and the `flock` serialisation between discover and scan
correctly; use it if you'd rather not hand-roll those, and don't mind
the extra services it brings along. Either way, `PENNYRUN_HOST` is still
required (Caddy needs a name to request a certificate for, even on a box
that's mostly idle), and step 4's `checkhost` run on *this* box should
say `GOOD` — if it says `BLOCKED`, this box is on a datacentre range too
and can't collect either.

`/root/.pennyrun-db.env` and `/etc/pennyrun/ingest.env` are not
knobs — their paths are fixed because `db/migrate.py`, `api/db.py` and
the `pennyrun-api.service` unit `deploy/setup.sh` generates all hardcode
the first, and the second is what that same unit and
`pennyrun-scan.service` both read.

## Day to day

```bash
sudo systemctl start pennyrun-scan.service      # scan right now
sudo systemctl start pennyrun-discover.service  # discover right now
journalctl -u pennyrun-scan -f                  # watch the scan
systemctl list-timers 'pennyrun-*'              # when is the next one
sudo bash /opt/pennyrun/deploy/update.sh        # deploy a new build
```

## Why data lives outside the checkout

`PENNYRUN_DATA` (default `/var/lib/pennyrun`) holds `clearance.json`,
`hot.json`, `prices.json` and `cursor.json`. They are tracked files in
the repo, so if the sweep wrote them in place every `git pull` would
collide with last night's data. Keeping them out means a deploy is
always a clean fast-forward, and `update.sh` can `reset --hard` without
ever destroying a sweep.

The copies in `tools/` are the seed: the first run on an empty data
directory reads them, then never touches them again.

`clearance.json` is written to a temp file and renamed into place, so a
concurrent reader of the data directory gets the old file whole rather
than half of the new one.

`pennyrun/index.html` still fetches `/clearance.json` directly — the full
app rewrite that switches it to the read API instead (the
`pennyrun-api.service` unit, `GET /api/v1/store/{id}/clearance`) is a
later plan, not done yet. **The Caddyfile serves both at once on
purpose**: `/clearance.json` from `$PENNYRUN_DATA` (this box's own sweep,
or the seed copy if it hasn't swept yet) so the app keeps working today,
and `/api/*` so the write path (and, later, the read path once the app
catches up) works too. Do not remove the `/clearance.json` handler before
the app itself stops asking for it — a machine still serving the app from
the checked-in seed file with no way to ever refresh it is worse than not
having the API route at all.

## Moving off GitHub Pages

Two things worth knowing before you switch the phone over:

**Your saved items will not come with you.** They live in the browser's
local storage, which is scoped to the origin. A different hostname is a
different origin, so the new install starts empty. If there is anything
in the current one you care about, note it down first.

**Install it fresh.** The old Pages install keeps its own service worker
and cached build. Remove that icon from the home screen, open the new
hostname in Safari, and add it again from there — otherwise you end up
with two Penny Runs and no way to tell which is which.

Nothing stops you leaving Pages up as a fallback. The Pages deploy still
runs on push and would keep serving the last `clearance.json` that was
committed — stale, but a working app if the droplet is ever down.
