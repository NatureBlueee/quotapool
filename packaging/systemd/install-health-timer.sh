#!/bin/sh
# Install a recurring fleet-health check plus the retention sweep.
#
# Why this exists: on 2026-07-30 the Harness server had, for fifteen hours,
# routed 17 requests and parked 678, held two accounts at 100% weekly, and spent
# an hour logging `holder is required` every second. `fleet doctor` on that same
# machine reported ok:true, ready_for_canary:true — correctly, because every one
# of its checks inspects configuration, and none asks whether requests are being
# served. Nobody noticed because nobody was looking, which is the normal state of
# a server.
#
# So: something has to look, on a schedule, and it has to be loud only when the
# fleet is genuinely not serving.
#
#   claude-fleet-health.timer       every 15 min — verdict to the journal
#   claude-fleet-maintenance.timer  daily — retention sweep
#
# Read the results with:
#   journalctl -u claude-fleet-health.service -n 40
#   systemctl list-timers 'claude-fleet-*'

set -eu

FLEET_USER=""
DRY_RUN=0
UNIT_DIR=/etc/systemd/system
INTERVAL=15min
MARKER="# generated-by: claude-fleet install-health-timer.sh"

usage() {
  cat >&2 <<'EOF'
usage: install-health-timer.sh --user <name> [--interval 15min] [--dry-run]

  --user      the account that owns the Fleet install (e.g. nature)
  --interval  how often to check (systemd time span, default 15min)
  --dry-run   print the units instead of installing them
EOF
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --user) FLEET_USER=${2:-}; shift 2 ;;
    --user=*) FLEET_USER=${1#--user=}; shift ;;
    --interval) INTERVAL=${2:-}; shift 2 ;;
    --interval=*) INTERVAL=${1#--interval=}; shift ;;
    --unit-dir) UNIT_DIR=${2:-}; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) printf '%s\n' "install-health-timer.sh: unknown argument: $1" >&2; usage ;;
  esac
done

[ -n "$FLEET_USER" ] || usage

if [ "$(uname -s)" != "Linux" ] && [ "$DRY_RUN" -eq 0 ]; then
  printf '%s\n' "install-health-timer.sh: Linux only; macOS uses launchd." >&2
  exit 78
fi

if command -v getent >/dev/null 2>&1; then
  FLEET_HOME=$(getent passwd "$FLEET_USER" | cut -d: -f6)
else
  FLEET_HOME=$(python3 -c 'import pwd,sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' \
    "$FLEET_USER" 2>/dev/null || true)
fi
[ -n "$FLEET_HOME" ] || {
  printf '%s\n' "install-health-timer.sh: cannot resolve home for $FLEET_USER" >&2
  exit 78
}
FLEET_GROUP=$(id -gn "$FLEET_USER")
RUNTIME="$FLEET_HOME/.config/claude-fleet/runtime.json"
PREFIX="$FLEET_HOME/.local/share/claude-fleet"
BIN_DIR="$FLEET_HOME/.local/bin"

[ -r "$RUNTIME" ] || {
  printf '%s\n' "install-health-timer.sh: no runtime config at $RUNTIME" >&2
  exit 78
}
DB_PATH=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("db_path",""))' "$RUNTIME")
STATE_DIR=$(dirname "$DB_PATH")

emit_unit() {
  case "$1" in
    health)
      cat <<EOF
$MARKER
[Unit]
Description=Claude Fleet health check (is the fleet actually serving?)
Documentation=file://$PREFIX/scripts/fleet-health

[Service]
Type=oneshot
User=$FLEET_USER
Group=$FLEET_GROUP
Environment=HOME=$FLEET_HOME
Environment=FLEET_RUNTIME_CONFIG=$RUNTIME
# fleet-health exits 2 when the fleet is not serving. That is a report, not a
# unit failure — SuccessExitStatus keeps a stalled fleet from also showing up
# as a broken timer, which would bury the actual signal.
SuccessExitStatus=0 1 2 3
ExecStart=$PREFIX/scripts/fleet-health
# Reading the data plane journal is what catches clients bypassing Fleet
# entirely. Without this the check runs but is blind to that failure.
SupplementaryGroups=systemd-journal
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$STATE_DIR
EOF
      ;;
    health-timer)
      cat <<EOF
$MARKER
[Unit]
Description=Check every $INTERVAL whether Claude Fleet is serving

[Timer]
OnBootSec=5min
OnUnitActiveSec=$INTERVAL
Persistent=true

[Install]
WantedBy=timers.target
EOF
      ;;
    maintenance)
      cat <<EOF
$MARKER
[Unit]
Description=Claude Fleet state retention sweep
Documentation=file://$PREFIX/packaging/systemd/README.md

[Service]
Type=oneshot
User=$FLEET_USER
Group=$FLEET_GROUP
Environment=HOME=$FLEET_HOME
Environment=FLEET_RUNTIME_CONFIG=$RUNTIME
# --db explicitly, so the sweep uses the same advisory writer lock file as the
# coordinator even if runtime.json is edited later. Two lock paths against one
# database is the worst form of the lock convoy.
ExecStart=$BIN_DIR/fleet --db $DB_PATH maintenance sweep
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$STATE_DIR
EOF
      ;;
    maintenance-timer)
      cat <<EOF
$MARKER
[Unit]
Description=Daily Claude Fleet state retention sweep

[Timer]
OnCalendar=*-*-* 04:30:00
Persistent=true
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF
      ;;
  esac
}

if [ "$DRY_RUN" -eq 1 ]; then
  for pair in "claude-fleet-health.service:health" \
              "claude-fleet-health.timer:health-timer" \
              "claude-fleet-maintenance.service:maintenance" \
              "claude-fleet-maintenance.timer:maintenance-timer"; do
    printf '\n===== %s =====\n' "${pair%%:*}"
    emit_unit "${pair#*:}"
  done
  exit 0
fi

[ "$(id -u)" -eq 0 ] || {
  printf '%s\n' "install-health-timer.sh: must run as root to write $UNIT_DIR" >&2
  exit 77
}
[ -x "$PREFIX/scripts/fleet-health" ] || {
  printf '%s\n' "install-health-timer.sh: $PREFIX/scripts/fleet-health missing" >&2
  exit 78
}

install_unit() {
  target="$UNIT_DIR/$1"
  if [ -e "$target" ] && ! grep -qF "$MARKER" "$target" 2>/dev/null; then
    printf '%s\n' "refusing to overwrite $target (not generated by this installer)" >&2
    exit 73
  fi
  emit_unit "$2" > "$target.tmp"
  chmod 0644 "$target.tmp"
  mv "$target.tmp" "$target"
  printf 'installed %s\n' "$target"
}

install_unit claude-fleet-health.service health
install_unit claude-fleet-health.timer health-timer
install_unit claude-fleet-maintenance.service maintenance
install_unit claude-fleet-maintenance.timer maintenance-timer

systemctl daemon-reload
systemctl enable --now claude-fleet-health.timer claude-fleet-maintenance.timer

printf '\nRunning the health check once now:\n\n'
systemctl start claude-fleet-health.service || true
journalctl -u claude-fleet-health.service -n 25 --no-pager -q || true

cat <<EOF

Timers active. From now on:

  journalctl -u claude-fleet-health.service -n 40    # what it last found
  systemctl list-timers 'claude-fleet-*'             # when it next runs
  $PREFIX/scripts/fleet-health                       # check right now

The health check exits 2 when the fleet is not serving. Wire that into whatever
you already use for alerting — it is the signal `fleet doctor` cannot give you,
because doctor inspects configuration and this asks whether requests are being
routed.
EOF
