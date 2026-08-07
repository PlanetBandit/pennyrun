#!/usr/bin/env bash
# Collect Home Depot clearance prices and upload them to the droplet.
#
#   ./collect.sh              discover a slice, then price every store
#   ./collect.sh scan         skip discovery, just price the stores
#   ./collect.sh check        only ask whether this machine can reach them
#
# Run this from a RESIDENTIAL connection. Home Depot needs two things at
# once -- a browser-grade TLS fingerprint and a non-datacentre address --
# so this cannot run on the droplet. The droplet stores and serves; this
# machine collects. That split is the whole architecture.
#
# Config lives in .env.collector (gitignored, 0600): PENNYRUN_API and
# PENNYRUN_INGEST_TOKEN. Without them the sweep still runs and still
# writes its local list, it just has nowhere to send the results.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV=.venv/bin/python
CONF=.env.collector
DATA="${PENNYRUN_DATA:-.collector-data}"
LOCK="$DATA/collect.lock"   # derived, so PENNYRUN_DATA moves the lock with it
STAGE="${1:-full}"

say()  { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '    %s\n' "$*"; }
die()  { printf '\n\033[31mcollect: %s\033[0m\n' "$*" >&2; exit 1; }

[ -x "$VENV" ] || die "no virtualenv at $VENV
    python3 -m venv .venv && ./.venv/bin/pip install -r tools/requirements.txt"

mkdir -p "$DATA"

# One collector at a time. Two sweeps at 20 workers each from one address
# is exactly the traffic pattern that gets an address range refused, and
# this script is easy to run twice by accident.
#
# `mkdir` rather than flock(1): macOS does not ship flock, and mkdir is
# atomic on every POSIX filesystem. The pid file lets a run that was killed
# (Ctrl-C, closed terminal, reboot) be cleaned up instead of wedging the
# lock forever.
if ! mkdir "$LOCK" 2>/dev/null; then
	OWNER=$(cat "$LOCK/pid" 2>/dev/null || echo "")
	if [ -n "$OWNER" ] && kill -0 "$OWNER" 2>/dev/null; then
		die "another collect.sh is already running (pid $OWNER)"
	fi
	warn "clearing a stale lock from pid ${OWNER:-unknown}"
	rm -rf "$LOCK"
	mkdir "$LOCK" || die "could not take the lock at $LOCK"
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

if [ -f "$CONF" ]; then
	set -a; . "./$CONF"; set +a
fi
export PENNYRUN_DATA="$DATA"
export PENNYRUN_OUT="$DATA/clearance.json"

# ---------------------------------------------------------------- check

say "can this machine reach Home Depot?"
CHECK=0; "$VENV" -m tools.checkhost || CHECK=$?
case "$CHECK" in
	0) ;;
	1) die "this address is refused. Home Depot answers residential
    connections and refuses datacentre ranges -- a VPN pointed at a
    cloud exit will do this too. Turn it off and try again." ;;
	2) die "could not reach Home Depot at all. That is a local network
    or certificate problem, not a refusal -- see the detail above." ;;
	*) die "checkhost exited $CHECK" ;;
esac

[ "$STAGE" = "check" ] && { say "reachable. nothing else to do."; exit 0; }

if [ -n "${PENNYRUN_API:-}" ] && [ -n "${PENNYRUN_INGEST_TOKEN:-}" ]; then
	warn "uploading to $PENNYRUN_API"
else
	warn "no $CONF -- collecting locally, uploading nothing"
fi

# -------------------------------------------------------------- discover

# Discovery widens the net; the scan is what produces the list. Home Depot
# challenges the sitemap often enough that discovery failing is ordinary,
# and it must never take the scan down with it -- so it is allowed to fail.
if [ "$STAGE" = "full" ]; then
	say "discovering new products (this may be refused; that is survivable)"
	if "$VENV" -m tools.sweep discover; then
		warn "discovery finished"
	else
		warn "discovery was refused or failed -- continuing to the scan,"
		warn "which prices the existing pool and is unaffected by this."
	fi
fi

# ------------------------------------------------------------------ scan

say "pricing every store in ${PENNYRUN_STORES:-tools/stores.json}"
"$VENV" -m tools.sweep scan

say "done"
if [ -f "$PENNYRUN_OUT" ]; then
	"$VENV" - <<PY
import json
d = json.load(open("$PENNYRUN_OUT"))
print(f"    {d['total_hits']} clearance hits across {d['stores_n']} stores")
print(f"    local list: $PENNYRUN_OUT")
PY
fi
[ -n "${PENNYRUN_API:-}" ] && warn "live at ${PENNYRUN_API}/api/v1/store/2502/clearance"
exit 0
