#!/bin/bash
# Regression tests for hosts-file-monitor.sh behavior.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
TARGET_SCRIPT="$REPO_DIR/scripts/periodic_background/system-maintenance/bin/hosts-file-monitor.sh"

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

printf 'Checking inotify event path avoids per-event sleep and debounces bursts...\n'
sleep_log="$TMP_DIR/sleep.log"
: >"$sleep_log"
counter_file="$TMP_DIR/debounce-count.log"
: >"$counter_file"
debounce_calls=$(env -i PATH="$BIN_DIR" HOSTS_FILE_MONITOR_SKIP_MAIN=1 SLEEP_LOG="$sleep_log" COUNTER_FILE="$counter_file" MOCK_INOTIFY_OUTPUT=$'/etc/hosts MODIFY 2026-01-01 00:00:00\n/etc/hosts ATTRIB 2026-01-01 00:00:01\n/etc/hosts MODIFY 2026-01-01 00:00:02' /bin/bash -c \
  "source '$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh'; \
  needs_restoration() { printf 'x\n' >> \"\$COUNTER_FILE\"; return 1; }; \
   idx=0; \
   current_epoch() { \
     local out_var=\"\${1:-}\"; \
     local ts; \
     case \$idx in 0) ts='100';; 1) ts='101';; 2) ts='106';; *) ts='999';; esac; \
     idx=\$((idx + 1)); \
     if [[ -n \$out_var ]]; then printf -v \"\$out_var\" '%s' \"\$ts\"; else printf '%s\\n' \"\$ts\"; fi; \
   }; \
   monitor_with_inotify >/dev/null 2>&1 || true; \
  total=0; \
  while IFS= read -r _; do total=\$((total + 1)); done < \"\$COUNTER_FILE\"; \
  printf '%s' \"\$total\"")
assert_equals '2' "$debounce_calls" 'monitor_with_inotify should debounce rapid successive events'

if [[ -s $sleep_log ]]; then
  fail 'monitor_with_inotify should not call sleep in the event path'
fi

printf 'Checking polling wait helper enforces delay on /dev/null stdin...\n'
wait_elapsed=$(env -i PATH="/usr/bin:/bin" HOSTS_FILE_MONITOR_SKIP_MAIN=1 /bin/bash -c \
  "source '$WORKTREE/scripts/system-maintenance/bin/hosts-file-monitor.sh'; \
   start=\$(printf '%(%s)T' -1); \
   wait_seconds 1; \
   end=\$(printf '%(%s)T' -1); \
   printf '%s' \$((end-start))" </dev/null)
assert_equals '1' "$wait_elapsed" 'wait_seconds should not return immediately on /dev/null stdin'

printf 'hosts-file-monitor.sh regression checks passed.\n'
