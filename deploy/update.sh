#!/usr/bin/env bash
# Deploy whatever is on the branch. Sweep data lives outside the checkout,
# so this is always a clean fast-forward with nothing to merge.
#
#   sudo bash /opt/pennyrun/deploy/update.sh

set -euo pipefail

DIR="${PENNYRUN_DIR:-/opt/pennyrun}"
BRANCH="${PENNYRUN_BRANCH:-main}"

[ "$(id -u)" = "0" ] || { echo "run this with sudo" >&2; exit 1; }
[ -d "$DIR/.git" ] || { echo "no checkout at $DIR -- run setup.sh first" >&2; exit 1; }

before=$(git -C "$DIR" rev-parse --short HEAD)
git -C "$DIR" fetch --quiet origin "$BRANCH"
git -C "$DIR" reset --hard --quiet "origin/$BRANCH"
after=$(git -C "$DIR" rev-parse --short HEAD)

if [ "$before" = "$after" ]; then
	echo "already on $after -- nothing to deploy"
else
	echo "deployed $before -> $after"
	git -C "$DIR" log --oneline "$before..$after" | sed 's/^/    /'
fi

# The site is served straight off the checkout, so there is nothing to
# copy. Reload only picks up Caddyfile changes that came with the pull.
if [ -f /etc/caddy/sites/pennyrun.caddyfile ]; then
	systemctl reload caddy 2>/dev/null || true
fi

# Schema before code, always: a migration that has not run is a column the
# new code is about to select. update.sh did not apply migrations at all,
# for the same reason it did not restart the API -- the site is served
# straight off the checkout, so "deploy" meant "git pull" and nothing else.
# A migration would have sat in the tree unapplied and the first symptom
# would have been the API erroring on a column that plainly exists in
# db/migrations.
if [ -x "$DIR/.venv/bin/python" ]; then
	# same credentials the service runs with, nothing new on disk
	set -a
	. /root/.pennyrun-db.env
	set +a
	applied=$(cd "$DIR" && ./.venv/bin/python -m db.migrate)
	echo "$applied" | sed 's/^/    /'
fi

# The API is a long-lived uvicorn process: it imported api/main.py at start
# and will not see a word of the file we just pulled. This script reloaded
# Caddy and stopped, so every deploy carrying an API change appeared to
# succeed -- new code on disk, old code answering -- and the only symptom
# was a 404 on an endpoint that plainly exists in the checkout.
if systemctl list-unit-files pennyrun-api.service >/dev/null 2>&1; then
	systemctl restart pennyrun-api
	# A restart that fails leaves the OLD process dead and nothing serving,
	# which is worse than a stale deploy and must not exit 0.
	sleep 2
	if ! systemctl is-active --quiet pennyrun-api; then
		echo "pennyrun-api failed to come back up after restart:" >&2
		systemctl status pennyrun-api --no-pager -n 20 >&2 || true
		exit 1
	fi
	echo "restarted pennyrun-api"
fi
