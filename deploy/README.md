# Running Penny Run on your own droplet

The droplet serves the app **and** runs the sweep, which is simpler than
it sounds: the nightly job writes `clearance.json` straight into what the
web server is already serving. No GitHub runner, no commit-and-deploy
round trip, no waiting for Pages to rebuild.

## Before anything else: can the droplet talk to Home Depot?

GitHub's hosted runners are refused — the pricing API answers 206 to
their whole address range. DigitalOcean may or may not be. Ask from the
droplet:

```bash
curl -fsSL https://raw.githubusercontent.com/PlanetBandit/pennyrun/main/tools/checkhost.py | python3 -
```

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

It installs Caddy and Python, clones the repo to `/opt/pennyrun`, checks
whether the host can reach Home Depot, serves the app over HTTPS, and
installs two timers: the sweep nightly at 05:10 and the pool harvest on
Sundays at 04:10. Re-running it is safe.

Knobs, all optional:

| variable | default | |
|---|---|---|
| `PENNYRUN_HOST` | — | required, the hostname to serve |
| `PENNYRUN_DIR` | `/opt/pennyrun` | where the checkout lives |
| `PENNYRUN_DATA` | `/var/lib/pennyrun` | where sweep state lives |
| `PENNYRUN_HOUR` | `05` | local hour the sweep runs |
| `PENNYRUN_BRANCH` | `main` | branch to deploy |

## Day to day

```bash
sudo systemctl start pennyrun-sweep.service   # sweep right now
journalctl -u pennyrun-sweep -f               # watch it
systemctl list-timers 'pennyrun-*'            # when is the next one
sudo bash /opt/pennyrun/deploy/update.sh      # deploy a new build
```

## Why data lives outside the checkout

`PENNYRUN_DATA` (default `/var/lib/pennyrun`) holds `clearance.json`,
`hot.json`, `prices.json` and `cursor.json`. They are tracked files in
the repo, so if the sweep wrote them in place every `git pull` would
collide with last night's data. Keeping them out means a deploy is
always a clean fast-forward, and `update.sh` can `reset --hard` without
ever destroying a sweep.

The copies in `tools/` are the seed: the first run on an empty data
directory reads them, then never touches them again. Caddy serves
`/clearance.json` from the data directory and everything else from the
checkout.

`clearance.json` is written to a temp file and renamed into place, so a
phone fetching mid-sweep gets the old file whole rather than half of the
new one.

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
