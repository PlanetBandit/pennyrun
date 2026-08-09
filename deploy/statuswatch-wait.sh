#!/usr/bin/env bash
# Wait for the next sweep's unpriced rows to land, then report transitions.
#
# Fires on evidence, not on a clock: the sweep runs on a laptop that may be
# asleep at 05:10, and a report that ran anyway would say "no state changes"
# for the wrong reason -- indistinguishable from a real negative result.
set -uo pipefail
set -a; . /root/.pennyrun-db.env; set +a
cd /opt/pennyrun

CUTOFF="$1"                       # only rows newer than this count as a new run
OUT=/root/statuswatch-latest.txt
DEADLINE=$(( $(date +%s) + 30*3600 ))

count_new() {
  ./.venv/bin/python - "$CUTOFF" <<'PY' 2>/dev/null
import os, sys, psycopg
with psycopg.connect(os.environ["PENNYRUN_DB_URL"]) as c, c.cursor() as cur:
    cur.execute("select count(*) from observation"
                " where clearance_price is null and observed_at > %s", (sys.argv[1],))
    print(cur.fetchone()[0])
PY
}

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
	n=$(count_new)
	if [ -n "${n:-}" ] && [ "$n" -gt 2000 ]; then
		{
			echo "# statuswatch — $(date -u '+%Y-%m-%d %H:%M UTC')"
			echo "# triggered by $n unpriced rows newer than $CUTOFF"
			echo
			./.venv/bin/python -m tools.statuswatch
		} > "$OUT" 2>&1
		cp "$OUT" "/root/statuswatch-$(date -u +%Y%m%d).txt"
		exit 0
	fi
	sleep 600
done
{ echo "# statuswatch — timed out after 18h waiting for a full sweep newer than $CUTOFF"
  echo "# the collector laptop may have been asleep; nothing was reported."
} > "$OUT"
exit 1
