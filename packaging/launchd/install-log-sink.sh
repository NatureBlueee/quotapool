#!/bin/sh
# Route the macOS LaunchAgents through `fleet log-sink`.
#
# Why this is not cosmetic. Both agents write straight to StandardOutPath /
# StandardErrorPath, which gives untimestamped lines in a file that is appended
# to across restarts and never rotates. Five of `fleet doctor`'s runtime checks
# consequently report `skipped` on macOS: they cannot tell a current failure from
# one that ended a day ago, and refusing to guess is the only honest answer.
#
# Measured on 2026-07-30: after a data plane restart that stopped the parking
# completely, the stale tail still said "All accounts exhausted" and still
# counted 309 sx.org lines. Zero of either was actually happening.
#
# Applying this makes those five checks work on macOS as they do under systemd,
# and caps both logs at 8MB x 3 generations.
#
# The change takes effect on the next service start. Restarting the data plane
# briefly interrupts in-flight requests, including those of any Claude Code
# session running through it — hence --restart is opt-in rather than automatic.

set -eu

DRY_RUN=0
RESTART=0
LABELS="com.claude-fleet.coordinator com.claude-fleet.dataplane"

usage() {
  cat >&2 <<'EOF'
usage: install-log-sink.sh [--dry-run] [--restart]

  --dry-run   show the rewritten ProgramArguments without touching anything
  --restart   kickstart the agents afterwards so it takes effect now
              (briefly interrupts in-flight requests)
EOF
  exit 64
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY_RUN=1; shift ;;
    --restart) RESTART=1; shift ;;
    -h|--help) usage ;;
    *) printf '%s\n' "install-log-sink.sh: unknown argument: $1" >&2; usage ;;
  esac
done

if [ "$(uname -s)" != "Darwin" ]; then
  printf '%s\n' "install-log-sink.sh: macOS only; Linux uses packaging/systemd." >&2
  exit 78
fi

FLEET="$HOME/.local/bin/fleet"
[ -x "$FLEET" ] || {
  printf '%s\n' "install-log-sink.sh: $FLEET not found or not executable" >&2
  exit 78
}

DRY_RUN="$DRY_RUN" RESTART="$RESTART" LABELS="$LABELS" FLEET="$FLEET" \
python3 <<'PY'
import os, plistlib, shutil, subprocess, sys, time
from pathlib import Path

dry = os.environ["DRY_RUN"] == "1"
restart = os.environ["RESTART"] == "1"
fleet = os.environ["FLEET"]
agents = Path.home() / "Library" / "LaunchAgents"
stamp = time.strftime("%Y%m%dT%H%M%S")
changed = []

for label in os.environ["LABELS"].split():
    path = agents / f"{label}.plist"
    if not path.is_file():
        print(f"skip {label}: {path} not found")
        continue
    with path.open("rb") as handle:
        plist = plistlib.load(handle)

    argv = list(plist.get("ProgramArguments") or [])
    if not argv:
        print(f"skip {label}: no ProgramArguments")
        continue
    if any("log-sink" in str(item) for item in argv):
        print(f"skip {label}: already routed through log-sink")
        continue

    out = plist.get("StandardOutPath")
    err = plist.get("StandardErrorPath")
    if not err:
        print(f"skip {label}: no StandardErrorPath to take over")
        continue

    wrapped = [
        fleet, "log-sink",
        "--path", err,
        *(["--stdout-path", out] if out else []),
        "--max-bytes", "8388608", "--keep", "3",
        "--", *[str(item) for item in argv],
    ]
    plist["ProgramArguments"] = wrapped
    # log-sink now owns those two paths. Keep a separate file for the sink's own
    # failures, so a crash inside the logger is not itself invisible.
    plist["StandardErrorPath"] = f"{err}.sink"
    plist.pop("StandardOutPath", None)

    print(f"\n=== {label}")
    print("  ProgramArguments:")
    for item in wrapped:
        print(f"    {item}")
    print(f"  StandardErrorPath -> {plist['StandardErrorPath']}")

    if dry:
        continue

    backup = path.with_name(f"{path.name}.before-log-sink-{stamp}")
    shutil.copy2(path, backup)
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as handle:
        plistlib.dump(plist, handle)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    print(f"  written (backup: {backup.name})")
    changed.append(label)

if dry:
    print("\ndry run: nothing written")
    sys.exit(0)

if not changed:
    print("\nnothing to change")
    sys.exit(0)

uid = os.getuid()
print("\nbootout+bootstrap is required for a ProgramArguments change to be read.")
for label in changed:
    plist_path = agents / f"{label}.plist"
    if restart:
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"],
                       capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
                           capture_output=True, text=True)
        status = "reloaded" if r.returncode == 0 else f"FAILED: {r.stderr.strip()}"
        print(f"  {label}: {status}")
    else:
        print(f"  launchctl bootout gui/{uid}/{label} && "
              f"launchctl bootstrap gui/{uid} {plist_path}")

if restart:
    print("\nVerify: new lines in the logs should start with a timestamp, and")
    print("`fleet doctor` should no longer skip the five runtime checks.")
PY
