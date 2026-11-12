#!/bin/bash
# Regression tests for hosts-file-monitor.sh behavior.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
TARGET_SCRIPT="$REPO_DIR/bin/hosts-file-monitor.sh"

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
cp "$TARGET_SCRIPT" "$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh"

cat >"$BIN_DIR/tee" <<'EOF'
#!/bin/bash
while IFS= read -r _; do
  :
done
EOF
chmod +x "$BIN_DIR/tee"

cat >"$BIN_DIR/sleep" <<'EOF'
#!/bin/bash
printf '%s\n' "$1" >> "${SLEEP_LOG:?}"
exit 99
EOF
chmod +x "$BIN_DIR/sleep"

cat >"$BIN_DIR/inotifywait" <<'EOF'
#!/bin/bash
while IFS= read -r line; do
  printf '%s\n' "$line"
done <<< "${MOCK_INOTIFY_OUTPUT:-}"
EOF
chmod +x "$BIN_DIR/inotifywait"
# needs_restoration shells out to grep; the restricted PATH must still have it.
ln -s "$(command -v grep)" "$BIN_DIR/grep"

run_shell() {
  env -i PATH="$BIN_DIR" HOSTS_FILE_MONITOR_SKIP_MAIN=1 /bin/bash -c "$1"
}

make_hosts_file() {
  local file_path="$1"
  local include_custom="$2"
  local include_stevenblack="$3"
  local i

  : >"$file_path"
  for ((i = 1; i <= 1005; i++)); do
    printf '127.0.0.1 example-%d.test\n' "$i" >>"$file_path"
  done

  if [[ $include_custom -eq 1 ]]; then
    printf '# Custom blocking entries\n' >>"$file_path"
  fi

  if [[ $include_stevenblack -eq 1 ]]; then
    printf '# StevenBlack hosts\n' >>"$file_path"
  fi
}

printf 'Checking intact hosts files are accepted...\n'
hosts_ok="$TMP_DIR/hosts-ok"
make_hosts_file "$hosts_ok" 1 1
ok_result=$(run_shell "source '$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh'; HOSTS_FILE='$hosts_ok'; if needs_restoration; then printf restore; else printf ok; fi")
assert_equals 'ok' "$ok_result" 'needs_restoration should accept intact hosts files'

printf 'Checking missing markers trigger restoration...\n'
hosts_missing="$TMP_DIR/hosts-missing"
make_hosts_file "$hosts_missing" 1 0
missing_result=$(run_shell "source '$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh'; HOSTS_FILE='$hosts_missing'; if needs_restoration; then printf restore; else printf ok; fi")
assert_equals 'restore' "$missing_result" 'needs_restoration should reject files missing required markers'

printf 'Checking inotify path is preferred when available...\n'
inotify_mode=$(run_shell "source '$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh'; monitor_with_inotify() { printf inotify; }; monitor_with_polling() { printf polling; }; start_monitoring")
assert_equals 'inotify' "$inotify_mode" 'start_monitoring should prefer inotifywait when present'

printf 'Checking polling fallback is used without inotifywait...\n'
mv "$BIN_DIR/inotifywait" "$BIN_DIR/inotifywait.off"
poll_mode=$(run_shell "source '$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh'; monitor_with_inotify() { printf inotify; }; monitor_with_polling() { printf polling; }; start_monitoring")
mv "$BIN_DIR/inotifywait.off" "$BIN_DIR/inotifywait"
assert_equals 'polling' "$poll_mode" 'start_monitoring should fall back to polling when inotifywait is absent'

MONITOR="$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh"
EVENTS3=$'/etc/hosts MODIFY 2026-01-01 00:00:00\n/etc/hosts ATTRIB 2026-01-01 00:00:01\n/etc/hosts MODIFY 2026-01-01 00:00:02'
HOLD_FIFO="$TMP_DIR/hold.fifo"
EVENT_FIFO="$TMP_DIR/event.fifo"
mkfifo "$HOLD_FIFO" "$EVENT_FIFO"

# Mock inotifywait with timed events: MOCK_SCRIPT_STEPS="<delay_s>|<line>;..."
# Delays use read -t on a fifo because the PATH sleep stub exits 99.
cat >"$BIN_DIR/inotifywait" <<'EOF'
#!/bin/bash
if [[ -n ${MOCK_SCRIPT_STEPS:-} ]]; then
  exec 3<>"$EVENT_FIFO"
  IFS=';' read -r -a steps <<< "$MOCK_SCRIPT_STEPS"
  for step in "${steps[@]}"; do
    read -r -t "${step%%|*}" -u 3 || true
    printf '%s\n' "${step#*|}"
  done
  exit 0
fi
while IFS= read -r line; do
  printf '%s\n' "$line"
done <<< "${MOCK_INOTIFY_OUTPUT:-}"
EOF
chmod +x "$BIN_DIR/inotifywait"

# Runs monitor_with_inotify with a stub needs_restoration that logs one line
# per call (holding CHECK_HOLD_S seconds, like a slow check), prints the count.
count_checks() {
  local counter_file="$TMP_DIR/checks.log"
  : >"$counter_file"
  env -i PATH="$BIN_DIR" HOSTS_FILE_MONITOR_SKIP_MAIN=1 COUNTER_FILE="$counter_file" \
    MOCK_INOTIFY_OUTPUT="${1:-}" MOCK_SCRIPT_STEPS="${2:-}" EVENT_FIFO="$EVENT_FIFO" \
    HOLD_FIFO="$HOLD_FIFO" CHECK_HOLD_S="${3:-0}" /bin/bash -c \
    "source '$MONITOR'; \
     needs_restoration() { \
       printf 'x\n' >> \"\$COUNTER_FILE\"; \
       if [[ \$CHECK_HOLD_S != 0 ]]; then exec 4<>\"\$HOLD_FIFO\"; read -r -t \"\$CHECK_HOLD_S\" -u 4 || true; fi; \
       return 1; \
     }; \
     restore_hosts_file() { :; }; \
     monitor_with_inotify >/dev/null 2>&1 || true; \
     total=0; \
     while IFS= read -r _; do total=\$((total + 1)); done < \"\$COUNTER_FILE\"; \
     printf '%s' \"\$total\""
}

printf 'Checking a queued burst of events is drained into ONE check...\n'
sleep_log="$TMP_DIR/sleep.log"
: >"$sleep_log"
burst_calls=$(count_checks "$EVENTS3")
assert_equals '1' "$burst_calls" 'a queued burst of events must produce exactly one check'
if [[ -s $sleep_log ]]; then
  fail 'monitor_with_inotify should not call sleep in the event path'
fi

printf 'Checking a tamper shortly after a check (inside the old 5 s cooldown) is not dropped...\n'
late_calls=$(count_checks "" '0.01|/etc/hosts MODIFY t1;1.2|/etc/hosts ATTRIB t2')
assert_equals '2' "$late_calls" 'an event arriving after the burst was drained must trigger its own check'

printf 'Checking a tamper landing DURING a running check triggers one more check...\n'
during_calls=$(count_checks "" '0.01|/etc/hosts MODIFY t1;1.0|/etc/hosts ATTRIB t2' 1.5)
assert_equals '2' "$during_calls" 'an event arriving during a check must trigger a follow-up check'

printf 'Checking events for other files in /etc do not trigger checks...\n'
other_calls=$(count_checks $'/etc/passwd MODIFY t1\n/etc/fstab ATTRIB t2')
assert_equals '0' "$other_calls" 'events on other /etc files must not run the hosts check'

printf 'Checking a grep error fails closed (restore, never intact)...\n'
FAIL_BIN="$TMP_DIR/failbin"
mkdir -p "$FAIL_BIN"
printf '#!/bin/bash\nexit 2\n' >"$FAIL_BIN/grep"
chmod +x "$FAIL_BIN/grep"
grep_err_result=$(env -i PATH="$FAIL_BIN" HOSTS_FILE_MONITOR_SKIP_MAIN=1 /bin/bash -c \
  "source '$MONITOR'; HOSTS_FILE='$hosts_ok'; if needs_restoration; then printf restore; else printf ok; fi")
assert_equals 'restore' "$grep_err_result" 'a grep failure (exit 2) must mean restore'

printf 'Checking line-count boundary and missing file...\n'
hosts_999="$TMP_DIR/hosts-999"
head -n 997 "$hosts_ok" >"$hosts_999"
printf '# Custom blocking entries\n# StevenBlack hosts\n' >>"$hosts_999"
hosts_1000="$TMP_DIR/hosts-1000"
head -n 998 "$hosts_ok" >"$hosts_1000"
printf '# Custom blocking entries\n# StevenBlack hosts\n' >>"$hosts_1000"
short_result=$(run_shell "source '$MONITOR'; HOSTS_FILE='$hosts_999'; if needs_restoration; then printf restore; else printf ok; fi")
assert_equals 'restore' "$short_result" '999 lines (< MIN_HOSTS_LINES) must mean restore'
edge_result=$(run_shell "source '$MONITOR'; HOSTS_FILE='$hosts_1000'; if needs_restoration; then printf restore; else printf ok; fi")
assert_equals 'ok' "$edge_result" 'exactly MIN_HOSTS_LINES lines with both markers must be intact'
missing_file_result=$(run_shell "source '$MONITOR'; HOSTS_FILE='$TMP_DIR/nope'; if needs_restoration; then printf restore; else printf ok; fi")
assert_equals 'restore' "$missing_file_result" 'a missing hosts file must mean restore'

printf 'Checking polling wait helper enforces delay on /dev/null stdin...\n'
wait_elapsed=$(env -i PATH="/usr/bin:/bin" HOSTS_FILE_MONITOR_SKIP_MAIN=1 /bin/bash -c \
  "source '$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh'; \
   start=\$(printf '%(%s)T' -1); \
   wait_seconds 1; \
   end=\$(printf '%(%s)T' -1); \
   printf '%s' \$((end-start))" </dev/null)
assert_equals '1' "$wait_elapsed" 'wait_seconds should not return immediately on /dev/null stdin'

printf 'hosts-file-monitor.sh regression checks passed.\n'
