#!/bin/sh
# Install the Claude Fleet systemd system units on a Linux host.
#
# Design constraints, from ARCHITECTURE.md:
#   - root-managed system units that execute as an unprivileged user
#   - services bind loopback only
#   - existing Harness units are never touched
#   - a single active coordinator
#
# Everything this script writes is named claude-fleet-*. It refuses to replace a
# unit file it did not generate, so a name collision with an existing Harness
# unit is an error rather than a silent takeover.

set -eu

FLEET_USER=""
DRY_RUN=0
UNIT_DIR=/etc/systemd/system
MARKER="# generated-by: claude-fleet install-systemd.sh"

usage() {
  cat >&2 <<'EOF'
usage: install-systemd.sh --user <name> [--unit-dir DIR] [--dry-run]

  --user      unprivileged account that owns the Fleet install (e.g. nature)
  --unit-dir  where to write units (default /etc/systemd/system)
  --dry-run   print the units instead of installing them
EOF
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --user) FLEET_USER=${2:-}; shift 2 ;;
    --user=*) FLEET_USER=${1#--user=}; shift ;;
    --unit-dir) UNIT_DIR=${2:-}; shift 2 ;;
    --unit-dir=*) UNIT_DIR=${1#--unit-dir=}; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage ;;
    *) printf '%s\n' "install-systemd.sh: unknown argument: $1" >&2; usage ;;
  esac
done

[ -n "$FLEET_USER" ] || usage

# --dry-run only prints unit text, so allow it anywhere: reviewing the units the
# server will get, from the laptop where they are being written, is the point.
if [ "$(uname -s)" != "Linux" ]; then
  if [ "$DRY_RUN" -eq 0 ]; then
    printf '%s\n' "install-systemd.sh: this installer is for Linux; macOS uses LaunchAgents." >&2
    exit 78
  fi
  printf '%s\n' "note: previewing Linux units on $(uname -s); install must run on the server." >&2
fi

if [ "$DRY_RUN" -eq 0 ]; then
  command -v systemctl >/dev/null 2>&1 || {
    printf '%s\n' "install-systemd.sh: systemctl not found." >&2
    exit 78
  }
fi

# HOME is not set for a system service. Resolve it the way the wrapper scripts
# will need it, from the passwd database rather than from the invoking shell.
if command -v getent >/dev/null 2>&1; then
  FLEET_HOME=$(getent passwd "$FLEET_USER" | cut -d: -f6)
else
  # getent is absent on macOS (preview path) and on some minimal images.
  FLEET_HOME=$(python3 -c 'import pwd,sys; print(pwd.getpwnam(sys.argv[1]).pw_dir)' \
    "$FLEET_USER" 2>/dev/null || true)
fi
[ -n "$FLEET_HOME" ] || {
  printf '%s\n' "install-systemd.sh: cannot resolve home directory for $FLEET_USER" >&2
  exit 78
}
FLEET_GROUP=$(id -gn "$FLEET_USER")

BIN_DIR="$FLEET_HOME/.local/bin"
RUNTIME="$FLEET_HOME/.config/claude-fleet/runtime.json"

[ -r "$RUNTIME" ] || {
  printf '%s\n' \
    "install-systemd.sh: runtime config not found: $RUNTIME" \
    "Run the Fleet bootstrap as $FLEET_USER before installing the units." >&2
  exit 78
}

read_runtime() {
  python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get(sys.argv[2],""))' \
    "$RUNTIME" "$1"
}

DB_PATH=$(read_runtime db_path)
[ -n "$DB_PATH" ] || {
  printf '%s\n' "install-systemd.sh: runtime.json has no db_path" >&2
  exit 78
}
STATE_DIR=$(dirname "$DB_PATH")
CONFIG_DIR=$(dirname "$RUNTIME")
COORDINATOR_URL=$(python3 -c 'import json,sys
d=json.load(open(sys.argv[1]))
t=d.get("teamclaude_config","")
try:
    print((json.load(open(t)).get("fleetCoordinator") or {}).get("url",""))
except Exception:
    print("")' "$RUNTIME")

# The maintenance sweep needs to reach the same database and therefore the same
# advisory lock file as the coordinator. Passing --db explicitly keeps the two
# from diverging if runtime.json is later edited.
emit_unit() {
  case "$1" in
    coordinator)
      cat <<EOF
$MARKER
[Unit]
Description=Claude Fleet coordinator (route, writer fencing, policy)
Documentation=file://$FLEET_HOME/.local/share/claude-fleet/ARCHITECTURE.md
After=network-online.target
Wants=network-online.target
# StartLimit* belong in [Unit] since systemd v229. In [Service] they are unknown
# keys — warned about and ignored — so the crash-loop bound would not exist.
# The coordinator is fail-closed by design; a crash loop must stay visible rather
# than be papered over by infinite fast restarts.
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=$FLEET_USER
Group=$FLEET_GROUP
Environment=HOME=$FLEET_HOME
Environment=XDG_CONFIG_HOME=$FLEET_HOME/.config
Environment=XDG_STATE_HOME=$FLEET_HOME/.local/state
Environment=FLEET_RUNTIME_CONFIG=$RUNTIME
# Logs go to files rather than journald on purpose: fleet doctor's runtime
# checks read them, and on a headless box that health check is the only thing
# watching. The sink adds timestamps and caps size.
#
# log-sink OWNS the service process (note the --) instead of being piped into.
# A shell pipeline would report the pipeline's LAST exit status, i.e. the sink's,
# which is always 0 — a crashed coordinator would look like a clean exit and
# Restart=on-failure would never fire. Measured: pipeline form turns exit 7 into
# exit 0; this form preserves 7.
ExecStart=$BIN_DIR/fleet log-sink --path $STATE_DIR/coordinator.error.log --stdout-path $STATE_DIR/coordinator.log --max-bytes 8388608 --keep 3 -- $BIN_DIR/claude-fleet-coordinator-service
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
# ProtectHome=read-only covers the state directory too, so re-open it: this is
# where the SQLite database, its advisory writer lock and the log sink live.
ReadWritePaths=$STATE_DIR
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
# Loopback only. The coordinator must never be reachable off-host.
IPAddressAllow=localhost
IPAddressDeny=any

[Install]
WantedBy=multi-user.target
EOF
      ;;
    dataplane)
      cat <<EOF
$MARKER
[Unit]
Description=Claude Fleet data plane (TeamClaude OAuth/MITM/SSE, loopback)
Documentation=file://$FLEET_HOME/.local/share/claude-fleet/ARCHITECTURE.md
# Coordinator transport failure is fail-closed, so starting without it only
# produces held requests. Ordering, not a hard requirement.
After=network-online.target claude-fleet-coordinator.service
Wants=network-online.target
StartLimitIntervalSec=300
StartLimitBurst=10

[Service]
Type=simple
User=$FLEET_USER
Group=$FLEET_GROUP
Environment=HOME=$FLEET_HOME
Environment=XDG_CONFIG_HOME=$FLEET_HOME/.config
Environment=XDG_STATE_HOME=$FLEET_HOME/.local/state
Environment=FLEET_RUNTIME_CONFIG=$RUNTIME
# --stdout-path keeps the streams in separate files. TeamClaude writes
# "All accounts exhausted — holding connection" to stdout and doctor reads that
# file to decide whether the fleet is serving, so merging would blind the check.
ExecStart=$BIN_DIR/fleet log-sink --path $STATE_DIR/dataplane.error.log --stdout-path $STATE_DIR/dataplane.log --max-bytes 8388608 --keep 3 -- $BIN_DIR/claude-fleet-dataplane-service
Restart=on-failure
RestartSec=5
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
# TeamClaude is the sole credential writer and rotates OAuth tokens into
# teamclaude.json, so the config directory must stay writable here.
ReadWritePaths=$STATE_DIR
ReadWritePaths=$CONFIG_DIR
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

[Install]
WantedBy=multi-user.target
EOF
      ;;
    maintenance)
      cat <<EOF
$MARKER
[Unit]
Description=Claude Fleet state retention sweep
Documentation=file://$FLEET_HOME/.local/share/claude-fleet/packaging/systemd/README.md

[Service]
Type=oneshot
User=$FLEET_USER
Group=$FLEET_GROUP
Environment=HOME=$FLEET_HOME
Environment=FLEET_RUNTIME_CONFIG=$RUNTIME
# --db is explicit so the sweep uses the same advisory lock file as the
# coordinator even if runtime.json is edited later.
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
Persistent=yes
RandomizedDelaySec=600

[Install]
WantedBy=timers.target
EOF
      ;;
    tokens)
      # It has been running on both of this project's hosts every minute since
      # long before it was in any repository — which is why this exists. The
      # script shipped; nothing ever started it, so a machine rebuilt from this
      # checkout would get the collector and never run it. Quota readings would
      # simply stop updating, and every routing decision is made from them: an
      # account frozen at "spent" gets no traffic, so it never learns otherwise.
      cat <<EOF
$MARKER
[Unit]
Description=Claude Fleet token collector
Documentation=file://$FLEET_HOME/.local/share/claude-fleet/packaging/systemd/README.md

[Service]
Type=oneshot
User=$FLEET_USER
Group=$FLEET_GROUP
Environment=HOME=$FLEET_HOME
Environment=FLEET_RUNTIME_CONFIG=$RUNTIME
ExecStart=$FLEET_HOME/.local/share/claude-fleet/scripts/fleet-tokens --collect --quiet
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=$STATE_DIR
EOF
      ;;
    tokens-timer)
      cat <<EOF
$MARKER
[Unit]
Description=Collect Claude Fleet token usage every minute

[Timer]
# 2min rather than 1, matching the unit that has been running on this project's
# server: it staggers the first collection past the coordinator's own start.
OnBootSec=2min
OnUnitActiveSec=1min

[Install]
WantedBy=timers.target
EOF
      ;;
  esac
}

install_unit() {
  name=$1
  kind=$2
  target="$UNIT_DIR/$name"
  if [ -e "$target" ] && ! grep -qF "$MARKER" "$target" 2>/dev/null; then
    printf '%s\n' \
      "install-systemd.sh: refusing to overwrite $target" \
      "It was not generated by this installer. Existing Harness units are never replaced." >&2
    exit 73
  fi
  emit_unit "$kind" > "$target.tmp"
  chmod 0644 "$target.tmp"
  mv "$target.tmp" "$target"
  printf '%s\n' "installed $target"
}

if [ "$DRY_RUN" -eq 1 ]; then
  for pair in "claude-fleet-coordinator.service:coordinator" \
              "claude-fleet-dataplane.service:dataplane" \
              "claude-fleet-maintenance.service:maintenance" \
              "claude-fleet-maintenance.timer:maintenance-timer" \
              "claude-fleet-tokens.service:tokens" \
              "claude-fleet-tokens.timer:tokens-timer"; do
    printf '\n===== %s =====\n' "${pair%%:*}"
    emit_unit "${pair#*:}"
  done
  exit 0
fi

[ "$(id -u)" -eq 0 ] || {
  printf '%s\n' "install-systemd.sh: must run as root to write $UNIT_DIR" >&2
  exit 77
}

install_unit claude-fleet-coordinator.service coordinator
install_unit claude-fleet-dataplane.service dataplane
install_unit claude-fleet-maintenance.service maintenance
install_unit claude-fleet-maintenance.timer maintenance-timer
install_unit claude-fleet-tokens.service tokens
install_unit claude-fleet-tokens.timer tokens-timer

systemctl daemon-reload
systemctl enable claude-fleet-coordinator.service claude-fleet-dataplane.service
systemctl enable --now claude-fleet-maintenance.timer claude-fleet-tokens.timer

printf '\n%s\n' "Units installed and enabled (not started)."

case "$COORDINATOR_URL" in
  ""|http://127.0.0.1*|http://localhost*|https://127.0.0.1*)
    ;;
  *)
    printf '%s\n' \
      "" \
      "WARNING: teamclaude.json points fleetCoordinator at $COORDINATOR_URL," \
      "which is not loopback. ARCHITECTURE.md requires loopback-only binding." >&2
    ;;
esac

# runtime.json is written by the macOS bootstrap and carries absolute paths from
# that machine. Each of these is a service that starts and then fails in a way
# that looks like something else, so check them here rather than at 3am.
warn() { printf '%s\n' "WARNING: $*" >&2; }

NODE_BINARY=$(read_runtime node_binary)
if [ -n "$NODE_BINARY" ] && [ ! -x "$NODE_BINARY" ]; then
  warn "node_binary does not exist here: $NODE_BINARY"
  warn "  (a Homebrew path from macOS; set it to this host's node)"
fi

CLAUDE_BINARY=$(read_runtime claude_binary)
if [ -n "$CLAUDE_BINARY" ] && [ ! -e "$CLAUDE_BINARY" ]; then
  warn "claude_binary does not exist here: $CLAUDE_BINARY"
  warn "  (the path pins an exact version, so it also breaks on every upgrade)"
fi

OUTBOUND_PROXY=$(read_runtime outbound_proxy)
if [ -n "$OUTBOUND_PROXY" ]; then
  warn "outbound_proxy is set to $OUTBOUND_PROXY"
  warn "  Every upstream request goes through it. If that is the laptop's local"
  warn "  proxy, clear it or point it at this host's egress, or all requests fail."
fi

# claude-fleet-local derives its workload id from ITERM_SESSION_ID /
# TERM_SESSION_ID, neither of which exists on Linux; it then falls back to
# standalone-$$, so every relaunch becomes a new workload and sticky routing and
# workload policy stop applying. hsm-session-fleet derives a stable
# "hsm:<tmux-session>" id instead and is the supported path here.
if [ ! -x "$BIN_DIR/hsm-session-fleet" ]; then
  warn "hsm-session-fleet is missing from $BIN_DIR."
  warn "  On Linux, launch sessions through it (stable tmux-derived workload id)"
  warn "  rather than claude-fleet-local, whose id falls back to the PID."
fi

PY_VERSION=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
printf '%s\n' "python3 on this host: $PY_VERSION (package requires >= 3.9)"

cat <<EOF

Next steps, as $FLEET_USER:

  systemctl start claude-fleet-coordinator claude-fleet-dataplane
  fleet doctor --db $DB_PATH

Before starting: ARCHITECTURE.md forbids two active coordinators without
distributed fencing. If a macOS coordinator is still running against a shared
account pool, read packaging/systemd/README.md and decide the topology first.
EOF
