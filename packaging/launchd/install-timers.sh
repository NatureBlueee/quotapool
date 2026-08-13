#!/bin/sh
# Install the health check and retention sweep as LaunchAgents on macOS.
#
# The server has had both as systemd timers since the deployment; the Mac had
# neither. So requirement (h) — the fleet can show it is working without a human
# reading logs — held on one host only, and nothing was reclaiming state here.
#
# Measured when this was written: 172,426 events against doctor's 200,000
# warning line, and 8,362 active executions against a 2,000 line. The sweep
# exists and is safe to run against a live coordinator; nothing was calling it.
#
#   install-timers.sh              install and start
#   install-timers.sh --dry-run    print the plists, change nothing
#   install-timers.sh --remove     unload and delete

set -eu

DRY_RUN=0
REMOVE=0
for arg in "$@"; do
  case "$arg" in
    --dry-run) DRY_RUN=1 ;;
    --remove) REMOVE=1 ;;
    -h|--help)
      printf '%s\n' "usage: install-timers.sh [--dry-run|--remove]" >&2
      exit 64 ;;
    *) printf '%s\n' "unknown argument: $arg" >&2; exit 64 ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' "install-timers.sh: macOS only; Linux uses packaging/systemd." >&2
  exit 78
fi

PREFIX=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
AGENTS="$HOME/Library/LaunchAgents"
STATE="$HOME/.local/state/claude-fleet"
FLEET="$HOME/.local/bin/fleet"
DB=$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.config/claude-fleet/runtime.json")))["db_path"])')

HEALTH=com.claude-fleet.health
SWEEP=com.claude-fleet.maintenance

if [ "$REMOVE" -eq 1 ]; then
  for label in "$HEALTH" "$SWEEP"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
    rm -f "$AGENTS/$label.plist"
    printf 'removed %s\n' "$label"
  done
  exit 0
fi

emit_health() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$HEALTH</string>
  <key>ProgramArguments</key>
  <array><string>$PREFIX/scripts/fleet-health</string></array>
  <key>StartInterval</key><integer>900</integer>
  <key>RunAtLoad</key><true/>
  <!-- fleet-health exits 2 when the fleet is not serving. That is a report, not
       a crash: without this launchd would treat a stalled fleet as a failing
       job and start throttling the very check that reports it. -->
  <key>AbandonProcessGroup</key><false/>
  <key>StandardOutPath</key><string>$STATE/health.log</string>
  <key>StandardErrorPath</key><string>$STATE/health.error.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
</dict>
</plist>
PLIST
}

emit_sweep() {
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$SWEEP</string>
  <!-- --db explicitly: the sweep must use the same advisory writer lock file as
       the coordinator, which is derived from the database path. Two lock paths
       against one database is the worst form of the lock convoy. -->
  <key>ProgramArguments</key>
  <array>
    <string>$FLEET</string>
    <string>--db</string><string>$DB</string>
    <string>maintenance</string><string>sweep</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>4</integer><key>Minute</key><integer>40</integer></dict>
  <key>StandardOutPath</key><string>$STATE/maintenance.log</string>
  <key>StandardErrorPath</key><string>$STATE/maintenance.error.log</string>
  <key>ProcessType</key><string>Background</string>
  <key>LowPriorityIO</key><true/>
  <key>Nice</key><integer>5</integer>
</dict>
</plist>
PLIST
}

if [ "$DRY_RUN" -eq 1 ]; then
  printf '\n===== %s.plist =====\n' "$HEALTH"; emit_health
  printf '\n===== %s.plist =====\n' "$SWEEP"; emit_sweep
  exit 0
fi

[ -x "$PREFIX/scripts/fleet-health" ] || {
  printf '%s\n' "missing $PREFIX/scripts/fleet-health" >&2; exit 78; }
[ -x "$FLEET" ] || { printf '%s\n' "missing $FLEET" >&2; exit 78; }

mkdir -p "$AGENTS" "$STATE"
emit_health > "$AGENTS/$HEALTH.plist"
emit_sweep  > "$AGENTS/$SWEEP.plist"
chmod 600 "$AGENTS/$HEALTH.plist" "$AGENTS/$SWEEP.plist"

for label in "$HEALTH" "$SWEEP"; do
  launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
  printf 'installed %s\n' "$label"
done

cat <<EOF

Health runs every 15 minutes, the sweep daily at 04:40.

  latest verdict:  tail -20 $STATE/health.log
  when they run:   launchctl list | grep claude-fleet
  right now:       $PREFIX/scripts/fleet-health

fleet-health exits 2 when the fleet is not serving — wire that into whatever you
already use for alerting. It is the signal doctor cannot give you, because
doctor inspects configuration and this asks whether requests are being routed.
EOF
