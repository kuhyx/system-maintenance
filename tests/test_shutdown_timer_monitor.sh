#!/bin/bash
# Regression tests for shutdown-timer-monitor.sh.
#
# 2026-10-24: a root `busctl monitor --system org.freedesktop.systemd1` piped
# into a bash read loop fell behind, filled dbus-broker's shared root-on-root
# 'bytes' quota, and the broker disconnected PID1 and logind from the system
# bus. That killed the X session at 21:19. The monitor must poll, never
# eavesdrop on the bus.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
TARGET_SCRIPT="$REPO_DIR/bin/shutdown-timer-monitor.sh"

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

assert_equals() {
  local expected="$1"
  local actual="$2"
  local context="$3"
  if [[ "$expected" != "$actual" ]]; then
    fail "$context (expected: '$expected', actual: '$actual')"
  fi
}

TMP_DIR=$(mktemp -d)
cleanup() {
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

BIN_DIR="$TMP_DIR/bin"
mkdir -p "$BIN_DIR"

# busctl present on PATH: the old code took the D-Bus path whenever it was.
cat >"$BIN_DIR/busctl" <<'EOF'
#!/bin/bash
printf 'busctl %s\n' "$*" >> "${BUSCTL_LOG:?}"
EOF
chmod +x "$BIN_DIR/busctl"

run_shell() {
  env -i PATH="$BIN_DIR" SHUTDOWN_TIMER_MONITOR_SKIP_MAIN=1 \
    BUSCTL_LOG="$TMP_DIR/busctl.log" /bin/bash -c "$1"
}

printf 'Checking start_monitoring polls even when busctl is installed...\n'
: >"$TMP_DIR/busctl.log"
mode=$(run_shell "source '$TARGET_SCRIPT'; LOG_FILE=/dev/null; \
  timer_needs_restoration() { return 1; }; \
  monitor_with_polling() { printf polling; }; \
  start_monitoring 2>/dev/null")
assert_equals 'polling' "$mode" 'start_monitoring should always poll'
assert_equals '' "$(cat "$TMP_DIR/busctl.log")" 'start_monitoring must not invoke busctl'

printf 'Checking the script never runs a bus monitor...\n'
if grep -vE '^\s*#' "$TARGET_SCRIPT" | grep -qE 'busctl[[:space:]]+monitor|dbus-monitor'; then
  fail 'shutdown-timer-monitor.sh still contains a busctl/dbus monitor'
fi

printf 'All shutdown-timer-monitor tests passed.\n'
