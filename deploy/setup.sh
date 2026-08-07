#!/usr/bin/env bash
# Stand Penny Run up on a fresh Ubuntu droplet: serve the app and the read/
# write API over HTTPS, run Postgres, and -- on any box Home Depot hasn't
# blocked -- sweep nightly straight into that same database.
#
#   sudo PENNYRUN_HOST=pennyrun.example.duckdns.org bash deploy/setup.sh
#
# This is the one deploy path. It installs Postgres, migrates and seeds
# it, installs and starts the API, configures Caddy in front of both the
# API and the static app, and installs the sweep timers. (An earlier
# version of this task described a second, manual path -- SSH in and run
# a block of commands by hand to stand up just the API. That path is
# superseded: everything it did, this script now does too, idempotently,
# alongside everything else.)
#
# Safe to re-run. It writes its own Caddy site file and its own systemd
# units, leaves an existing database credential alone, and leaves anything
# else on the box alone.

set -euo pipefail

HOST="${PENNYRUN_HOST:-}"
DIR="${PENNYRUN_DIR:-/opt/pennyrun}"
DATA="${PENNYRUN_DATA:-/var/lib/pennyrun}"
REPO="${PENNYRUN_REPO:-https://github.com/PlanetBandit/pennyrun.git}"
BRANCH="${PENNYRUN_BRANCH:-main}"
RUN_AS="${PENNYRUN_USER:-pennyrun}"
HOUR="${PENNYRUN_HOUR:-05}"      # local hour discover/scan run
DB_NAME="${PENNYRUN_DB_NAME:-pennyrun}"
DB_ROLE="${PENNYRUN_DB_ROLE:-pennyrun}"

# Fixed, not configurable: db/migrate.py's db_url() and api/db.py's rows()
# both fall back to reading PENNYRUN_DB_URL out of this exact path when the
# environment variable itself isn't set, so every systemd unit below that
# touches the database points an EnvironmentFile at this same literal path
# rather than a $DB_ENV knob that could drift from what the Python side
# hardcodes.
DB_ENV=/root/.pennyrun-db.env
INGEST_ENV=/etc/pennyrun/ingest.env
LOCK=/run/lock/pennyrun-sweep.lock

# How long discover/scan get to actually run once they're going, and the
# TimeoutStartSec each of their units needs as a result. These are not the
# same number: systemd starts a unit's timeout clock the moment ExecStart
# is invoked, which for both units is the moment `flock $LOCK` starts
# *blocking* -- not the moment the sweep itself begins. Whichever unit
# starts second can lose up to SWEEP_RUN_BUDGET seconds just waiting for
# the other to release the lock (each unit's own run is bounded at
# SWEEP_RUN_BUDGET, so that's the most it can ever wait), and then still
# needs a full SWEEP_RUN_BUDGET of its own once it acquires the lock. A
# TimeoutStartSec of just SWEEP_RUN_BUDGET would let systemd SIGTERM a
# scan that waited out a full discover run after only ~10 real minutes of
# work instead of the 90 it was sized for. SWEEP_LOCK_TIMEOUT = wait +
# run = SWEEP_RUN_BUDGET * 2, applied to both units symmetrically since
# either could in principle be the one waiting (a human firing one of
# them manually while the other is mid-run, not just the nightly order).
SWEEP_RUN_BUDGET=5400
SWEEP_LOCK_TIMEOUT=$((SWEEP_RUN_BUDGET * 2))

die() { echo "setup: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[ "$(id -u)" = "0" ] || die "run this with sudo"
[ -n "$HOST" ] || die "set PENNYRUN_HOST to the hostname you will serve from"

# The scan unit's upload target defaults to this box's own API (the
# droplet topology: this box serves and collects both) -- but Home Depot
# only ever answers a residential address (see the SWEEPABLE check
# below), so a droplet can never actually be the box that collects.
# PENNYRUN_API, set in this script's own environment, points the scan
# timer at a *different* droplet instead: the collector topology this
# architecture actually needs. See deploy/README.md, "Running the
# collector on a home box".
API_URL="${PENNYRUN_API:-https://$HOST}"

# ---------------------------------------------------------------- packages

say "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git python3 python3-venv postgresql sudo \
	debian-keyring debian-archive-keyring apt-transport-https

if ! command -v caddy >/dev/null; then
	say "installing caddy"
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
		| gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		> /etc/apt/sources.list.d/caddy-stable.list
	apt-get update -qq
	apt-get install -y -qq caddy
fi

# ---------------------------------------------------------------- the code

if ! id -u "$RUN_AS" >/dev/null 2>&1; then
	say "creating service user $RUN_AS"
	useradd --system --create-home --shell /usr/sbin/nologin "$RUN_AS"
fi

if [ -d "$DIR/.git" ]; then
	say "updating $DIR"
	git -C "$DIR" fetch --quiet origin "$BRANCH"
	git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
else
	say "cloning into $DIR"
	git clone --quiet --branch "$BRANCH" "$REPO" "$DIR"
fi
git config --global --add safe.directory "$DIR"

# curl_cffi (and everything else the sweep and the read API import) is not
# in the standard library, and nothing was ever installing it -- the
# sweep's systemd units used to point straight at /usr/bin/python3, which
# has never seen these packages. A venv, installed once here and reused by
# every unit below, is what makes `import curl_cffi` not fail at 5am.
say "python virtualenv"
if [ ! -x "$DIR/.venv/bin/python" ]; then
	python3 -m venv "$DIR/.venv"
fi
"$DIR/.venv/bin/pip" install -q --upgrade pip
# api/requirements.txt covers fastapi/uvicorn/psycopg -- what the API and
# db.migrate/db.seed need. curl_cffi (tools/requirements.txt) is the one
# runtime dependency the sweep needs on top of that. Installed explicitly
# rather than `-r tools/requirements.txt` wholesale so a deploy box doesn't
# also pull in pytest and httpx, which nothing here ever runs.
"$DIR/.venv/bin/pip" install -q -r "$DIR/api/requirements.txt" curl_cffi

# ------------------------------------------------------- retire old units

# Pre-Task-9 boxes have pennyrun-sweep.{service,timer}: one unit running
# `discover scan` together through /usr/bin/python3, which never had
# curl_cffi. Left alone it fails closed (an import error, not a bad
# sweep) but keeps firing a red unit every night at ${HOUR}:10 -- the same
# minute pennyrun-scan.timer now runs -- which is both noisy and exactly
# the double-hit-Home-Depot-at-once problem the new lock below exists to
# prevent. Remove it before installing anything new.
if [ -f /etc/systemd/system/pennyrun-sweep.timer ] || [ -f /etc/systemd/system/pennyrun-sweep.service ]; then
	say "retiring the old combined pennyrun-sweep unit"
	systemctl disable --now pennyrun-sweep.timer pennyrun-sweep.service >/dev/null 2>&1 || true
	rm -f /etc/systemd/system/pennyrun-sweep.timer /etc/systemd/system/pennyrun-sweep.service
fi

# Sweep state lives here, outside git, so a deploy is always a clean
# fast-forward and never a conflict with last night's data.
say "data directory $DATA"
mkdir -p "$DATA"
chown -R "$RUN_AS:$RUN_AS" "$DATA"
chmod 755 "$DATA"

if [ ! -f "$DATA/clearance.json" ]; then
	say "seeding the list from the checkout"
	install -o "$RUN_AS" -g "$RUN_AS" -m 644 \
		"$DIR/pennyrun/clearance.json" "$DATA/clearance.json"
fi

# ---------------------------------------------------------------- can it?

say "checking whether this machine can reach Home Depot"
# tools/checkhost.py exits 0 GOOD, 1 BLOCKED (they answered and refused --
# a datacentre address, not fixable from here), or 2 UNKNOWN (we never got
# a clean answer -- almost always a local network/TLS problem, not a Home
# Depot refusal). Capturing the real code, not just pass/fail, is what
# lets the message below tell those two apart instead of sending whoever
# is reading it down the wrong road.
CHECK_STATUS=0
(cd "$DIR" && "$DIR/.venv/bin/python" -m tools.checkhost) || CHECK_STATUS=$?
SWEEPABLE=yes
if [ "$CHECK_STATUS" != "0" ]; then
	SWEEPABLE=no
fi
if [ "$SWEEPABLE" = "no" ]; then
	echo
	if [ "$CHECK_STATUS" = "1" ]; then
		echo "    Home Depot refused this address (checkhost: BLOCKED). That is a"
		echo "    datacentre IP range being refused, not a header or user-agent"
		echo "    problem -- it will not start working by itself, and nothing in"
		echo "    this script can fix it. The sweep has to run from a residential"
		echo "    connection instead -- see deploy/README.md, 'Running the"
		echo "    collector on a home box'."
		echo
		echo "    The site will still serve. pennyrun-discover.timer,"
		echo "    pennyrun-scan.timer and pennyrun-harvest.timer are installed"
		echo "    below and will be disabled (even if an earlier run on this box"
		echo "    left them enabled) -- nothing here hits Home Depot from this box"
		echo "    until you explicitly run:"
		echo "        sudo systemctl enable --now pennyrun-discover.timer pennyrun-scan.timer pennyrun-harvest.timer"
	else
		echo "    checkhost could not tell (exit $CHECK_STATUS, UNKNOWN) -- most"
		echo "    likely a local network or TLS problem on this box, not a Home"
		echo "    Depot refusal. Fix that first, then check again:"
		echo "        $DIR/.venv/bin/python -m tools.checkhost"
		echo
		echo "    The site will still serve. The sweep timers are installed below"
		echo "    and left exactly as they currently are -- not newly enabled by"
		echo "    this run, but not disabled either: an UNKNOWN result is not a"
		echo "    confirmed refusal, and a transient blip must not silently kill a"
		echo "    collector that has been working every other night. If they were"
		echo "    never enabled before, enable them once you've confirmed GOOD:"
		echo "        sudo systemctl enable --now pennyrun-discover.timer pennyrun-scan.timer pennyrun-harvest.timer"
	fi
	echo
fi

# --------------------------------------------------------------- database

say "postgres role, database and credentials"
if [ ! -f "$DB_ENV" ]; then
	DB_PASS="$(openssl rand -hex 24)"
	sudo -u postgres psql -tAc "select 1 from pg_roles where rolname='$DB_ROLE'" \
		| grep -q 1 || sudo -u postgres psql -c \
		"create role \"$DB_ROLE\" login" >/dev/null
	# Unconditional, whether the role above was just created or already
	# existed (e.g. $DB_ENV was deleted by hand while the role survived).
	# Without this, a pre-existing role keeps its old password while a
	# fresh random one gets written to $DB_ENV below -- db.migrate fails
	# loudly right now (set -euo pipefail catches it), but the mismatched
	# file is left on disk, "already exists" on every later run, and the
	# next restart of pennyrun-api.service (a reboot, a crash, a manual
	# restart -- not necessarily today) reads the wrong password and
	# crash-loops with no self-repair. Setting the password here keeps
	# the role and the file it's about to be written into in sync at the
	# moment of writing, every time.
	sudo -u postgres psql -c \
		"alter role \"$DB_ROLE\" password '$DB_PASS'" >/dev/null
	sudo -u postgres psql -tAc "select 1 from pg_database where datname='$DB_NAME'" \
		| grep -q 1 || sudo -u postgres psql -c \
		"create database \"$DB_NAME\" owner \"$DB_ROLE\"" >/dev/null
	# umask, not a `>file` followed by a later chmod: the latter briefly
	# leaves the password readable at the process's default umask (022 ->
	# 644) between the write and the fixup. This never creates the file
	# world- or group-readable in the first place.
	(umask 077; printf 'PENNYRUN_DB_URL=postgresql://%s:%s@localhost/%s\n' \
		"$DB_ROLE" "$DB_PASS" "$DB_NAME" > "$DB_ENV")
else
	say "  $DB_ENV already exists -- leaving the database credential alone"
fi
chmod 600 "$DB_ENV"  # self-heal a file that predates this fix

say "applying migrations"
(cd "$DIR" && "$DIR/.venv/bin/python" -m db.migrate)
say "seeding stores and products"
(cd "$DIR" && "$DIR/.venv/bin/python" -m db.seed)

say "ingest token"
mkdir -p /etc/pennyrun
if [ ! -f "$INGEST_ENV" ]; then
	(umask 077; printf 'PENNYRUN_INGEST_TOKEN=%s\n' \
		"$(openssl rand -hex 24)" > "$INGEST_ENV")
else
	say "  $INGEST_ENV already exists -- leaving the ingest token alone"
fi
chmod 600 "$INGEST_ENV"  # self-heal a file that predates this fix

# ---------------------------------------------------------------- serving

say "configuring caddy for $HOST"
mkdir -p /etc/caddy/sites /var/log/caddy
chown caddy:caddy /var/log/caddy

sed -e "s|{\$PENNYRUN_HOST}|$HOST|g" \
    -e "s|{\$PENNYRUN_ROOT}|$DIR/pennyrun|g" \
    -e "s|{\$PENNYRUN_DATA}|$DATA|g" \
    "$DIR/deploy/Caddyfile" > /etc/caddy/sites/pennyrun.caddyfile

if ! grep -q "^import sites/\*" /etc/caddy/Caddyfile 2>/dev/null; then
	say "adding the site import to /etc/caddy/Caddyfile"
	printf '\nimport sites/*\n' >> /etc/caddy/Caddyfile
fi

CADDY_ERR=$(caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1) \
	|| die "caddy rejected the config; nothing was restarted:
$CADDY_ERR"

# `caddy validate` runs as root here and opens the log writer to check it,
# which CREATES /var/log/caddy/pennyrun.log owned by root:root 0600. The
# caddy service then runs as User=caddy and cannot write its own log, so it
# exits 1 with "permission denied" and the site never comes up -- with a
# Caddyfile that just validated cleanly, which makes it a confusing failure.
# Re-chown after validate, not only before it.
chown -R caddy:caddy /var/log/caddy

systemctl enable caddy >/dev/null
systemctl reload caddy 2>/dev/null || systemctl restart caddy

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
	say "opening 80 and 443"
	ufw allow 80/tcp >/dev/null
	ufw allow 443/tcp >/dev/null
fi

write_unit() { cat > "/etc/systemd/system/$1"; }

# ------------------------------------------------------------------ the API

# Generated fresh every run rather than `install`ed from a committed unit
# file, so it always tracks $DIR and $RUN_AS when either is overridden --
# a static file would hardcode /opt/pennyrun and the `pennyrun` user,
# which is only ever right at the defaults. (An earlier version of this
# script did ship a committed deploy/pennyrun-api.service alongside this
# generated one; it was never installed from and drifted out of sync
# with what this actually writes, so it was deleted rather than fixed.)
say "installing the API service"
write_unit pennyrun-api.service <<EOF
[Unit]
Description=Penny Run API
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR
EnvironmentFile=$DB_ENV
EnvironmentFile=$INGEST_ENV
ExecStart=$DIR/.venv/bin/uvicorn api.main:app --host 127.0.0.1 --port 8000
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now pennyrun-api.service >/dev/null

# ---------------------------------------------------------------- the sweep

say "installing timers (discover ${HOUR}:00, scan ${HOUR}:10, harvest Sundays 04:10)"

# discover() and scan() used to run as one unit ("discover scan" in a
# single ExecStart). discover() now die()s -- exits non-zero -- the moment
# Home Depot challenges the sitemap, which is a live, ordinary occurrence,
# not a bug; with both stages in one process that exit kills scan() too,
# even though scan() prices the pool against a completely different set of
# hosts and is unaffected by a blocked sitemap. Separate units mean a
# blocked discover() only means the hot list didn't grow tonight -- it can
# no longer take the whole night's data down with it.
#
# Separate units also means nothing stops them running at once, and that
# is the one thing this architecture cannot allow: two sweep processes at
# WORKERS=20 each, from one residential IP, hitting apionline.homedepot.com
# simultaneously looks exactly like the burst that gets a datacentre range
# refused (tools/README.md, "Where it can run"). discover fires at
# ${HOUR}:00; scan fires ten minutes later and could easily start while
# discover is still going. Both ExecStarts below run under `flock $LOCK`,
# which serialises them against each other: whichever starts second
# blocks (no -n) until the first releases the lock, rather than either
# failing outright or the two running concurrently. A plain `Conflicts=`
# was the other option here and was rejected -- it stops whichever unit
# is already running the moment the other one starts, throwing away a
# partially-completed discover or scan instead of letting it finish.
# TimeoutStartSec is SWEEP_LOCK_TIMEOUT (see above), not SWEEP_RUN_BUDGET
# -- it has to cover a worst-case wait for the lock *and* a full run once
# it's acquired, since systemd's timeout clock starts at `flock`, not at
# the sweep itself.
write_unit pennyrun-discover.service <<EOF
[Unit]
Description=Penny Run nightly catalogue discovery
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$DIR
Environment=PENNYRUN_DATA=$DATA
ExecStart=/usr/bin/flock $LOCK $DIR/.venv/bin/python -m tools.sweep discover
TimeoutStartSec=$SWEEP_LOCK_TIMEOUT
Nice=10
EOF

write_unit pennyrun-discover.timer <<EOF
[Unit]
Description=Walk the next slice of the sitemap before the doors open

[Timer]
OnCalendar=*-*-* ${HOUR}:00:00
Persistent=true
RandomizedDelaySec=120

[Install]
WantedBy=timers.target
EOF

# The scan unit's ingest credential always comes from a 0600
# EnvironmentFile -- one code path, one secret-handling rule, matching
# $DB_ENV/$INGEST_ENV just above (umask 077 at write time, chmod 600 to
# self-heal an older file; never `Environment=TOKEN=...` embedded
# straight in a unit, which write_unit's plain `cat >` leaves at the
# process umask -- 0644, world-readable -- with no chmod ever run on it).
# An explicit PENNYRUN_INGEST_TOKEN in this script's own environment
# means this box collects for a *different* droplet, so it gets written
# into its own file ($UPSTREAM_INGEST_ENV) rather than reused from
# $INGEST_ENV, which holds *this* box's own token for *its own* API.
# Prefixed with "-" either way because scan() already treats a missing
# token as "don't upload, just write clearance.json" (tested) -- a
# missing/unreadable token file must not stop the scan itself from
# running.
UPSTREAM_INGEST_ENV=/etc/pennyrun/upstream-ingest.env
if [ -n "${PENNYRUN_INGEST_TOKEN:-}" ]; then
	mkdir -p /etc/pennyrun
	(umask 077; printf 'PENNYRUN_INGEST_TOKEN=%s\n' \
		"$PENNYRUN_INGEST_TOKEN" > "$UPSTREAM_INGEST_ENV")
	chmod 600 "$UPSTREAM_INGEST_ENV"  # self-heal a file that predates this fix
	SCAN_INGEST_ENV="$UPSTREAM_INGEST_ENV"
else
	SCAN_INGEST_ENV="$INGEST_ENV"
fi
INGEST_LINE="EnvironmentFile=-$SCAN_INGEST_ENV"

write_unit pennyrun-scan.service <<EOF
[Unit]
Description=Penny Run nightly clearance scan
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$DIR
Environment=PENNYRUN_DATA=$DATA
Environment=PENNYRUN_OUT=$DATA/clearance.json
# A systemd unit does not inherit anyone's login environment -- setting
# PENNYRUN_API/PENNYRUN_INGEST_TOKEN in a shell profile on this box does
# nothing for this unit. Wired here instead: PENNYRUN_API (\$API_URL, set
# above) defaults to the host this box itself serves (its own API,
# through Caddy's reverse proxy) but honours an explicit PENNYRUN_API in
# this script's own environment -- see the comment above \$API_URL. The
# ingest credential line (\$INGEST_LINE, just above) follows the same
# rule for the token.
Environment=PENNYRUN_API=$API_URL
$INGEST_LINE
# The scan refuses to overwrite the list on a bad run, so a failure here
# leaves yesterday's data serving rather than emptying the app. It does
# not depend on pennyrun-discover.service succeeding first -- a blocked
# sitemap only means the hot list didn't grow tonight, not that the pool
# can't be priced.
ExecStart=/usr/bin/flock $LOCK $DIR/.venv/bin/python -m tools.sweep scan
# SWEEP_LOCK_TIMEOUT, not SWEEP_RUN_BUDGET -- see the comment above
# pennyrun-discover.service. Without it, a scan that waited out a full
# discover run gets SIGTERM'd after ~10 real minutes instead of the 90 it
# was sized for.
TimeoutStartSec=$SWEEP_LOCK_TIMEOUT
Nice=10
EOF

write_unit pennyrun-scan.timer <<EOF
[Unit]
Description=Price the pool and hot list before the doors open

[Timer]
OnCalendar=*-*-* ${HOUR}:10:00
Persistent=true
RandomizedDelaySec=300

[Install]
WantedBy=timers.target
EOF

write_unit pennyrun-harvest.service <<EOF
[Unit]
Description=Penny Run weekly product-pool harvest
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$DIR
Environment=PENNYRUN_DATA=$DATA
ExecStart=$DIR/.venv/bin/python -m tools.sweep harvest
TimeoutStartSec=3600
Nice=10
EOF

write_unit pennyrun-harvest.timer <<EOF
[Unit]
Description=Rebuild the product pool weekly

[Timer]
OnCalendar=Sun *-*-* 04:10:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
# All three sweep timers are gated on the SWEEPABLE check above -- a
# BLOCKED or UNKNOWN host would otherwise fire ~3,750 refused discover
# chunks, ~408 refused scan chunks, and 772 refused harvest requests
# (193 search terms x 4 pages, weekly) at Home Depot, forever, against a
# service whose terms are already being stretched, for runs that can
# never produce a hit. The units are always written -- so enabling them
# later, if the situation changes, is one command, not a re-run of this
# script -- but "enable" only ever fires on SWEEPABLE=yes.
#
# BLOCKED (CHECK_STATUS=1) additionally *disables* them if a previous
# run on this same box left them enabled -- a definite, durable answer
# ("this address is refused") must not leave last night's timers still
# firing just because this script only ever handled the enable
# direction before. UNKNOWN (CHECK_STATUS=2) never disables anything:
# it is not evidence of a refusal, just a failure to get a clean answer,
# and silently killing a collector that has been working every other
# night over one transient local blip would be worse than leaving it
# running through it.
SWEEP_TIMERS="pennyrun-discover.timer pennyrun-scan.timer pennyrun-harvest.timer"
if [ "$SWEEPABLE" = "yes" ]; then
	systemctl enable --now $SWEEP_TIMERS >/dev/null
	say "sweep timers enabled -- this host is sweepable"
elif [ "$CHECK_STATUS" = "1" ]; then
	systemctl disable --now $SWEEP_TIMERS >/dev/null 2>&1 || true
	say "sweep timers disabled -- this host is BLOCKED (see above)"
else
	say "sweep timers left exactly as they were -- this host's checkhost result is UNKNOWN, not a confirmed refusal (see above)"
fi

# ---------------------------------------------------------------- done

echo
say "serving https://$HOST"
echo "    the certificate takes a few seconds on the first request"
echo
if [ "$SWEEPABLE" = "no" ]; then
	echo "    enable the sweep    sudo systemctl enable --now $SWEEP_TIMERS"
fi
echo "    scan now        sudo systemctl start pennyrun-scan.service"
echo "    discover now    sudo systemctl start pennyrun-discover.service"
echo "    watch it        journalctl -u pennyrun-scan -f"
echo "    next run        systemctl list-timers 'pennyrun-*'"
echo "    deploy a build  sudo bash $DIR/deploy/update.sh"
echo
