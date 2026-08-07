# Running Penny Run on your own droplet

The droplet serves the app, the read/write API, and Postgres, and —
on any box Home Depot hasn't blocked — runs the sweep straight into that
same database. `deploy/setup.sh` is the **only** deploy path: it installs
and configures all of it in one idempotent run, including standing up the
database and starting the API service. There is no separate manual step
for the API; an earlier draft of this deploy split "serve the app" and
"stand up the API" into two procedures that could each write Caddy's main
config file and collide. They're one script now.

## Before anything else: can the droplet talk to Home Depot?

GitHub's hosted runners are refused — the pricing API answers 206 to
their whole address range. DigitalOcean may or may not be. Ask from the
droplet:

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

`GOOD` means everything below works. `BLOCKED` means the droplet can
still serve the site perfectly well, but the sweep has to run somewhere
else. Find out before you build on it.

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
4. Checks whether the host can reach Home Depot (`GOOD`/`BLOCKED` —
   the site and API still come up either way; see below).
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
8. Installs and starts `pennyrun-api.service`.
9. Installs three sweep timers: catalogue discovery nightly at 05:00, the
   clearance scan nightly at 05:10 (which also uploads to this box's own
   API, over `https://$PENNYRUN_HOST`, when a token is present — see
   below), and the pool harvest on Sundays at 04:10. Discovery and the
   scan are separate systemd units so a sitemap Home Depot is challenging
   tonight can't take the scan down with it, but both run under a shared
   `flock` (`/run/lock/pennyrun-sweep.lock`) so they still can't overlap
   — two sweeps hitting Home Depot from the same IP at once looks exactly
   like the burst that gets an address range refused.

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

`/root/.pennyrun-db.env` and `/etc/pennyrun/ingest.env` are not
knobs — their paths are fixed because `db/migrate.py`, `api/db.py` and
`deploy/pennyrun-api.service` all hardcode the first, and the second is
what `pennyrun-api.service` and `pennyrun-scan.service` both read.

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
app rewrite that switches it to the read API instead
(`deploy/pennyrun-api.service`, `GET /api/v1/store/{id}/clearance`) is a
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
