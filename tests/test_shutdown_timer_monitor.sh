#!/bin/bash
# Regression tests for shutdown-timer-monitor.sh dispatcher behavior.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
TARGET_SCRIPT="$REPO_DIR/scripts/periodic_background/system-maintenance/bin/shutdown-timer-monitor.sh"
SETUP_SCRIPT="$REPO_DIR/scripts/periodic_background/digital_wellbeing/setup_midnight_shutdown.sh"

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
mkdir -p "$WORKTREE/scripts/periodic_background/system-maintenance/bin" "$BIN_DIR"
cp "$TARGET_SCRIPT" "$WORKTREE/scripts/periodic_background/system-maintenance/bin/shutdown-timer-monitor.sh"

cat >"$BIN_DIR/busctl" <<'EOF'
#!/bin/bash
if [[ $1 == monitor ]]; then
  payload=${MOCK_BUSCTL_LINES:-${MOCK_BUSCTL_LINE:-no relevant event}}
  printf '%b\n' "$payload" | while read -r line; do
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
    "source '$WORKTREE/scripts/periodic_background/system-maintenance/bin/shutdown-timer-monitor.sh'; \
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

run_dbus_throttle_case() {
  local ts_sequence="$1"
  local expected_calls="$2"
  local check_interval="${3:-30}"
  local calls
  local counter_file="$TMP_DIR/timer_checks.log"

  : >"$counter_file"

  calls=$(env -i PATH="$BIN_DIR" SHUTDOWN_TIMER_MONITOR_SKIP_MAIN=1 MOCK_BUSCTL_LINES="day-specific-shutdown.timer\nday-specific-shutdown.timer\nday-specific-shutdown.timer" MOCK_TS_SEQUENCE="$ts_sequence" COUNTER_FILE="$counter_file" TEST_CHECK_INTERVAL="$check_interval" /bin/bash -c '
    source "$1"
    CHECK_INTERVAL="$TEST_CHECK_INTERVAL"
    timer_needs_restoration() { printf "x\n" >> "$COUNTER_FILE"; return 1; }
    restore_timer() { :; }
    mock_idx=0
    IFS=" " read -r -a mock_ts <<< "$MOCK_TS_SEQUENCE"
    current_epoch() {
      local out_var="${1:-}"
      local ts_value="${mock_ts[$mock_idx]:-0}"
      mock_idx=$((mock_idx + 1))

      if [[ -n $out_var ]]; then
        printf -v "$out_var" '%s' "$ts_value"
      else
        printf "%s\n" "$ts_value"
      fi
    }
    monitor_with_dbus >/dev/null 2>&1 || true
    timer_checks=0
    while IFS= read -r _; do
      timer_checks=$((timer_checks + 1))
    done < "$COUNTER_FILE"
    printf "%s" "$timer_checks"
  ' _ "$WORKTREE/scripts/periodic_background/system-maintenance/bin/shutdown-timer-monitor.sh")

  assert_equals "$expected_calls" "$calls" 'monitor_with_dbus should throttle repeated relevant events'
}

printf 'Checking D-Bus path is preferred when busctl exists...\n'
run_case dbus 1

printf 'Checking polling fallback is used when busctl is absent...\n'
run_case polling 0

printf 'Checking D-Bus monitor throttles repeated events within interval...\n'
run_dbus_throttle_case '100 105 109' '1'

printf 'Checking D-Bus monitor can process all events when interval is zero...\n'
run_dbus_throttle_case '100 101 102' '3' '0'

printf 'Checking wait helper enforces delay even with /dev/null stdin...\n'
wait_elapsed=$(env -i PATH="/usr/bin:/bin" SHUTDOWN_TIMER_MONITOR_SKIP_MAIN=1 /bin/bash -c \
  "source '$WORKTREE/scripts/periodic_background/system-maintenance/bin/shutdown-timer-monitor.sh'; \
   start=\$(printf '%(%s)T' -1); \
   wait_seconds 1; \
   end=\$(printf '%(%s)T' -1); \
   printf '%s' \$((end-start))" </dev/null)
assert_equals '1' "$wait_elapsed" 'wait_seconds should not return immediately on /dev/null stdin'

printf 'Checking installer template stays in sync with the event-driven monitor...\n'
grep -Fq 'monitor_with_dbus()' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should install the D-Bus monitor helper'
grep -Fq 'start_monitoring()' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should install the start_monitoring dispatcher'
grep -Fq 'if command -v busctl &>/dev/null; then' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should prefer busctl when available'
grep -Fq 'current_epoch now_ts' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should use out-var epoch helper in D-Bus throttling path'
if grep -Fq 'now_ts=$(current_epoch)' "$SETUP_SCRIPT"; then
  fail 'setup_midnight_shutdown.sh should avoid subshell epoch capture in D-Bus path'
fi
if grep -Fq 'now_ts=$(current_epoch)' "$TARGET_SCRIPT"; then
  fail 'runtime shutdown monitor should avoid subshell epoch capture in D-Bus path'
fi
grep -Fq 'OnUnitActiveSec=300' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should run watchdog timer at 300s cadence'
grep -Fq 'wait_seconds()' "$SETUP_SCRIPT" \
  || fail 'setup_midnight_shutdown.sh should install builtin wait helper in polling fallback'
grep -Fq 'wait_seconds "$CHECK_INTERVAL"' "$TARGET_SCRIPT" \
  || fail 'runtime shutdown monitor polling fallback should use builtin wait helper'

printf 'shutdown-timer-monitor.sh regression checks passed.\n'
