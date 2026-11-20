#!/bin/bash
# Regression tests for bin/browser-preexec-wrapper.sh.
#
# The bug these guard (2026-11-20): the log-size watchdog ran `sleep 60` with
# the wrapper's stdout/stderr inherited. When the browser exited, the wrapper
# killed the watchdog subshell but not its `sleep`, which kept the caller's
# pipe open for up to 60 s. Any caller reading the wrapper's output through a
# pipe or $(...) (scripts, headless --dump-dom runs, agents) hung for a minute
# after a browser that had already exited.
#
# Runs fully sandboxed: fake sudo, a stub hosts installer and a fake browser.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_DIR=$(cd -- "$SCRIPT_DIR/.." && pwd)
TARGET_SCRIPT="$REPO_DIR/bin/browser-preexec-wrapper.sh"
# A wrapper whose browser has exited must release the caller's pipe at once;
# the stub preexec and fake browser take a few ms, so 3 s is generous.
readonly PIPE_BUDGET_SEC=3
# Upper bound for one run; well below the 60 s watchdog sleep the bug leaked.
readonly RUN_TIMEOUT_SEC=10

fail() {
  printf 'FAIL: %s\n' "$1" >&2
  exit 1
}

TMP_DIR=$(mktemp -d)
cleanup() {
  # A red run leaves the leaked `sleep 60` behind; reap it by its marker cwd.
  pgrep -f "^sleep 60$" | while read -r pid; do
    [[ $(readlink "/proc/$pid/cwd" 2>/dev/null) == "$TMP_DIR"* ]] && kill "$pid" 2>/dev/null
  done || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# The wrapper resolves the real browser as /usr/bin/<its own name>; when that
# is absent it takes the first argument instead. Pick a Chromium-family name
# (those get the watchdog) that is not installed here.
pick_absent_chromium_name() {
  local name
  for name in vivaldi-stable brave-browser brave thorium-browser google-chrome; do
    [[ -x /usr/bin/$name ]] || {
      echo "$name"
      return 0
    }
  done
  return 1
}

NAME=$(pick_absent_chromium_name) || fail "every Chromium-family name is installed; cannot sandbox"
BIN_DIR="$TMP_DIR/bin"
mkdir -p "$BIN_DIR" "$TMP_DIR/state"
WRAPPER="$BIN_DIR/$NAME"
HOSTS_MARKER="$TMP_DIR/hosts_ran"
STUB_HOSTS="$TMP_DIR/hosts-install.sh"

sed "s|__HOSTS_INSTALL_SCRIPT__|$STUB_HOSTS|g" "$TARGET_SCRIPT" >"$WRAPPER"
chmod +x "$WRAPPER"

printf '#!/bin/bash\ntouch %q\n' "$HOSTS_MARKER" >"$STUB_HOSTS"
chmod +x "$STUB_HOSTS"

# Fake sudo: drop -n and run the command as-is.
cat >"$BIN_DIR/sudo" <<'EOF'
#!/bin/bash
[[ ${1:-} == -n ]] && shift
exec "$@"
EOF
chmod +x "$BIN_DIR/sudo"

# Fake browser: refuses to "start" unless the hosts preexec already ran, then
# prints a DOM and exits with FAKE_RC.
FAKE_BROWSER="$TMP_DIR/fake-browser"
cat >"$FAKE_BROWSER" <<EOF
#!/bin/bash
[[ -f $HOSTS_MARKER ]] || exit 99
echo '<html></html>'
exit "\${FAKE_RC:-0}"
EOF
chmod +x "$FAKE_BROWSER"

run_env=(env "PATH=$BIN_DIR:$PATH" "XDG_STATE_HOME=$TMP_DIR/state")

# 1. Output read through a pipe: EOF must arrive within the budget.
rm -f "$HOSTS_MARKER"
start=$EPOCHREALTIME
set +e
(cd "$TMP_DIR" && timeout "$RUN_TIMEOUT_SEC" bash -c '"$@" | cat >/dev/null' _ \
  "${run_env[@]}" "$WRAPPER" "$FAKE_BROWSER" --headless=new --dump-dom about:blank)
pipe_rc=$?
set -e
elapsed=$(awk -v s="$start" -v e="$EPOCHREALTIME" 'BEGIN { printf "%.2f", e - s }')
echo "piped run: rc=$pipe_rc elapsed=${elapsed}s (budget ${PIPE_BUDGET_SEC}s)"
[[ $pipe_rc -ne 124 ]] || fail "caller's pipe still open after ${RUN_TIMEOUT_SEC}s: watchdog leaked it"
awk -v e="$elapsed" -v b="$PIPE_BUDGET_SEC" 'BEGIN { exit !(e <= b) }' ||
  fail "piped run took ${elapsed}s, budget ${PIPE_BUDGET_SEC}s"
[[ $pipe_rc -eq 0 ]] || fail "piped run exited $pipe_rc, expected 0 (99 = browser started before hosts preexec)"

# 2. The hosts preexec still runs before every launch.
[[ -f $HOSTS_MARKER ]] || fail "hosts install script was not run before the browser"

# 3. The browser's exit code still propagates.
set +e
(cd "$TMP_DIR" && FAKE_RC=3 timeout "$RUN_TIMEOUT_SEC" "${run_env[@]}" "$WRAPPER" "$FAKE_BROWSER" >/dev/null 2>&1)
rc=$?
set -e
[[ $rc -eq 3 ]] || fail "wrapper exited $rc, expected the browser's 3"

# 4. The per-launch log still records the exit verdict.
log=$(find "$TMP_DIR/state/browser-crash-logs" -name "$NAME-*.log" -newer "$HOSTS_MARKER" | head -1)
[[ -n $log ]] || fail "no per-launch log written"
grep -q '^exit code: 3$' "$log" || fail "log $log lacks the exit verdict"

echo "PASS: browser-preexec-wrapper"
