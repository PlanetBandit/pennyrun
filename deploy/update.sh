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
