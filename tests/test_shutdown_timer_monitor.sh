#!/bin/bash
# Regression tests for shutdown-timer-monitor.sh dispatcher behavior.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
TARGET_SCRIPT="$REPO_DIR/scripts/system-maintenance/bin/shutdown-timer-monitor.sh"
SETUP_SCRIPT="$REPO_DIR/scripts/digital_wellbeing/setup_midnight_shutdown.sh"

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

WORKTREE="$TMP_DIR/worktree"
BIN_DIR="$TMP_DIR/bin"
mkdir -p "$WORKTREE/scripts/system-maintenance/bin" "$BIN_DIR"
cp "$TARGET_SCRIPT" "$WORKTREE/scripts/system-maintenance/bin/shutdown-timer-monitor.sh"

cat >"$BIN_DIR/busctl" <<'EOF'
#!/bin/bash
if [[ $1 == monitor ]]; then
  printf '%s\n' "${MOCK_BUSCTL_LINE:-no relevant event}" | while read -r line; do
    printf '%s\n' "$line"
  done
  exit 0
fi

printf 'unexpected busctl args: %s\n' "$*" >&2
exit 1
EOF
chmod +x "$BIN_DIR/busctl"

cat >"$BIN_DIR/systemctl" <<'EOF'
#!/bin/bash
case "$*" in
  'is-enabled day-specific-shutdown.timer'|'is-active day-specific-shutdown.timer')
    exit 0
    ;;
  'daemon-reload'|'enable day-specific-shutdown.timer'|'start day-specific-shutdown.timer')
    exit 0
    ;;
  *)
    printf 'unexpected systemctl args: %s\n' "$*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$BIN_DIR/systemctl"

cat >"$BIN_DIR/sleep" <<'EOF'
#!/bin/bash
printf '%s\n' "$1" >> "${SLEEP_LOG:?}"
exit 99
EOF
chmod +x "$BIN_DIR/sleep"

cat >"$BIN_DIR/tee" <<'EOF'
#!/bin/bash
while IFS= read -r _; do
  :
done
EOF
chmod +x "$BIN_DIR/tee"

run_case() {
  local expected_mode="$1"
  local busctl_present="$2"
  local sleep_log="$TMP_DIR/sleep.log"
  local mode_file="$TMP_DIR/mode.log"

  : >"$sleep_log"
  : >"$mode_file"
  if [[ $busctl_present -eq 0 ]]; then
    mv "$BIN_DIR/busctl" "$BIN_DIR/busctl.off"
  fi

  mode=$(env -i PATH="$BIN_DIR" SLEEP_LOG="$sleep_log" SHUTDOWN_TIMER_MONITOR_SKIP_MAIN=1 /bin/bash -c \
    "source '$WORKTREE/scripts/system-maintenance/bin/shutdown-timer-monitor.sh'; \
     timer_needs_restoration() { return 1; }; \
     restore_timer() { :; }; \
     monitor_with_dbus() { printf 'dbus'; }; \
     monitor_with_polling() { printf 'polling'; }; \
     start_monitoring")

  assert_equals "$expected_mode" "$mode" 'shutdown timer monitor should choose the expected dispatcher'

  if [[ -f "$BIN_DIR/busctl.off" ]]; then
    mv "$BIN_DIR/busctl.off" "$BIN_DIR/busctl"
  fi
}

printf 'Checking D-Bus path is preferred when busctl exists...\n'
run_case dbus 1

printf 'Checking polling fallback is used when busctl is absent...\n'
run_case polling 0

printf 'Checking installer template stays in sync with the event-driven monitor...\n'
grep -Fq 'monitor_with_dbus()' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should install the D-Bus monitor helper'
grep -Fq 'start_monitoring()' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should install the start_monitoring dispatcher'
grep -Fq 'if command -v busctl &>/dev/null; then' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should prefer busctl when available'

printf 'shutdown-timer-monitor.sh regression checks passed.\n'
