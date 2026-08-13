#!/bin/sh
# Release gate. README has promised this file for a while; it did not exist,
# and neither did any test. Every change in this package lands in a request hot
# path, so "it ran once by hand" was the only evidence any behaviour still held.
#
# Runs: Python control-plane tests, Node data-plane tests, syntax checks across
# every language in the tree, and a schema-migration round trip.
#
# Exits non-zero on the first failure. Safe to run at any time — it touches only
# a scratch database under a temp directory, never the live state.

set -eu

PREFIX=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$PREFIX"

# --release turns "I could not run this check" into a failure.
#
# Without it the gate lies by omission. With no node on PATH the data-plane
# tests print SKIPPED to stderr and leave FAILED at 0; the JavaScript syntax
# check disappears entirely (its `if` has no else, so not even a line of
# output); and on anything but macOS the plist lint block does not exist. The
# script then prints "all checks passed" having run neither of the two suites
# that cover the code holding the OAuth credentials.
#
# Interactive use keeps the lenient behaviour on purpose — a laptop without node
# should still be able to run the Python tests — but nothing may be called a
# release on evidence that was never gathered.
STRICT=0
for arg in "$@"; do
  case "$arg" in
    --release) STRICT=1 ;;
    *) printf 'release-gate.sh: unknown argument: %s\n' "$arg" >&2; exit 2 ;;
  esac
done

FAILED=0
# Names of what failed, so the summary can say it. Without this the gate printed
# `ok` for every check on stdout and then `release gate: FAILED`, because the
# failing section's header and output went to stderr — a reader piping stdout
# saw an all-green run that ended in failure and had nothing to go on.
FAILED_NAMES=""
run() {
  label=$1
  shift
  printf '\n=== %s\n' "$label"
  if "$@"; then
    printf '    ok\n'
  else
    printf '    FAILED\n' >&2
    FAILED=1
    FAILED_NAMES="$FAILED_NAMES
  - $label"
  fi
}

# A check that could not run. Reported either way; fatal only for a release.
skip() {
  if [ "$STRICT" -eq 1 ]; then
    printf '\n=== %s\n    FAILED (--release 要求这项必须真的跑过): %s\n' "$1" "$2" >&2
    FAILED=1
    FAILED_NAMES="$FAILED_NAMES
  - $1（没能运行）"
  else
    printf '\n=== %s\n    SKIPPED: %s\n' "$1" "$2" >&2
  fi
}

PYTHON=${PYTHON:-python3}
NODE=${NODE:-node}
if ! command -v "$NODE" >/dev/null 2>&1; then
  # The runtime config pins the interpreter the services actually use.
  NODE=$("$PYTHON" -c 'import json,os,sys
p=os.path.expanduser(os.environ.get("FLEET_RUNTIME_CONFIG","~/.config/claude-fleet/runtime.json"))
try: print(json.load(open(p)).get("node_binary",""))
except Exception: print("")')
fi

# --- tests ------------------------------------------------------------------

run "Python control-plane tests" \
  "$PYTHON" -m pytest tests/ -q

if [ -n "$NODE" ] && command -v "$NODE" >/dev/null 2>&1; then
  run "Node data-plane tests" \
    sh -c "cd '$PREFIX/vendor/teamclaude' && '$NODE' --test 'test/**/*.test.js'"
else
  skip "Node data-plane tests" "找不到 node —— 数据面 86 条测试一条没跑"
fi

# --- syntax -----------------------------------------------------------------

run "Python compileall" \
  "$PYTHON" -m compileall -q src/claude_fleet scripts

# compileall only looks at *.py, and almost every operational tool in scripts/
# is an extensionless Python file: fleet-rollback, fleet-quota-sync, fleet-health,
# fleet-snapshot, claude-direct… Measured — a deliberately broken file dropped
# into scripts/ passed compileall with exit 0. A typo in the rollback tool would
# have shipped green, and it is the tool you reach for when something else has
# already gone wrong.
run "Python scripts (无扩展名) syntax" "$PYTHON" -c '
import pathlib, sys
bad = []
for path in sorted((pathlib.Path(sys.argv[1]) / "scripts").iterdir()):
    if not path.is_file() or path.suffix:
        continue
    try:
        head = path.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
    except OSError:
        continue
    if not head or "python" not in head[0]:
        continue
    try:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")
    except SyntaxError as exc:
        bad.append("%s:%s %s" % (path.name, exc.lineno, exc.msg))
for line in bad:
    print(line, file=sys.stderr)
sys.exit(1 if bad else 0)
' "$PREFIX"

if [ -n "$NODE" ] && command -v "$NODE" >/dev/null 2>&1; then
  run "JavaScript syntax" sh -c '
    set -eu
    for f in "$1"/vendor/teamclaude/src/*.js; do
      "$2" --check "$f"
    done
  ' _ "$PREFIX" "$NODE"
else
  skip "JavaScript syntax" "找不到 node —— 数据面源码一个文件都没检查"
fi

run "Shell syntax" sh -c '
  set -eu
  for f in "$1"/scripts/*.sh "$1"/packaging/*/*.sh; do
    [ -f "$f" ] || continue
    sh -n "$f"
  done
' _ "$PREFIX"

if [ "$(uname -s)" = "Darwin" ]; then
  # The repo's templates are what gets released, so those are what must lint.
  # This used to check ~/Library/LaunchAgents instead — the files on THIS
  # machine, which no release ever ships and which a broken template would not
  # touch until someone installed it.
  # Templates now, with @HOME@ where an absolute path used to be. Substituting
  # before linting checks the thing the installer will actually write, rather
  # than a file that no machine ever loads.
  run "launchd plist syntax (仓库模板)" sh -c '
    set -eu
    found=0
    tmp=$(mktemp -d)
    trap "rm -rf \"$tmp\"" EXIT
    for f in "$1"/packaging/launchd/*.plist.in; do
      [ -f "$f" ] || continue
      found=1
      sed "s|@HOME@|/tmp/fleet-lint|g" "$f" > "$tmp/$(basename "$f" .in)"
      plutil -lint "$tmp/$(basename "$f" .in)" >/dev/null
    done
    [ "$found" = 1 ]
  ' _ "$PREFIX"
else
  skip "launchd plist syntax" "不是 macOS —— plutil 不可用, 模板未校验"
fi

# --- schema migration round trip --------------------------------------------
# A stale database must migrate forward cleanly. This is the check that would
# have caught a SCHEMA_VERSION bump that forgot its ALTER TABLE.

run "Schema migration round trip" "$PYTHON" - <<'PY'
import os, sqlite3, sys, tempfile
sys.path.insert(0, os.path.join(os.getcwd(), "src"))
from claude_fleet.db import FleetDB, SCHEMA_VERSION

with tempfile.TemporaryDirectory() as tmp:
    path = os.path.join(tmp, "fleet.sqlite3")
    FleetDB(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    # Simulate an older install and migrate forward.
    conn.execute("PRAGMA user_version=0")
    conn.commit()
    conn.close()
    FleetDB(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    conn.close()
PY

printf '\n'
if [ "$FAILED" -ne 0 ]; then
  printf 'release gate: FAILED%s\n' "$FAILED_NAMES" >&2
  exit 1
fi
printf 'release gate: all checks passed\n'
