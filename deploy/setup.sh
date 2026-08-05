#!/usr/bin/env bash
# Stand Penny Run up on a fresh Ubuntu droplet: serve the app over HTTPS
# and sweep Home Depot nightly straight into what is being served.
#
#   sudo PENNYRUN_HOST=pennyrun.example.duckdns.org bash deploy/setup.sh
#
# Safe to re-run. It writes its own Caddy site file and its own systemd
# units and leaves anything else on the box alone.

set -euo pipefail

HOST="${PENNYRUN_HOST:-}"
DIR="${PENNYRUN_DIR:-/opt/pennyrun}"
DATA="${PENNYRUN_DATA:-/var/lib/pennyrun}"
REPO="${PENNYRUN_REPO:-https://github.com/PlanetBandit/pennyrun.git}"
BRANCH="${PENNYRUN_BRANCH:-main}"
RUN_AS="${PENNYRUN_USER:-pennyrun}"
HOUR="${PENNYRUN_HOUR:-05}"      # local hour the sweep runs

die() { echo "setup: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[ "$(id -u)" = "0" ] || die "run this with sudo"
[ -n "$HOST" ] || die "set PENNYRUN_HOST to the hostname you will serve from"

# ---------------------------------------------------------------- packages

say "installing packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl git python3 debian-keyring debian-archive-keyring apt-transport-https

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
SWEEPABLE=yes
python3 "$DIR/tools/checkhost.py" || SWEEPABLE=no
if [ "$SWEEPABLE" = "no" ]; then
	echo
	echo "    The site will still serve. The nightly timer is installed and"
	echo "    will start working by itself the day this host stops being"
	echo "    refused -- nothing here needs redoing."
	echo
fi

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

caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile >/dev/null 2>&1 \
	|| die "caddy rejected the config; nothing was restarted"
systemctl enable caddy >/dev/null
systemctl reload caddy 2>/dev/null || systemctl restart caddy

if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
	say "opening 80 and 443"
	ufw allow 80/tcp >/dev/null
	ufw allow 443/tcp >/dev/null
fi

# ---------------------------------------------------------------- the sweep

say "installing timers (sweep ${HOUR}:10 daily, harvest Sundays 04:10)"

write_unit() { cat > "/etc/systemd/system/$1"; }

write_unit pennyrun-sweep.service <<EOF
[Unit]
Description=Penny Run nightly clearance sweep
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=$RUN_AS
WorkingDirectory=$DIR
Environment=PENNYRUN_DATA=$DATA
Environment=PENNYRUN_OUT=$DATA/clearance.json
# The scan refuses to overwrite the list on a bad run, so a failure here
# leaves yesterday's data serving rather than emptying the app.
ExecStart=/usr/bin/python3 $DIR/tools/sweep.py discover scan
TimeoutStartSec=5400
Nice=10
EOF

write_unit pennyrun-sweep.timer <<EOF
[Unit]
Description=Sweep Home Depot before the doors open

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
ExecStart=/usr/bin/python3 $DIR/tools/sweep.py harvest
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
systemctl enable --now pennyrun-sweep.timer pennyrun-harvest.timer >/dev/null

# ---------------------------------------------------------------- done

echo
say "serving https://$HOST"
echo "    the certificate takes a few seconds on the first request"
echo
echo "    sweep now       sudo systemctl start pennyrun-sweep.service"
echo "    watch it        journalctl -u pennyrun-sweep -f"
echo "    next run        systemctl list-timers 'pennyrun-*'"
echo "    deploy a build  sudo bash $DIR/deploy/update.sh"
echo
