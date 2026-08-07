#!/usr/bin/env bash
# Schedule the collector on a Mac.
#
#   bash deploy/install-launchd.sh            # install and start
#   bash deploy/install-launchd.sh --uninstall
#
# Two jobs, because they answer different questions:
#
#   pennyrun.sweep   nightly, 05:10. Walks a slice of the catalogue and
#                    prices every configured store. This is what keeps the
#                    candidate list fresh -- and the candidate list is why an
#                    on-demand store check is ~81 requests instead of ~587.
#
#   pennyrun.jobs    every 5 minutes. Takes at most ONE queued check per run.
#                    People ask for their stores during the day, not at 5am.
#
# Both are user agents, not daemons: they run as you, only while you are
# logged in, and they need no root. A Mac that is asleep runs nothing -- and
# that is visible rather than silent, because the droplet reports the
# collector as offline and the app says so instead of showing a countdown.

set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
ROOT="$PWD"

LABEL_SWEEP=local.pennyrun.sweep
LABEL_JOBS=local.pennyrun.jobs
AGENTS="$HOME/Library/LaunchAgents"
LOGS="$ROOT/.collector-data/logs"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\n\033[31minstall-launchd: %s\033[0m\n' "$*" >&2; exit 1; }

unload() {
  for L in "$LABEL_SWEEP" "$LABEL_JOBS"; do
    launchctl bootout "gui/$(id -u)/$L" 2>/dev/null || true
    launchctl unload -w "$AGENTS/$L.plist" 2>/dev/null || true
  done
}

if [ "${1:-}" = "--uninstall" ]; then
  say "removing the launch agents"
  unload
  rm -f "$AGENTS/$LABEL_SWEEP.plist" "$AGENTS/$LABEL_JOBS.plist"
  echo "    gone. Logs and sweep state under .collector-data/ are left alone."
  exit 0
fi

# ---------------------------------------------------------------- checks

[ -x "$ROOT/.venv/bin/python" ] || die "no virtualenv at .venv
    python3 -m venv .venv && ./.venv/bin/pip install -r tools/requirements.txt"
[ -f "$ROOT/.env.collector" ] || die "no .env.collector -- the scheduled runs
    have no login environment and would have nowhere to read PENNYRUN_API
    and PENNYRUN_INGEST_TOKEN from"
[ -x "$ROOT/collect.sh" ] || die "collect.sh is missing or not executable"

# macOS gates ~/Documents, ~/Desktop and ~/Downloads behind TCC. A launch
# agent gets no consent prompt -- it simply cannot read files there, and
# fails silently: an empty log, a process that hangs, and a queue that never
# drains. Measured on 2026-08-07: an agent could `ls` this repo but not read
# .env.collector, and hung trying to exec .venv/bin/python. Refuse to install
# something that cannot work rather than leave a dead schedule behind.
case "${ROOT#$HOME/}" in
  Documents/*|Desktop/*|Downloads/*)
    cat >&2 <<MSG

install-launchd: this repo is inside a folder macOS protects.

    $ROOT

A launch agent cannot read files under ~/Documents, ~/Desktop or
~/Downloads, and gets no prompt to ask -- it just fails silently. Two ways
forward:

  1. Move the repo somewhere unprotected. Cleanest, no system settings:

       mv "$ROOT" ~/pennyrun
       cd ~/pennyrun && bash deploy/install-launchd.sh

     .env.collector and .collector-data/ travel with it; nothing else
     needs changing.

  2. Grant Full Disk Access to the interpreter, in
     System Settings -> Privacy & Security -> Full Disk Access, adding:

       $ROOT/.venv/bin/python3.12

     Then re-run this script. Note this grants that binary access to
     everything, not just this project.

MSG
    exit 1 ;;
esac

say "checking this machine can still reach Home Depot"
set -a; . "$ROOT/.env.collector"; set +a
if ! "$ROOT/.venv/bin/python" -m tools.checkhost >/dev/null 2>&1; then
  echo "    Home Depot is not answering this address right now."
  echo "    Installing anyway -- the schedule is still correct, and a run that"
  echo "    is refused reports it rather than writing a bad list. Check with:"
  echo "      ./.venv/bin/python -m tools.checkhost"
fi

mkdir -p "$AGENTS" "$LOGS"

# ---------------------------------------------------------------- plists

write_plist() {  # label, log-basename, then ProgramArguments lines on stdin
  local label="$1" logname="$2"
  cat > "$AGENTS/$label.plist" <<HEAD
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$label</string>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>ProgramArguments</key>
  <array>
$(cat)
  </array>
  <key>StandardOutPath</key><string>$LOGS/$logname.log</string>
  <key>StandardErrorPath</key><string>$LOGS/$logname.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
HEAD
}

say "writing $LABEL_SWEEP (nightly 05:10)"
write_plist "$LABEL_SWEEP" sweep <<ARGS
    <string>/bin/bash</string>
    <string>$ROOT/collect.sh</string>
ARGS
cat >> "$AGENTS/$LABEL_SWEEP.plist" <<'TAIL'
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>5</integer><key>Minute</key><integer>10</integer></dict>
  <!-- A Mac asleep at 05:10 runs this when it wakes rather than skipping the
       night entirely. The scan refuses to overwrite a good list with a
       collapsed run, so a late run is safe. -->
  <key>RunAtLoad</key><false/>
</dict>
</plist>
TAIL

say "writing $LABEL_JOBS (every 5 minutes, one check per run)"
write_plist "$LABEL_JOBS" jobs <<ARGS
    <string>$ROOT/.venv/bin/python</string>
    <string>-m</string>
    <string>tools.jobs</string>
    <string>--once</string>
ARGS
cat >> "$AGENTS/$LABEL_JOBS.plist" <<'TAIL'
  <key>StartInterval</key><integer>300</integer>
  <!-- --once rather than a long-running loop on purpose: a laptop that sleeps
       and wakes would otherwise leave a stale daemon, and the request budget
       is persisted to disk so repeated short runs share one ceiling with the
       nightly sweep instead of each starting from an empty window. -->
  <key>RunAtLoad</key><true/>
</dict>
</plist>
TAIL

# ---------------------------------------------------------------- load

say "loading"
unload
for L in "$LABEL_SWEEP" "$LABEL_JOBS"; do
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$L.plist" 2>/dev/null \
    || launchctl load -w "$AGENTS/$L.plist"
done

say "scheduled"
launchctl list | grep -E 'local\.pennyrun' | sed 's/^/    /' || true
cat <<EOF

    nightly sweep     05:10, or on wake if asleep
    check queue       every 5 minutes, one store per run
    logs              $LOGS/

    run the sweep now      bash collect.sh
    drain the queue now    ./.venv/bin/python -m tools.jobs --once
    watch                  tail -f $LOGS/jobs.log
    remove               bash deploy/install-launchd.sh --uninstall
EOF
